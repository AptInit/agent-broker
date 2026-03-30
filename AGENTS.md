# AGENTS.md

This file is for repository contributors.

`README.md` is for contributors operating the repo. Generated `SKILL.md` is for users, usually LLM agents, diagnosing brokered CLI behavior.

## What This Repo Is

This repo is a local prototype for brokering a narrow set of host-side CLI executions to a sandboxed client.

The current v1 design is intentionally constrained:

- one broker instance serves one client session
- one session directory owns one request FIFO, one response FIFO, one lock file, one generated shim `bin/`, and one `logs/` directory
- one request/response transaction is in flight per session
- the sandbox-side shim holds a blocking `flock` across the full exchange
- the transport is for non-interactive commands

Do not casually generalize this into a generic RPC framework or a transparent shell replacement. The current premise is narrower than that, and the code and tests assume that narrower model.

## Source Of Truth

When deciding how the system should behave, use these files in this order:

1. `README.md`
2. `tests/test_shim_cli.py`
3. `agent_broker_v1/`
4. `scripts/generate_skill.py`
5. `experiments/`

`experiments/` matters because it captures why the current architecture exists:

- direct `localhost` access is blocked in the target sandbox setup
- direct Unix socket connection is blocked
- arbitrary inherited file descriptors are unreliable
- FIFO pairs work
- `flock` on a regular file works

Do not replace the FIFO-plus-lock design without understanding those constraints first.

## Code Map

- `agent_broker_v1/broker.py`
  Host-side broker. Reads newline-delimited base64 JSON requests, validates them, enforces the allowlist, runs the subprocess, logs stdout/stderr by request id, and returns a base64 JSON result.
- `agent_broker_v1/shim.py`
  Sandbox-side transport client. Builds requests, serializes access with `flock`, filters stale responses, decodes raw stdout/stderr bytes, and turns transport failures into structured error responses.
- `cli/shim_cli.py`
  User-facing wrapper. Usually invoked by symlink name from the generated session `bin/`. Also supports direct debug mode via `--tool`.
- `agent_broker_v1/lifecycle.py`
  Session creation/reuse, FIFO creation, shim symlink generation, startup instructions, and cleanup.
- `agent_broker_v1/config.py`
  Central allowlist config. The broker resolves the runtime allowlist with `shutil.which()` at startup.
- `agent_broker_v1/protocol.py`
  Protocol framing helpers and error response construction.
- `agent_broker_v1/session.py`
  Shared session path model and `BROKER_SESSION_DIR` handling.
- `scripts/generate_skill.py`
  Generates an uncommitted `SKILL.md` that explains brokered CLI behavior to users, especially agents.

## Behavioral Invariants To Preserve

If you change behavior in these areas, assume you must update tests and docs in the same patch.

- Protocol framing is newline-delimited base64-encoded JSON, not plain JSON lines.
- `PROTOCOL_VERSION` is enforced on both sides.
- Broker responses may be written in chunks, but each logical response is still one newline-terminated frame.
- The shim ignores stale complete responses whose `id` does not match the active request.
- The shim also ignores stale frames with `id: null` while waiting for the active request.
- Stdout and stderr are transported as raw bytes via base64 fields, not as decoded text.
- Large outputs must survive chunking intact.
- The broker returns structured errors for validation, timeout, transport, and allowlist failures.
- Missing session files should fail cleanly with broker-unavailable style errors, not tracebacks.
- The broker logs stdout/stderr per request into `logs/`.
- Cleanup removes `req.fifo`, `resp.fifo`, `client.lock`, generated shims, and `bin/`, but leaves `logs/`.
- The current system does not transport stdin and does not promise TTY-like interactive behavior.

## Design Constraints

Contributors should work with the current design, not against it.

- Keep the broker single-session unless the change explicitly broadens the architecture.
- Keep the client-side lock semantics unless you are intentionally redesigning request concurrency.
- Keep `cli/shim_cli.py` usable both by symlink invocation and direct `--tool` invocation.
- Preserve the distinction between configured allowlist and effective runtime allowlist.
  `config.py` lists requested tools; `lifecycle.build_allowlist_from_config()` drops names that do not resolve on the host.
