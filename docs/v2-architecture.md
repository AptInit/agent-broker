# V2 Architecture Proposal

This document maps the proposed v2 wire protocol onto concrete broker, shim, session, and testing architecture.

It assumes the wire protocol described in `docs/v2-protocol.md`.

## Design Direction

V2 should widen behavior without throwing away the current narrow operating model.

Keep these constraints:

- one broker instance serves one client session
- one active request exists per session
- one session still uses one request FIFO and one response FIFO
- the shim still holds a blocking `flock` for the full active request
- execution is still explicit brokered execution, not transparent local shell execution

Change these parts:

- replace one-shot request/result handling with streamed request lifetime handling
- replace `subprocess.run(..., capture_output=True)` with a long-lived child process plus incremental IO forwarding
- replace one terminal response object with a stream of `data` and `heartbeat` frames followed by one `stopped` frame

## Repository Shape

V2 should live beside v1 rather than mutating v1 in place.

Suggested layout:

- `agent_broker_v2/protocol.py`
- `agent_broker_v2/session.py`
- `agent_broker_v2/lifecycle.py`
- `agent_broker_v2/broker.py`
- `agent_broker_v2/shim.py`
- `agent_broker_v2/__main__.py`
- `cli/shim_cli_v2.py`
- `tests/test_v2_protocol.py`
- `tests/test_v2_shim_cli.py`

V1 remains intact as the stable point of comparison until v2 behavior is proven.

## Session Layout

V2 can keep the current session directory layout:

- `req.fifo`
- `resp.fifo`
- `client.lock`
- `bin/`
- `logs/`

No extra FIFO is required because:

- client-to-broker traffic already has `req.fifo`
- broker-to-client traffic already has `resp.fifo`
- both directions can remain open for the lifetime of one active request

No extra manifest file is required for the first v2 cut. The branch already assumes a v2-specific implementation and can continue using fixed session-relative paths.

## Broker Process Model

The broker should stop using `subprocess.run()` and move to `subprocess.Popen()` with explicit pipes:

- `stdin=subprocess.PIPE`
- `stdout=subprocess.PIPE`
- `stderr=subprocess.PIPE`

The broker should then run one event loop per active request that monitors:

- `req.fifo` for incoming `data(channel="stdin")` and `data(channel="signal")`
- child `stdout`
- child `stderr`
- heartbeat deadlines
- child process exit state

`selectors.DefaultSelector` is the right fit for this. It matches the need to multiplex FIFO reads and child pipes without introducing extra worker threads.

## Broker Request Lifecycle

Per request, the broker should behave like this:

1. Read and validate one `start` frame.
2. Resolve the allowlisted tool executable.
3. Spawn the child with `Popen`.
4. Register child `stdout`, child `stderr`, and `req.fifo` with the selector.
5. Append outgoing stdout and stderr bytes to per-request log files as they arrive.
6. Emit broker-to-client `data` frames as output bytes become available.
7. Forward client `stdin` frames into child stdin.
8. Forward client `signal` frames to the child with `Popen.send_signal`.
9. Emit broker-to-client `heartbeat` frames during silence.
10. Emit broker-to-client `data(..., eof=true)` when stdout and stderr each reach EOF.
11. Reap the child and emit one terminal `stopped`.

If validation or spawn fails before a child exists, the broker should emit terminal `stopped` with `reason: "protocol"` or `reason: "broker"` as appropriate.

## Shim Process Model

The shim should also move from one blocking request/response exchange into one request-lifetime event loop.

It should:

- acquire `client.lock`
- open `req.fifo` for long-lived writing
- open `resp.fifo` for long-lived reading
- send `start`
- then multiplex:
  - local stdin
  - broker `resp.fifo`
  - local heartbeat deadlines
  - client timeout policy

The shim should use `selectors.DefaultSelector` for the same reason as the broker.

## Shim IO Policy

The shim should keep the current user-facing behavior that the caller cwd is always concrete.

Suggested stdin policy:

- if local stdin is a pipe or file, stream it as `data(channel="stdin")`
- if local stdin is a TTY, either do not read it by default or gate it behind an explicit opt-in CLI mode

That keeps v2 from accidentally claiming full terminal semantics.

Suggested signal policy:

- the shim installs local signal handlers for the direct CLI process
- when the local process receives a signal that should be forwarded, the shim emits `data(channel="signal", signal=...)`
- the shim does not reinterpret the signal into protocol-level meaning

Suggested timeout policy:

- timeout remains a shim policy, not a wire terminal reason
- the default direct CLI behavior is no client timeout
- on client timeout, the shim emits one or more signal-channel `data` frames, such as `SIGTERM` followed by `SIGKILL` after a grace period
- the broker still reports the observed terminal outcome later via `stopped`

## Long-Lived FIFO Handling

V1 closes request and response file descriptors after one round trip. V2 should not do that inside one active request.

Per active request:

- shim keeps `req.fifo` open for writes until stdin is closed and no more signal forwarding is needed
- broker keeps `resp.fifo` open for writes until terminal `stopped` is sent
- broker keeps `req.fifo` open for reads so it can continue receiving stdin and signal frames

