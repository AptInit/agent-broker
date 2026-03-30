# V2 Streaming Protocol Proposal

This document captures the proposed v2 wire protocol for streaming stdio between the sandbox-side shim and the host-side broker.

It is a design note for a future v2 branch. It does not describe the current v1 implementation.

See also `docs/v2-architecture.md` for the component layout and event-loop architecture that sit on top of this wire protocol.

## Goals

- preserve the repo's narrow one-session, one-active-request model
- preserve FIFO transport and client-side `flock`
- stream stdout and stderr incrementally from broker to shim
- stream stdin incrementally from shim to broker
- forward client signals to the broker without requiring the shim to infer their meaning
- keep the wire protocol small and canonical

## Non-Goals

- multiple concurrent active requests per session
- PTY allocation or terminal emulation
- shell job control
- transparent local-process semantics
- broker-enforced request timeout as a wire-level terminal reason

Client-side timeout policy remains above the wire in v2.

## Session Model

V2 keeps the current narrow architecture:

- one broker instance serves one client session
- one session has one active request at a time
- one session uses one request FIFO and one response FIFO
- the shim holds a blocking `flock` across the full active request

The main change from v1 is that the active request becomes a streamed exchange instead of a single request/result round trip.

## Common Envelope

Every frame is newline-delimited base64-encoded JSON.

Every decoded frame has exactly these required common fields:

- `version`
- `id`
- `kind`

Field definitions:

- `version`
  Integer protocol version. V2 frames use `2`.
- `id`
  Request id string. Fixed for the lifetime of one active brokered command.
- `kind`
  Closed-set message kind.

The protocol intentionally does not include a generic sequence number.

## Message Kinds

Client to broker:

- `start`
- `data`
- `heartbeat`

Broker to client:

- `data`
- `stopped`
- `heartbeat`

The protocol intentionally does not define a wire-level `cancel` kind.

If a caller wants to cancel a request, the shim expresses that policy by sending one or more `data` frames on the `signal` channel. The broker forwards those signals to the child process and reports the observed terminal outcome later in `stopped`.

## `start`

`start` opens a request and carries the execution description.

Required fields:

- `tool`
- `argv`
- `cwd`
- `timeout_ms`
- `env_delta`

Field definitions:

- `tool`
  String tool name requested through the broker allowlist.
- `argv`
  Array of strings. Always present, even when empty.
- `cwd`
  String working directory. Required and non-null.
- `timeout_ms`
  Non-negative integer. `0` disables client timeout. The field is still included in the request because the current CLI shim surface models timeout, even though v2 policy expects the client to enforce timeout behavior.
- `env_delta`
  Object with string keys and string values. Always present, even when empty.

Validation rules:

- `tool` must be a string
- `argv` must be an array of strings
- `cwd` must be a non-empty string
- `timeout_ms` must be a non-negative integer
- `env_delta` must be an object of string-to-string entries

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "start",
  "tool": "python3",
  "argv": ["-c", "print('hello')"],
  "cwd": "/work",
  "timeout_ms": 0,
  "env_delta": {}
}
```

## `data`

`data` is a tagged union keyed by `channel`.

Allowed channels by direction:

- client to broker:
  - `stdin`
  - `signal`
- broker to client:
  - `stdout`
  - `stderr`

### Client `data` on `stdin`

Required fields:

- `channel`
- `data_b64`
- `eof`

Field definitions:

- `channel`
  Must be `stdin`.
- `data_b64`
  Base64-encoded raw stdin bytes. May represent empty bytes.
- `eof`
  Boolean. `true` means this frame closes the stdin channel for the active request.

Validation rules:

- `data_b64` must be present
- `eof` must be present
- after a client `stdin` frame with `eof: true`, no further client `stdin` frames are valid for that request

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "data",
  "channel": "stdin",
  "data_b64": "aGVsbG8K",
  "eof": true
}
```

### Client `data` on `signal`

Required fields:

- `channel`
- `signal`

Field definitions:

- `channel`
  Must be `signal`.
- `signal`
  Signal name string such as `SIGINT`, `SIGTERM`, or `SIGKILL`.

Validation rules:

- `data_b64` must be absent
- `eof` must be absent
- the shim does not infer meaning from the signal name
- repeated signal frames are valid while the request is live

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "data",
  "channel": "signal",
  "signal": "SIGTERM"
}
```

### Broker `data` on `stdout` or `stderr`

Required fields:

- `channel`
- `data_b64`
- `eof`

Field definitions:

- `channel`
  Must be `stdout` or `stderr`.
- `data_b64`
  Base64-encoded raw output bytes. May represent empty bytes.
- `eof`
  Boolean. `true` means this frame closes that output channel for the active request.

Validation rules:

- broker never sends `channel: "stdin"` or `channel: "signal"`
- broker may interleave `stdout` and `stderr` frames in observation order
- once an output channel has emitted `eof: true`, no further frames on that channel are valid for that request

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "data",
  "channel": "stdout",
  "data_b64": "b2sK",
  "eof": false
}
```