- Preserve the explicit execution boundary in docs and error messages.
  Brokered commands are not ordinary local shell executions.

## Skill Generator Is A Contract

Treat `scripts/generate_skill.py` as a maintained template, not a convenience script.

`README.md` is for contributors operating the repo; generated `SKILL.md` is for users, usually LLM agents, diagnosing brokered CLI behavior.

The generated `SKILL.md` is the repo's user-facing troubleshooting contract for brokered CLI behavior. In practice, that usually means agent users.

Keep the generated skill narrowly scoped to troubleshooting and execution-model diagnosis.

- Put deployment, installation, repository placement, directory renaming, and operator startup workflow in `README.md`.
- Put one-shot session startup examples and operator launch instructions in broker lifecycle output.
- Put only the user-facing diagnostic model in `SKILL.md`: how to recognize brokered execution, what behavior is preserved, what is unsupported, common failure classes, and how to debug them.
- Do not turn the generated skill into a general setup guide for humans operating the repo.

If behavior changes and the generated skill does not change with it, users following that skill will be taught the wrong model of the system.

When you touch any of the following, inspect and usually update `render_skill_md()` in `scripts/generate_skill.py` in the same patch:

- allowlist semantics
- session layout or required environment variables
- direct debug workflow through `cli/shim_cli.py`
- shim-preserved fields such as `argv`, `cwd`, timeout, stdout, stderr, or exit code
- unsupported behavior such as stdin, TTY, or interactive signal handling
- broker error codes or the meaning of common failure classes
- recommended debugging steps
- wording that explains the execution boundary between shim and broker

## Sync Rules

These parts must stay aligned:

- If you change architecture, session layout, manual usage, or failure semantics that affect how contributors operate the repo, update `README.md`.
- If you change anything a user diagnosing brokered CLI behavior would rely on, update `scripts/generate_skill.py`.
- If you change shim or broker transport behavior, update or add tests in `tests/test_shim_cli.py`.
- If you change generated skill content, remember that `SKILL.md` is intentionally not committed; the generator is the source of truth.

Avoid landing behavior changes that update only code, only `README.md`, or only the skill generator. This repo is small enough that drift is usually a sign of an incomplete patch.

## Testing

Run the test suite from the repo root:

```bash
python3 -m unittest
```

At minimum, if you touch transport, shim, broker, lifecycle, or skill generation behavior, run:

```bash
python3 -m unittest tests.test_shim_cli tests.test_generate_skill
```

If you change the generated skill template, also verify:

```bash
python3 scripts/generate_skill.py --check
```

That command should fail when `SKILL.md` is absent or stale. That is expected because `SKILL.md` is a local generated artifact, not a committed file.

## Editing Guidance

- Prefer small patches that preserve the existing narrow contract.
- Read the tests before changing transport behavior.
- Read `scripts/generate_skill.py` before changing anything that affects user troubleshooting guidance.
- Keep deployment and operator workflow guidance out of the generated skill unless it directly changes the troubleshooting model a user needs once brokering is already in play.
- Do not assume shell builtins, aliases, stdin, or interactivity are supported through the broker.
- Do not describe brokered tools as if they run in the caller's shell process.
- Do not commit a generated `SKILL.md` unless a human explicitly asks for that artifact.
- Treat `experiments/` as evidence for architectural decisions, not dead code to delete casually.
- If you are an LLM contributor, do not treat `README.md` as the only documentation surface that matters. Check whether the generated skill would also become stale.

## Good Patch Shape

Strong patches in this repo usually do all of the following:

- change the minimal code necessary
- update tests for the new or preserved invariant
- update `README.md` if contributor-facing operating guidance changed
- update `scripts/generate_skill.py` if user-facing diagnostic guidance would become stale
- preserve clean error handling across the sandbox boundary

If you are unsure whether a behavior is intentional, check `README.md`, `scripts/generate_skill.py`, and `tests/test_shim_cli.py` before changing it.