At request end:

- broker emits terminal `stopped`
- shim drains and exits
- both sides close their per-request file descriptors

The session itself remains reusable for the next active request, still serialized by `client.lock`.

## State Handling

Even with minimal wire kinds, each side needs explicit local state.

Broker local state:

- no active child
- child spawned
- stdin open
- stdin closed
- stdout open
- stdout closed
- stderr open
- stderr closed
- child exited
- terminal frame sent

Shim local state:

- request started
- stdin open
- stdin closed
- stdout open
- stdout closed
- stderr open
- stderr closed
- terminal frame received

These local booleans matter even though the wire protocol itself is intentionally minimal.

## Heartbeat and Leases

Heartbeats should remain payload-free `heartbeat` frames.

Recommended behavior:

- each side records a monotonic `last_peer_activity`
- any valid frame resets that timer
- if a side has nothing else to send for one heartbeat interval, it sends `heartbeat`
- if peer silence exceeds the heartbeat timeout, the side treats the session as failed

Recommended defaults:

- heartbeat interval: 2000 ms
- heartbeat timeout: 7000 ms

Failure handling:

- shim on broker timeout:
  - stop waiting
  - emit a local broker-unavailable style error to stderr
  - exit nonzero
- broker on client timeout:
  - stop trusting further request progress
  - terminate or signal the child as broker policy dictates
  - emit best-effort `stopped(reason="client", code="CLIENT_LOST")`

## Logging

V2 should preserve the existing per-request stdout and stderr logs, but change how they are written.

Instead of writing logs only after `subprocess.run()` completes, the broker should:

- create stdout and stderr log files when the child is spawned
- append bytes as they are observed on child pipes
- flush logs promptly enough that they remain useful during long-running requests

Optional but useful addition:

- a third per-request event log that records decoded protocol frames in a human-readable form

That event log is not required for the wire design, but it would make transport debugging substantially easier.

## CLI Surface

Suggested direct CLI entrypoint:

- `cli/shim_cli_v2.py`

Suggested user-facing behavior:

- symlink invocation still infers the tool from the invoked name
- direct invocation still supports `--tool`
- direct invocation still supports `--timeout-ms`
- `--timeout-ms 0` means no client timeout and is the default
- direct invocation may add an explicit stdin mode flag later if needed

The CLI should continue to print troubleshooting hints only for structured brokered failures, not for ordinary nonzero child exits.

## Skill and README Impact

When v2 moves from proposal to implementation, these docs need to stay in sync:

- `README.md`
- `scripts/generate_skill.py`
- v2 tests

V2 will change the user troubleshooting model in material ways:

- stdin is now transportable
- stdout and stderr can arrive incrementally
- signals can be forwarded
- one request can remain live for a long time without being interactive in the PTY sense

That means v2 should eventually get its own skill wording rather than silently reusing v1 expectations.

## Error Mapping

Suggested mapping for broker-emitted terminal failures:

- malformed or structurally invalid frames:
  - `stopped(reason="protocol", code=..., detail=...)`
- allowlist rejection:
  - `stopped(reason="broker", code="NOT_ALLOWED", detail=...)`
- missing executable after allowlist resolution:
  - `stopped(reason="broker", code="TOOL_NOT_FOUND", detail=...)`
- child spawn failure:
  - `stopped(reason="broker", code="SPAWN_FAILED", detail=...)`
- lost client heartbeat:
  - `stopped(reason="client", code="CLIENT_LOST", detail=...)`

Observed child termination should not be rewritten into broker-style errors:

- normal exit becomes `stopped(reason="exit", exit_code=...)`
- signal termination becomes `stopped(reason="signal", signal=...)`

## Testing Plan

At minimum, v2 needs tests for:

- incremental stdout delivery before child exit
- incremental stderr delivery before child exit
- streamed stdin delivery and explicit stdin EOF handling
- forwarded signals reaching the child
- child termination reported as `reason="signal"` when observed that way
- client timeout policy translating into signal-channel frames
- heartbeat silence detection on both broker and shim sides
- large payloads spanning multiple transport chunks
- stale or invalid frames producing `reason="protocol"`
- broker recovery for the next request after a prior request ends

The existing v1 tests are still useful as reference for transport framing and broker-unavailable style failures, but they are not sufficient for v2 streaming behavior.

## Implementation Order

Recommended order:

1. Add `agent_broker_v2/protocol.py` with strict encode/decode and validation helpers.
2. Add `agent_broker_v2/session.py` and `agent_broker_v2/lifecycle.py` by adapting the v1 session model.
3. Implement broker-side streamed request handling with `Popen` and `selectors`.
4. Implement shim-side streamed request handling with `selectors`.
5. Add `cli/shim_cli_v2.py`.
6. Add v2 tests for protocol, broker, and shim CLI behavior.
7. Update `README.md` and `scripts/generate_skill.py` when the implementation becomes user-facing.

This keeps v1 stable while giving v2 a clean path to prove out the streamed architecture.
