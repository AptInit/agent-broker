# Agent Broker Prototype

This repository contains a local prototype for running a privileged broker outside a sandboxed client.

## Sandbox Findings

The items below describe the sandbox behavior observed while developing this prototype. They explain why the current transport and session model look the way they do:

- direct `localhost` access is blocked from the sandbox in this environment
- direct Unix domain socket `connect()` is blocked from the sandbox even when the socket path is visible
- arbitrary inherited file descriptors are not reliable through `codex sandbox linux`
- a per-client FIFO pair works across the sandbox boundary
- `flock` on a regular file works across the sandbox boundary

## Architecture

The repository currently carries two broker variants:

- `agent_broker_v1`
  One-shot request/result transport with whole-process stdout/stderr capture.
- `agent_broker_v2`
  Streamed request lifetime transport with incremental stdin, stdout, stderr, signal forwarding, and heartbeats.

Both variants keep the same intentionally narrow operating model:

- one broker instance serves one client session
- one session directory contains one FIFO pair, one lock file, and one generated shim-bin directory
- one request/response transaction is in flight per session
- the sandboxed shim holds a blocking `flock` across the full request/response exchange
- the command path is still not a transparent shell replacement

### Components

- `agent_broker_v1.broker`
  Host-side serving layer. Reads requests from the FIFO pair, validates them, executes tools from a supplied allowlist, and writes responses.
- `agent_broker_v1.lifecycle`
  Host-side bootstrap, allowlist, and cleanup layer. Creates or reuses the session directory, builds the runtime allowlist from config, creates shim symlinks for the allowed tools, prints manual bootstrap directions, handles shutdown, and removes session IPC files on exit.
- `agent_broker_v1.config`
  Centralized broker configuration. `config.py` provides defaults, and an optional uncommitted `config_local.py` can replace `BROKER_CFG_V1` with higher priority.
- `agent_broker_v1.session`
  Session path model shared by broker and shim.
- `agent_broker_v1.shim`
  Sandbox-side shim library for request/response transport over the FIFO pair.
- `cli/shim_cli.py`
  Sandbox-side tool wrapper that infers the requested tool from its symlink name.
- `agent_broker_v2.broker`
  Host-side streamed broker. Spawns a child process, forwards stdin and signals, streams stdout and stderr, and emits a terminal stop frame.
- `agent_broker_v2.shim`
  Sandbox-side streamed shim library for the v2 protocol.
- `cli/shim_cli_v2.py`
  Sandbox-side v2 tool wrapper that streams stdio incrementally.

## Session Layout

Each session directory contains:

- `req.fifo`
- `resp.fifo`
- `client.lock`
- `bin/`
- `logs/`

For the client side:

- `BROKER_SESSION_DIR` identifies the session
- symlink-based tool invocation also prepends `<session_dir>/bin` to `PATH`

## Broker Behavior

### V1

Run the broker from the repository root:

```bash
python3 -m agent_broker_v1
```

The broker:

- creates a fresh session directory under `/tmp` by default
- prints the session directory path
- prints the effective runtime allowlist resolved from central config
- creates shim symlinks under `bin/` for the allowlisted tools
- prints example client-side commands that use one-shot environment assignments
- serves until interrupted
- removes `req.fifo`, `resp.fifo`, `client.lock`, generated shim symlinks, and `bin/` on exit
- leaves `logs/` in place

To reuse an existing session directory:

```bash
python3 -m agent_broker_v1 --session-dir /tmp/agent-broker-v1-...
```

When `--session-dir` is used, lifecycle recreates any missing session IPC files in that directory and then regenerates the allowed-tool shims under `bin/`.

### V2

Run the streamed broker from the repository root:

```bash
python3 -m agent_broker_v2
```

The v2 broker:

- creates a fresh session directory under `/tmp` by default
- prints the session directory path and effective allowlist
- creates v2 shim symlinks under `bin/`
- keeps the same FIFO-plus-lock session model as v1
- spawns one child process per active request and forwards `stdin` incrementally
- streams `stdout` and `stderr` back to the shim as data arrives
- forwards client-originated signals to the child process
- emits heartbeats during otherwise silent periods
- removes `req.fifo`, `resp.fifo`, `client.lock`, generated shim symlinks, and `bin/` on exit
- leaves `logs/` in place

V2 protocol notes:

