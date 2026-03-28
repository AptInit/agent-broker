from __future__ import annotations

import json
import uuid
from typing import Any


PROTOCOL_VERSION = 1


def make_request(
    *,
    tool: str,
    argv: list[str] | None = None,
    cwd: str | None = None,
    timeout_ms: int = 30_000,
    env_delta: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "id": str(uuid.uuid4()),
        "tool": tool,
        "argv": argv or [],
        "cwd": cwd,
        "timeout_ms": timeout_ms,
        "env_delta": env_delta or {},
    }


def encode_message(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":")) + "\n"


def decode_message(raw: str) -> dict[str, Any]:
    return json.loads(raw)


def make_error_response(
    request_id: str | None,
    *,
    code: str,
    detail: str,
    exit_code: int | None = None,
) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "exit_code": exit_code,
        "error": {
            "code": code,
            "detail": detail,
        },
    }
