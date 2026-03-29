from __future__ import annotations

import argparse
import os
import signal
import stat
import shutil
import tempfile
import threading
from pathlib import Path

from agent_broker_v1.broker import Broker, ToolSpec
from agent_broker_v1.config import BROKER_CFG_V1
from agent_broker_v1.session import SessionPaths


def create_session(base_dir: str | Path | None = None) -> SessionPaths:
    root = Path(base_dir or "/tmp").resolve()
    session_dir = Path(
        tempfile.mkdtemp(prefix="agent-broker-v1-", dir=str(root))
    ).resolve()
    paths = SessionPaths.from_dir(session_dir)
    ensure_session_layout(paths)
    return paths


def ensure_fifo(path: Path) -> None:
    if path.exists():
        mode = path.stat().st_mode
        if stat.S_ISFIFO(mode):
            return
        raise RuntimeError(f"expected FIFO at {path}, found a different file type")
    os.mkfifo(path, 0o600)


def ensure_session_layout(paths: SessionPaths) -> None:
    paths.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_fifo(paths.req_fifo)
    ensure_fifo(paths.resp_fifo)
    paths.client_lock.write_text("", encoding="utf-8")
    paths.logs_dir.mkdir(mode=0o700, exist_ok=True)
    paths.shim_bin_dir.mkdir(mode=0o700, exist_ok=True)


def cleanup_session_ipc(paths: SessionPaths) -> None:
    for candidate in (paths.req_fifo, paths.resp_fifo, paths.client_lock):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
    if paths.shim_bin_dir.exists():
        for candidate in paths.shim_bin_dir.iterdir():
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
        try:
            paths.shim_bin_dir.rmdir()
        except OSError:
            pass


def ensure_session_shims(paths: SessionPaths, tool_names: list[str]) -> None:
    paths.shim_bin_dir.mkdir(mode=0o700, exist_ok=True)
    shim_target = Path(__file__).resolve().parent.parent / "cli" / "shim_cli.py"
    for tool_name in tool_names:
        link_path = paths.shim_bin_dir / tool_name
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(shim_target)


def build_allowlist_from_config() -> dict[str, ToolSpec]:
    candidates = {
        tool_name: shutil.which(tool_name)
        for tool_name in BROKER_CFG_V1["allowlist"]
    }
    return {
        name: ToolSpec(executable=path)
        for name, path in candidates.items()
        if path is not None
    }


def print_manual_instructions(
    paths: SessionPaths,
    repo_root: str,
    allowed_tool_names: list[str],
) -> None:
    shim_cli_path = Path(repo_root).resolve() / "cli" / "shim_cli.py"
    print(f"session_dir: {paths.session_dir}", flush=True)
    print("allowlisted tools:", flush=True)
    print(f"  {', '.join(allowed_tool_names)}", flush=True)
    print("example shim command:", flush=True)
    print(
        f"  BROKER_SESSION_DIR={paths.session_dir} PATH={paths.shim_bin_dir}:$PATH echo hello-from-shim",
        flush=True,
    )
    print("example direct test command:", flush=True)
    print(
        f"  BROKER_SESSION_DIR={paths.session_dir} python3 {shim_cli_path} --tool echo -- hello-from-shim",
        flush=True,
    )
    print("press Ctrl-C to stop the broker", flush=True)


def run_manual_session(*, broker: Broker, repo_root: str) -> int:
    stop_event = threading.Event()
    print_manual_instructions(
        broker.paths,
        repo_root,
        sorted(broker.allowed_tools),
    )
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_shutdown(signum, frame) -> None:  # type: ignore[no-untyped-def]
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        broker.serve_until(stop_event)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        stop_event.set()
        cleanup_session_ipc(broker.paths)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the v1 broker for one manual client session."
    )
    parser.add_argument(
        "--session-dir",
        help="Reuse an existing session directory instead of creating a new one",
    )
    args = parser.parse_args(argv)

    repo_root = os.getcwd()
    paths = SessionPaths.from_dir(args.session_dir) if args.session_dir else create_session()
    ensure_session_layout(paths)
    allowed_tools = build_allowlist_from_config()
    ensure_session_shims(paths, sorted(allowed_tools))
    broker = Broker(
        paths=paths,
        allowed_tools=allowed_tools,
    )
    return run_manual_session(
        broker=broker,
        repo_root=repo_root,
    )
