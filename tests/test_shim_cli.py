from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from agent_broker_v1.broker import Broker, ToolSpec
from agent_broker_v1.lifecycle import cleanup_session_ipc, ensure_session_layout
from agent_broker_v1.protocol import (
    PROTOCOL_VERSION,
    TRANSPORT_CHUNK_SIZE,
    encode_blob,
    encode_message,
)
from agent_broker_v1.session import SESSION_ENV_VAR, SessionPaths


REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM_CLI = REPO_ROOT / "cli" / "shim_cli.py"


class BrokerHarness:
    def __init__(self, allowed_tools: dict[str, ToolSpec]) -> None:
        self.allowed_tools = allowed_tools
        self.tempdir = tempfile.TemporaryDirectory(prefix="agent-broker-test-")
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


class ShimCliTests(unittest.TestCase):
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

    def test_shim_discards_stale_response_until_request_id_matches(self) -> None:
        stale_response = encode_message(
            {
                "version": PROTOCOL_VERSION,
                "kind": "result",
                "id": "stale-response",
                "ok": True,
                "exit_code": 0,
                "stdout_b64": encode_blob(b"stale\n"),
                "stderr_b64": encode_blob(b""),
            }
        )

        def inject_stale_response() -> None:
            with self.harness.paths.resp_fifo.open("wb", buffering=0) as resp_file:
                resp_file.write(stale_response)

        writer = threading.Thread(target=inject_stale_response)
        writer.start()
        try:
            result = self.run_shim("--tool", "echo", "--", "fresh", timeout=5)
        finally:
            writer.join(timeout=2)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"fresh\n")
        self.assertEqual(result.stderr, b"")

    def test_shim_discards_stale_null_id_error_response(self) -> None:
        stale_response = encode_message(
            {
                "version": PROTOCOL_VERSION,
                "kind": "result",
                "id": None,
                "ok": False,
                "exit_code": 1,
                "error": {
                    "code": "BAD_JSON",
                    "detail": "stale malformed request",
                },
            }
        )

        def inject_stale_response() -> None:
            with self.harness.paths.resp_fifo.open("wb", buffering=0) as resp_file:
                resp_file.write(stale_response)

        writer = threading.Thread(target=inject_stale_response)
        writer.start()
        try:
            result = self.run_shim("--tool", "echo", "--", "fresh-null-id", timeout=5)
        finally:
            writer.join(timeout=2)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"fresh-null-id\n")
        self.assertEqual(result.stderr, b"")

    def test_shim_writes_non_utf8_stdout_bytes(self) -> None:
        result = self.run_shim(
            "--tool",
            "python3",
            "--",
            "-c",
            "import os; os.write(1, b'\\x80\\xff\\n')",
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"\x80\xff\n")
        self.assertEqual(result.stderr, b"")

    def test_shim_handles_large_output_across_transport_chunks(self) -> None:
        payload_size = TRANSPORT_CHUNK_SIZE * 3
        result = self.run_shim(
            "--tool",
            "python3",
            "--",
            "-c",
            f"import os; os.write(1, b'x' * {payload_size})",
            timeout=5,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"x" * payload_size)
        self.assertEqual(result.stderr, b"")

    def test_shim_reports_broker_unavailable_when_broker_closes_response_fifo(self) -> None:
        self.harness.stop_event.set()
        self.harness.thread.join(timeout=2)
        self.assertFalse(self.harness.thread.is_alive())

        def broker_closes_after_request() -> None:
            req_fd = os.open(self.harness.paths.req_fifo, os.O_RDONLY | os.O_NONBLOCK)
            resp_fd = os.open(self.harness.paths.resp_fifo, os.O_RDWR | os.O_NONBLOCK)
            try:
                read_buffer = b""
                while b"\n" not in read_buffer:
                    readable, _, _ = select.select([req_fd], [], [], 2.0)
                    if not readable:
                        return
                    chunk = os.read(req_fd, 4096)
                    if not chunk:
                        continue
                    read_buffer += chunk
            finally:
                os.close(resp_fd)
                os.close(req_fd)

        broker_thread = threading.Thread(target=broker_closes_after_request)
        broker_thread.start()
        try:
            result = self.run_shim("--tool", "echo", "--", "no-response", timeout=5)
        finally:
            broker_thread.join(timeout=2)

        self.assertEqual(result.returncode, 69)
        self.assertEqual(result.stdout, b"")
        self.assertIn(
            b"broker closed the response FIFO without sending a response\n",
            result.stderr,
        )

    def test_broker_recovers_after_abandoned_partial_request(self) -> None:
        req_fd = os.open(self.harness.paths.req_fifo, os.O_WRONLY)
        try:
            os.write(req_fd, b"not-a-complete-frame")
        finally:
            os.close(req_fd)

        result = self.run_shim("--tool", "echo", "--", "after-partial", timeout=5)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"after-partial\n")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
