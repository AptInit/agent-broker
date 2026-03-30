from __future__ import annotations

import fcntl
import os
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_broker_v2.broker import Broker, ToolSpec
from agent_broker_v2.lifecycle import cleanup_session_ipc, ensure_session_layout
from agent_broker_v2.protocol import build_data_message, build_start_message, decode_message, encode_message
from agent_broker_v2.session import SESSION_ENV_VAR, SessionPaths
from cli.shim_cli_v2 import build_parser


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM_CLI = REPO_ROOT / "cli" / "shim_cli_v2.py"


class BrokerHarness:
    def __init__(self, allowed_tools: dict[str, ToolSpec]) -> None:
        self.allowed_tools = allowed_tools
        self.tempdir = tempfile.TemporaryDirectory(prefix="agent-broker-v2-test-")
        self.paths = SessionPaths.from_dir(self.tempdir.name)
        ensure_session_layout(self.paths)
        self.broker = Broker(paths=self.paths, allowed_tools=allowed_tools)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.broker.serve_until, args=(self.stop_event,))
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        cleanup_session_ipc(self.paths)
        self.tempdir.cleanup()


class ShimCliV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = BrokerHarness(
            {
                "echo": ToolSpec(executable=shutil.which("echo") or "/usr/bin/echo"),
                "python3": ToolSpec(executable=shutil.which("python3") or "/usr/bin/python3"),
            }
        )

    def tearDown(self) -> None:
        self.harness.close()

    def run_shim(self, *args: str, **kwargs) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env[SESSION_ENV_VAR] = str(self.harness.paths.session_dir)
        return subprocess.run(
            ["python3", str(SHIM_CLI), *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            **kwargs,
        )

    def test_cli_timeout_defaults_to_disabled(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.timeout_ms, 0)

    def test_shim_streams_stdout_before_process_exit(self) -> None:
        env = os.environ.copy()
        env[SESSION_ENV_VAR] = str(self.harness.paths.session_dir)
        proc = subprocess.Popen(
            [
                "python3",
                str(SHIM_CLI),
                "--tool",
                "python3",
                "--",
                "-c",
                (
                    "import os,time; "
                    "os.write(1, b'chunk-one\\n'); "
                    "time.sleep(1.0); "
                    "os.write(1, b'chunk-two\\n')"
                ),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None

        readable, _, _ = select.select([proc.stdout], [], [], 2.0)
        self.assertTrue(readable, msg="expected streamed stdout before process exit")
        first_line = proc.stdout.readline()
        self.assertEqual(first_line, b"chunk-one\n")
        self.assertIsNone(proc.poll(), msg="shim should still be running after first chunk")

        remaining_stdout, stderr = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(first_line + remaining_stdout, b"chunk-one\nchunk-two\n")
        self.assertEqual(stderr, b"")

    def test_shim_streams_stdin_and_eof(self) -> None:
        result = self.run_shim(
            "--tool",
            "python3",
            "--",
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())",
            input=b"hello-stream\n",
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"HELLO-STREAM\n")
        self.assertEqual(result.stderr, b"")

    def test_shim_forwards_sigint_to_child(self) -> None:
        env = os.environ.copy()
        env[SESSION_ENV_VAR] = str(self.harness.paths.session_dir)
        proc = subprocess.Popen(
            [
                "python3",
                str(SHIM_CLI),
                "--tool",
                "python3",
                "--",
                "-c",
                (
                    "import os, signal\n"
                    "def handle(signum, frame):\n"
                    "    os.write(1, b'caught-sigint\\n')\n"
                    "    raise SystemExit(0)\n"
                    "signal.signal(signal.SIGINT, handle)\n"
                    "signal.pause()\n"
                ),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=5)

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(stdout, b"caught-sigint\n")
        self.assertEqual(stderr, b"")

    def test_shim_client_side_timeout_escalates_with_signal(self) -> None:
        result = self.run_shim(
            "--tool",
            "python3",
            "--timeout-ms",
            "200",
            "--",
            "-c",
            (
                "import signal, time\n"
                "def handle(signum, frame):\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, handle)\n"
                "time.sleep(10)\n"
            ),
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_broker_reports_protocol_error_for_invalid_client_channel(self) -> None:
        request = build_start_message(
            tool="python3",
            argv=["-c", "import time; time.sleep(10)"],
            cwd=str(REPO_ROOT),
            timeout_ms=1_000,
            env_delta={},
        )
        request_id = request["id"]
        invalid = build_data_message(request_id, channel="stdout", data=b"nope", eof=False)

        with self.harness.paths.client_lock.open("r+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            resp_fd = os.open(self.harness.paths.resp_fifo, os.O_RDONLY | os.O_NONBLOCK)
            try:
                req_fd = os.open(self.harness.paths.req_fifo, os.O_WRONLY)
                try:
                    os.write(req_fd, encode_message(request))
                    os.write(req_fd, encode_message(invalid))
                    stopped = self._read_until_stopped(resp_fd, request_id)
                finally:
                    os.close(req_fd)
            finally:
                os.close(resp_fd)

        self.assertEqual(stopped["reason"], "protocol")
        self.assertEqual(stopped["code"], "BAD_CHANNEL")

    def _read_until_stopped(self, resp_fd: int, request_id: str) -> dict:
        buffer = b""
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            readable, _, _ = select.select([resp_fd], [], [], 0.2)
            if not readable:
                continue
            chunk = os.read(resp_fd, 4096)
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.strip()
                if not line:
                    continue
                frame = decode_message(line)
                if frame.get("id") != request_id:
                    continue
                if frame.get("kind") == "stopped":
                    return frame
        self.fail("timed out waiting for stopped frame")


if __name__ == "__main__":
    unittest.main()
