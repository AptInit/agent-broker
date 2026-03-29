#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_broker_v1.shim import BrokerShim, make_missing_session_env_error


TROUBLESHOOTING_SKILL = "agent-broker-user-diagnose"


def is_direct_cli_invocation(invoked_as: str) -> bool:
    name = Path(invoked_as).name
    return name in {"shim_cli.py", "shim_cli"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Broker shim CLI. Normally invoke via a symlink named after the tool "
            "you want to request. If brokered execution behaves unexpectedly, "
            f"refer to the {TROUBLESHOOTING_SKILL!r} skill in SKILL.md."
        )
    )
    parser.add_argument(
        "--tool",
        help="Override the inferred tool name for direct testing",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=30_000,
        help="Request timeout in milliseconds",
    )
    parser.add_argument(
        "tool_args",
        nargs=argparse.REMAINDER,
        help="Arguments to forward to the requested tool",
    )
    return parser


def emit_troubleshooting_hint() -> None:
    sys.stderr.buffer.write(
        (
            f"LLM agents: refer to skill {TROUBLESHOOTING_SKILL!r} in SKILL.md.\n"
        ).encode("utf-8")
    )
    sys.stderr.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    direct_cli_mode = is_direct_cli_invocation(sys.argv[0])

    if direct_cli_mode:
        parser = build_parser()
        args = parser.parse_args(argv)
        tool_args = list(args.tool_args)
        if tool_args and tool_args[0] == "--":
            tool_args = tool_args[1:]
        tool_override = args.tool
        timeout_ms = args.timeout_ms
    else:
        tool_args = argv
        tool_override = None
        timeout_ms = 30_000

    try:
        shim = BrokerShim.from_env()
    except KeyError:
        response = make_missing_session_env_error()
    else:
        response = shim.run_from_process(
            invoked_as=sys.argv[0],
            argv=tool_args,
            cwd=os.getcwd(),
            timeout_ms=timeout_ms,
            tool_override=tool_override,
        )

    stdout = response.get("stdout", b"")
    stderr = response.get("stderr", b"")
    if stdout:
        sys.stdout.buffer.write(stdout)
        sys.stdout.buffer.flush()
    if stderr:
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()

    if not response.get("ok", False):
        error = response.get("error", {})
        detail = error.get("detail")
        if detail:
            if not stderr:
                sys.stderr.buffer.write((detail + "\n").encode("utf-8", errors="replace"))
                sys.stderr.buffer.flush()
        emit_troubleshooting_hint()
        return int(response.get("exit_code") or 1)

    return int(response.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
