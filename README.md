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

The v1 design is intentionally narrow:

- one broker instance serves one client session
- one session directory contains one FIFO pair, one lock file, and one generated shim-bin directory
- one request/response transaction is in flight per session
- the sandboxed shim serializes access with blocking `flock`

### Components

- `agent_broker_v1.broker`
  Host-side serving layer. Reads requests from the FIFO pair, validates them, executes tools from a supplied allowlist, and writes responses.
- `agent_broker_v1.lifecycle`
  Host-side bootstrap, allowlist, and cleanup layer. Creates or reuses the session directory, builds the runtime allowlist from config, creates shim symlinks for the allowed tools, prints manual bootstrap directions, handles shutdown, and removes session IPC files on exit.
- `agent_broker_v1.broker_config`
  Static broker configuration. Currently holds `BROKER_CFG_V1`, including the configured tool allowlist.
- `agent_broker_v1.session`
  Session path model shared by broker and shim.
- `agent_broker_v1.shim`
  Sandbox-side shim library for request/response transport over the FIFO pair.
- `cli/shim_cli.py`
  Sandbox-side tool wrapper that infers the requested tool from its symlink name.

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

Run the broker from the repository root:

```bash
python3 -m agent_broker_v1
```

The broker:

- creates a fresh session directory under `/tmp` by default
- prints the session directory path
- prints the effective runtime allowlist resolved from broker config
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

## Shim CLI

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
- takes a blocking `flock` on `client.lock`
- in symlink-invoked mode, infers the tool from its symlink name and forwards all arguments untouched
- in direct invocation mode, supports `--tool` and `--timeout-ms`
- forwards stdout, stderr, and exit code from the broker response

If `BROKER_SESSION_DIR` is unset, or the broker has already removed the session files, the shim exits cleanly with a broker-unavailable style error instead of crashing.

## Repo Layout

- `agent_broker_v1/`
  Package code for broker, lifecycle, broker config, protocol, session handling, and shim library.
- `cli/`
  User-facing CLI entrypoints.
- `experiments/`
  Transport and sandbox experiments kept for reference.

## Notes

- This is not designed for a zero-trust environment.
- The broker currently enforces the executable allowlist and basic request validation.
- Working-directory restrictions and environment filtering are not fully implemented yet.
- The current transport is Linux-oriented. Cross-platform transport work is still future work.
