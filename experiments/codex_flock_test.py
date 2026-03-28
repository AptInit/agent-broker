#!/usr/bin/env python3
import fcntl
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    lock_dir = Path(tempfile.mkdtemp(prefix="codex-flock-test-", dir="/tmp"))
    lock_path = lock_dir / "broker.lock"
    lock_path.write_text("", encoding="utf-8")

    lock_file = open(lock_path, "r+", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    child_code = f"""
import fcntl

f = open({str(lock_path)!r}, "r+", encoding="utf-8")
try:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("child_lock_acquired")
except BlockingIOError:
    print("child_lock_blocked")
"""

    cmd = [
        "codex",
        "sandbox",
        "linux",
        "--full-auto",
        "python3",
        "-c",
        child_code,
    ]

    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=10)

    print("lock_path:", lock_path)
    print("exit_code:", proc.returncode)
    if proc.stdout:
        print("stdout:")
        print(proc.stdout, end="")
    if proc.stderr:
        print("stderr:", file=sys.stderr)
        print(proc.stderr, end="", file=sys.stderr)

    lock_file.close()
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
