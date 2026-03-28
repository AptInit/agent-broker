#!/usr/bin/env python3
import json
import os
import socket
import subprocess
import sys
import threading


CLIENT_CODE = r"""
import json
import os
import socket
import sys

fd = int(os.environ["BROKER_FD"])
sock = socket.socket(fileno=fd)
rfile = sock.makefile("r", encoding="utf-8", newline="\n")
wfile = sock.makefile("w", encoding="utf-8", newline="\n")

req = {
    "id": "codex-test-1",
    "tool": "eda-demo",
    "argv": ["-mode", "batch", "-source", "build.tcl"],
}
wfile.write(json.dumps(req) + "\n")
wfile.flush()
print(rfile.readline(), end="")

rfile.close()
wfile.close()
sock.close()
"""


def broker_main(sock: socket.socket) -> None:
    rfile = sock.makefile("r", encoding="utf-8", newline="\n")
    wfile = sock.makefile("w", encoding="utf-8", newline="\n")

    for line in rfile:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        resp = {
            "id": req.get("id"),
            "ok": True,
            "note": "reply from host-side broker through codex sandbox",
        }
        wfile.write(json.dumps(resp) + "\n")
        wfile.flush()
        break

    rfile.close()
    wfile.close()
    sock.close()


def main() -> int:
    broker_sock, client_sock = socket.socketpair()
    client_fd = client_sock.fileno()
    os.set_inheritable(client_fd, True)

    broker_thread = threading.Thread(target=broker_main, args=(broker_sock,), daemon=True)
    broker_thread.start()

    env = os.environ.copy()
    env["BROKER_FD"] = str(client_fd)

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
        pass_fds=(client_fd,),
        text=True,
        capture_output=True,
    )

    client_sock.close()
    broker_thread.join(timeout=2)

    print("exit_code:", proc.returncode)
    if proc.stdout:
        print("stdout:")
        print(proc.stdout, end="")
    if proc.stderr:
        print("stderr:", file=sys.stderr)
        print(proc.stderr, end="", file=sys.stderr)

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
