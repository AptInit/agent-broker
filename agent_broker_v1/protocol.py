from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Any


PROTOCOL_VERSION = 1
TRANSPORT_CHUNK_SIZE = 65_536


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


def encode_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(payload) + b"\n"


def decode_message(raw: bytes | str) -> dict[str, Any]:
    frame = raw.encode("ascii") if isinstance(raw, str) else raw
    payload = base64.b64decode(frame.strip(), validate=True)
    return json.loads(payload.decode("utf-8"))


def encode_blob(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_blob(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError(str(exc)) from exc


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
        "kind": "result",
        "ok": False,
        "exit_code": exit_code,
        "error": {
            "code": code,
            "detail": detail,
        },
    }
