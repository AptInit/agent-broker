from __future__ import annotations

import os
import select
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_broker_v1.protocol import (
    PROTOCOL_VERSION,
    decode_message,
    encode_message,
    make_error_response,
)
from agent_broker_v1.session import SessionPaths


@dataclass(frozen=True)
class ToolSpec:
    executable: str


class Broker:
    def __init__(self, paths: SessionPaths, allowed_tools: dict[str, ToolSpec]) -> None:
        self.paths = paths
        self.allowed_tools = allowed_tools

    def serve_until(self, stop_event: threading.Event) -> None:
        req_fd = os.open(self.paths.req_fifo, os.O_RDWR | os.O_NONBLOCK)
        resp_fd = os.open(self.paths.resp_fifo, os.O_RDWR)
        read_buffer = b""
        try:
            while not stop_event.is_set():
                readable, _, _ = select.select([req_fd], [], [], 0.2)
                if not readable:
                    continue
                chunk = os.read(req_fd, 4096)
                if not chunk:
                    continue
                read_buffer += chunk
                while b"\n" in read_buffer:
                    raw_line, read_buffer = read_buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    response = self._handle_line(line)
                    os.write(resp_fd, encode_message(response).encode("utf-8"))
        finally:
            os.close(req_fd)
            os.close(resp_fd)

    def _handle_line(self, line: str) -> dict[str, Any]:
        try:
            request = decode_message(line)
        except Exception as exc:
            return make_error_response(
                None,
                code="BAD_JSON",
                detail=str(exc),
                exit_code=1,
            )

        request_id = request.get("id")
        if request.get("version") != PROTOCOL_VERSION:
            return make_error_response(
                request_id,
                code="BAD_VERSION",
                detail=f"expected version {PROTOCOL_VERSION}",
                exit_code=1,
            )

        tool = request.get("tool")
        argv = request.get("argv", [])
        cwd = request.get("cwd")
        timeout_ms = request.get("timeout_ms", 30_000)
        env_delta = request.get("env_delta", {})

        if not isinstance(tool, str):
            return make_error_response(
                request_id,
                code="BAD_REQUEST",
                detail="tool must be a string",
                exit_code=1,
            )
        if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
            return make_error_response(
                request_id,
                code="BAD_REQUEST",
                detail="argv must be a list of strings",
                exit_code=1,
            )
        if cwd is not None and not isinstance(cwd, str):
            return make_error_response(
                request_id,
                code="BAD_REQUEST",
                detail="cwd must be a string or null",
                exit_code=1,
            )
        if not isinstance(timeout_ms, int) or timeout_ms <= 0:
            return make_error_response(
                request_id,
                code="BAD_REQUEST",
                detail="timeout_ms must be a positive integer",
                exit_code=1,
            )
        if not isinstance(env_delta, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env_delta.items()
        ):
            return make_error_response(
                request_id,
                code="BAD_REQUEST",
                detail="env_delta must be a string-to-string object",
                exit_code=1,
            )

        tool_spec = self.allowed_tools.get(tool)
        if tool_spec is None:
            return make_error_response(
                request_id,
                code="NOT_ALLOWED",
                detail=f"tool {tool!r} is not in the broker allowlist",
                exit_code=126,
            )

        workdir = None
        if cwd is not None:
            workdir = str(Path(cwd).resolve())

        command = [tool_spec.executable, *argv]
        env = os.environ.copy()
        env.update(env_delta)

        try:
            result = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_ms / 1000.0,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return make_error_response(
                request_id,
                code="TIMEOUT",
                detail=str(exc),
                exit_code=124,
            )
        except FileNotFoundError as exc:
            return make_error_response(
                request_id,
                code="TOOL_NOT_FOUND",
                detail=str(exc),
                exit_code=127,
            )
        except Exception as exc:
            return make_error_response(
                request_id,
                code="BROKER_ERROR",
                detail=repr(exc),
                exit_code=1,
            )

        stdout_log = self.paths.logs_dir / f"{request_id}.stdout.log"
        stderr_log = self.paths.logs_dir / f"{request_id}.stderr.log"
        stdout_log.write_text(result.stdout, encoding="utf-8")
        stderr_log.write_text(result.stderr, encoding="utf-8")

        return {
            "version": PROTOCOL_VERSION,
            "id": request_id,
            "ok": True,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        }