- request and response frames are still newline-delimited base64-encoded JSON
- client frames use `start`, `data`, and `heartbeat`
- broker frames use `data`, `stopped`, and `heartbeat`
- `data` is keyed by `channel`
- client timeout remains shim policy above the wire
- the v2 CLI defaults to no client timeout unless `--timeout-ms` is set
- `stopped` reports observed process termination, not merely requested broker intent

## Shim CLI

### V1

The main client-side entrypoint is [cli/shim_cli.py](cli/shim_cli.py).

Normal usage is via symlink so the tool name is inferred from the invoked filename:

```bash
BROKER_SESSION_DIR=/tmp/agent-broker-v1-... \
PATH=/tmp/agent-broker-v1-.../bin:$PATH \
echo hello-from-shim
```

For direct debugging:

```bash
BROKER_SESSION_DIR=/tmp/agent-broker-v1-... \
python3 /path/to/repo/cli/shim_cli.py --tool echo -- hello-from-shim
```

`cli/shim_cli.py`:

- reads `BROKER_SESSION_DIR`
- derives the FIFO and lock paths from the session directory
- takes a blocking `flock` on `client.lock` for the full transaction
- in symlink-invoked mode, infers the tool from its symlink name and forwards all arguments untouched
- in direct invocation mode, supports `--tool` and `--timeout-ms`
- writes brokered stdout/stderr back to the local process as raw bytes and forwards the exit code
- emits a `SKILL.md` troubleshooting hint on structured broker-side failures

Transport notes:

- request and response messages are newline-delimited base64-encoded JSON
- large responses may be written to the FIFO in multiple raw slices, but they still form one logical newline-terminated response message
- successful responses carry `stdout_b64` and `stderr_b64` fields, which are base64 encodings of the raw tool output bytes
- the client ignores stale complete responses whose request id does not match the active request
- the client fails on malformed or structurally unexpected response frames
- the broker attempts to recover from abandoned or malformed client-side request traffic and continue serving later requests

The current implementation is intended for non-interactive commands. It does not currently transport stdin to brokered tools or preserve interactive signal behavior for foreground terminal use.

If `BROKER_SESSION_DIR` is unset, or the broker has already removed the session files, the shim exits cleanly with a broker-unavailable style error instead of crashing.

### V2

The v2 client-side entrypoint is [cli/shim_cli_v2.py](cli/shim_cli_v2.py).

Normal usage is the same symlink-based pattern, but the transport semantics are different:

- `stdin` can be streamed to the child process
- `stdout` and `stderr` are forwarded incrementally as broker `data` frames arrive
- forwarded signals are sent as `data(channel="signal")`
- the shim defaults to no client timeout and may escalate from `SIGTERM` to `SIGKILL` only when `--timeout-ms` is set
- `heartbeat` frames keep long-lived silent requests alive
- the terminal broker reply is one `stopped` frame, not a v1 `result`

Direct debug usage mirrors v1 with the v2 entrypoint:

```bash
BROKER_SESSION_DIR=/tmp/agent-broker-v2-... \
python3 /path/to/repo/cli/shim_cli_v2.py --tool echo -- hello-from-shim
```

## Generating the Agent Skill

This repo does not commit `SKILL.md`. Instead, a human can generate a local diagnostic skill and hand it to LLM agent users when they need help understanding that some apparent CLI tools are actually brokered.

Generate the skill from the current broker config:

```bash
python3 scripts/generate_skill.py
```

That writes a local `SKILL.md` in the repo root using the current v1 allowlist from `agent_broker_v1/config.py`, or from `agent_broker_v1/config_local.py` when that local override file is present. The generated skill also points users at the v2 config and behavior differences when they are debugging the streamed path. Regenerate it after changing either version's broker config or user-facing execution model.

The intended flow is:

- the human running this repo generates `SKILL.md`
- the human gives that generated skill to LLM agent users
- the agent uses that skill when brokered CLI behavior needs diagnosis

## Repo Layout

- `agent_broker_v1/`
  Package code for broker, lifecycle, central config, protocol, session handling, and shim library.
- `agent_broker_v2/`
  Package code for the streamed broker variant.
- `cli/`
  User-facing CLI entrypoints.
- `docs/`
  V2 design notes and protocol documentation.
- `experiments/`
  Transport and sandbox experiments kept for reference.
- `scripts/`
  Local utilities, including `generate_skill.py` for producing the uncommitted diagnostic skill from config.

## Notes

- This is not designed for a zero-trust environment.
- The broker currently enforces the executable allowlist and basic request validation.
- Working-directory restrictions and environment filtering are not fully implemented yet.
- The current transport is Linux-oriented. Cross-platform transport work is still future work.