## `stopped`

`stopped` is the only terminal broker-to-client message kind.

`stopped` is a tagged union keyed by `reason`.

Allowed `reason` values:

- `exit`
- `signal`
- `client`
- `protocol`
- `broker`

### `reason: "exit"`

Required fields:

- `reason`
- `exit_code`

Forbidden fields:

- `signal`
- `code`
- `detail`

Meaning:

- the child exited normally
- `exit_code` is the observed process exit code

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "stopped",
  "reason": "exit",
  "exit_code": 0
}
```

### `reason: "signal"`

Required fields:

- `reason`
- `signal`

Forbidden fields:

- `exit_code`
- `code`
- `detail`

Meaning:

- the child terminated due to an observed signal
- `signal` reports the observed terminating signal, not merely a signal that the broker attempted to forward

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "stopped",
  "reason": "signal",
  "signal": "SIGTERM"
}
```

### `reason: "client"`, `reason: "protocol"`, `reason: "broker"`

Required fields:

- `reason`
- `code`

Optional fields:

- `detail`

Forbidden fields:

- `exit_code`
- `signal`

Meaning:

- `client`
  The broker stopped because of client-side conditions, such as a lost client heartbeat.
- `protocol`
  The request became invalid at the wire level.
- `broker`
  Host-side execution or broker internals failed.

Examples:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "stopped",
  "reason": "client",
  "code": "CLIENT_LOST",
  "detail": "heartbeat lease expired"
}
```

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "stopped",
  "reason": "protocol",
  "code": "BAD_CHANNEL",
  "detail": "client sent channel=stdout"
}
```

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "stopped",
  "reason": "broker",
  "code": "SPAWN_FAILED",
  "detail": "[Errno 2] No such file or directory"
}
```

## `heartbeat`

`heartbeat` has no kind-specific payload fields.

Example:

```json
{
  "version": 2,
  "id": "req-123",
  "kind": "heartbeat"
}
```

Semantics:

- send `heartbeat` only when otherwise silent
- any valid inbound frame resets peer liveness
- if the broker goes silent past the lease timeout, the shim fails locally
- if the client goes silent past the lease timeout, the broker tears down the active request and emits best-effort `stopped` with `reason: "client"`

Suggested defaults:

- heartbeat interval: 2000 ms
- heartbeat timeout: 7000 ms

These values are policy, not part of the wire payload.

## Lifecycle

Minimal lifecycle:

- `idle`
- `live`
- `done`

Transitions:

- client sends `start`: request enters `live`
- either side sends `data` or `heartbeat`: request stays `live`
- broker sends `stopped`: request enters `done`

There is no separate started acknowledgment in the wire protocol. Once the shim sends `start`, the request is considered live until the broker eventually emits `stopped` or the transport fails.

## Validation Invariants

- exactly one active request exists per session
- `start` must be the first frame for a new request id
- `stopped` must be the last broker frame for a request id
- after `stopped`, no further frames for that request are valid
- client `data` frames may only use `stdin` or `signal`
- broker `data` frames may only use `stdout` or `stderr`
- `stdin` frames require `data_b64` and `eof`
- `signal` frames require `signal` and forbid `data_b64` and `eof`
- `stdout` and `stderr` frames require `data_b64` and `eof`
- once any channel has emitted `eof: true`, that channel may not emit further `data` frames

Protocol violations should terminate the active request with broker-emitted `stopped` using `reason: "protocol"`.

## Broker Interpretation Notes

- forwarded signal frames describe requested delivery
- `stopped` describes the observed terminal outcome
- if the child exits normally after receiving a forwarded signal, broker emits `reason: "exit"`
- if the child terminates due to a signal, broker emits `reason: "signal"`

This distinction keeps the wire faithful to observed process state instead of merely reporting broker intent.

## CLI Shim Policy Above The Wire

The CLI shim may still expose higher-level policy that is not part of the wire protocol.

Examples:

- explicitly enabling a client-side timeout and translating it into one or more `signal` channel frames
- treating Ctrl-C as a local decision to send `SIGINT`
- escalating from `SIGTERM` to `SIGKILL` after a grace period

Those behaviors are shim policy. The canonical wire protocol remains:

- `start`
- `data`
- `heartbeat`
- `stopped`
