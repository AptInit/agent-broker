from __future__ import annotations

import fcntl
import os
from pathlib import Path

from agent_broker_v1.protocol import (
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
                with self.paths.req_fifo.open("w", encoding="utf-8") as req_file:
                    req_file.write(encode_message(request))
                    req_file.flush()
                with self.paths.resp_fifo.open("r", encoding="utf-8") as resp_file:
                    response_line = resp_file.readline()
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

        if not response_line:
            return make_error_response(
                request["id"],
                code="BROKER_UNAVAILABLE",
                detail="broker closed the response FIFO without sending a response",
                exit_code=69,
            )

        try:
            return decode_message(response_line)
        except Exception as exc:
            return make_error_response(
                request["id"],
                code="BAD_RESPONSE",
                detail=str(exc),
                exit_code=70,
            )

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


def infer_tool_name(invoked_as: str) -> str:
    return Path(invoked_as).name


def make_missing_session_env_error() -> dict:
    return make_error_response(
        None,
        code="BROKER_UNAVAILABLE",
        detail=f"{SESSION_ENV_VAR} is not set",
        exit_code=69,
    )
