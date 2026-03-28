#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


CLIENT_CODE = r"""
import json
import os

req_path = os.environ["BROKER_REQ_FIFO"]
resp_path = os.environ["BROKER_RESP_FIFO"]

req = {
    "id": "fifo-test-1",
    "tool": "eda-demo",
    "argv": ["-mode", "batch", "-source", "build.tcl"],
}

with open(req_path, "w", encoding="utf-8") as req_file:
    req_file.write(json.dumps(req) + "\n")
    req_file.flush()

with open(resp_path, "r", encoding="utf-8") as resp_file:
    line = resp_file.readline()
    print(line, end="")
"""


def broker_main(req_fifo: Path, resp_fifo: Path, result: dict) -> None:
    # O_RDWR avoids FIFO open deadlocks when the peer has not opened yet.
    req_fd = os.open(req_fifo, os.O_RDWR)
    resp_fd = os.open(resp_fifo, os.O_RDWR)
    try:
        with os.fdopen(req_fd, "r", encoding="utf-8", closefd=False) as req_file:
            line = req_file.readline().strip()
        result["request"] = line

        response = {
            "id": "fifo-test-1",
            "ok": True,
            "note": "reply from host-side broker through FIFO",
        }
        with os.fdopen(resp_fd, "w", encoding="utf-8", closefd=False) as resp_file:
            resp_file.write(json.dumps(response) + "\n")
            resp_file.flush()
    finally:
        os.close(req_fd)
        os.close(resp_fd)


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="codex-fifo-test-", dir="/tmp"))
    req_fifo = temp_root / "req.fifo"
    resp_fifo = temp_root / "resp.fifo"
    os.mkfifo(req_fifo, 0o666)
    os.mkfifo(resp_fifo, 0o666)

    broker_result = {}
    broker_thread = threading.Thread(
        target=broker_main, args=(req_fifo, resp_fifo, broker_result), daemon=True
    )
    broker_thread.start()

    env = os.environ.copy()
    env["BROKER_REQ_FIFO"] = str(req_fifo)
    env["BROKER_RESP_FIFO"] = str(resp_fifo)

    cmd = [
        "codex",
        "sandbox",
        "linux",
        "python3",
        "-c",
        CLIENT_CODE,
    ]

    proc = subprocess.run(
        cmd,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    broker_thread.join(timeout=2)

    print("req_fifo:", req_fifo)
    print("resp_fifo:", resp_fifo)
    print("exit_code:", proc.returncode)
    if "request" in broker_result:
        print("broker_saw_request:")
        print(broker_result["request"])
    if proc.stdout:
        print("stdout:")
        print(proc.stdout, end="")
    if proc.stderr:
        print("stderr:", file=sys.stderr)
        print(proc.stderr, end="", file=sys.stderr)

    shutil.rmtree(temp_root, ignore_errors=True)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
