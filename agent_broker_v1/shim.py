from __future__ import annotations

import fcntl
import os
from pathlib import Path

from agent_broker_v1.protocol import (
    PROTOCOL_VERSION,
    decode_blob,
    decode_message,
    encode_message,
    make_error_response,
    make_request,
)
from agent_broker_v1.session import SESSION_ENV_VAR, SessionPaths


class BrokerShim:
    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths

    @classmethod
    def from_env(cls) -> "BrokerShim":
        return cls(SessionPaths.from_env())

    def run(
        self,
        *,
        tool: str,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_ms: int = 30_000,
        env_delta: dict[str, str] | None = None,
    ) -> dict:
        request = make_request(
            tool=tool,
            argv=argv,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env_delta=env_delta,
        )
        missing_paths = [
            path
            for path in (self.paths.client_lock, self.paths.req_fifo, self.paths.resp_fifo)
            if not path.exists()
        ]
        if missing_paths:
            detail = "session files not found: " + ", ".join(str(path) for path in missing_paths)
            return make_error_response(
                request["id"],
                code="BROKER_UNAVAILABLE",
                detail=detail,
                exit_code=69,
            )

        try:
            with self.paths.client_lock.open("r+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                resp_fd = os.open(self.paths.resp_fifo, os.O_RDONLY)
                try:
                    req_fd = os.open(self.paths.req_fifo, os.O_WRONLY)
                    try:
                        self._write_all(req_fd, encode_message(request))
                    finally:
                        os.close(req_fd)
                    response = self._read_response(resp_fd, request_id=request["id"])
                finally:
                    os.close(resp_fd)
        except FileNotFoundError as exc:
            return make_error_response(
                request["id"],
                code="BROKER_UNAVAILABLE",
                detail=str(exc),
                exit_code=69,
            )
        except OSError as exc:
            return make_error_response(
                request["id"],
                code="BROKER_IO_ERROR",
                detail=str(exc),
                exit_code=70,
            )

        return response

    def run_from_process(
        self,
        *,
        invoked_as: str,
        argv: list[str],
        cwd: str | None = None,
        timeout_ms: int = 30_000,
        env_delta: dict[str, str] | None = None,
        tool_override: str | None = None,
    ) -> dict:
        tool_name = tool_override or infer_tool_name(invoked_as)
        return self.run(
            tool=tool_name,
            argv=argv,
            cwd=cwd or os.getcwd(),
            timeout_ms=timeout_ms,
            env_delta=env_delta,
        )

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short write to broker transport")
            view = view[written:]

    def _read_response(self, resp_fd: int, *, request_id: str) -> dict:
        read_buffer = b""
        while True:
            while b"\n" not in read_buffer:
                try:
                    chunk = os.read(resp_fd, 4096)
                except InterruptedError:
                    continue
                if not chunk:
                    return make_error_response(
                        request_id,
                        code="BROKER_UNAVAILABLE",
                        detail="broker closed the response FIFO without sending a response",
                        exit_code=69,
                    )
                read_buffer += chunk

            response_line, read_buffer = read_buffer.split(b"\n", 1)
            response_line = response_line.strip()
            if not response_line:
                continue

            try:
                response = decode_message(response_line)
            except Exception as exc:
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail=str(exc),
                    exit_code=70,
                )

            if not isinstance(response, dict):
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail="response must decode to an object",
                    exit_code=70,
                )

            if response.get("version") != PROTOCOL_VERSION:
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail=f"expected response version {PROTOCOL_VERSION}",
                    exit_code=70,
                )

            response_id = response.get("id")
            if response_id is None:
                continue
            if response_id != request_id:
                continue
            if not isinstance(response_id, str):
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail="response id must be a string",
                    exit_code=70,
                )

            if response.get("kind") != "result":
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail="unexpected response kind",
                    exit_code=70,
                )

            try:
                response["stdout"] = decode_blob(response.get("stdout_b64", ""))
                response["stderr"] = decode_blob(response.get("stderr_b64", ""))
            except ValueError as exc:
                return make_error_response(
                    request_id,
                    code="BAD_RESPONSE",
                    detail=str(exc),
                    exit_code=70,
                )

            return response


def infer_tool_name(invoked_as: str) -> str:
    return Path(invoked_as).name


def make_missing_session_env_error() -> dict:
    return make_error_response(
        None,
        code="BROKER_UNAVAILABLE",
        detail=f"{SESSION_ENV_VAR} is not set",
        exit_code=69,
    )
