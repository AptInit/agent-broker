from __future__ import annotations

import base64
import binascii
import json
import uuid
from typing import Any


PROTOCOL_VERSION = 2
TRANSPORT_CHUNK_SIZE = 65_536
HEARTBEAT_INTERVAL_MS = 2_000
HEARTBEAT_TIMEOUT_MS = 7_000

CLIENT_KINDS = frozenset({"start", "data", "heartbeat"})
BROKER_KINDS = frozenset({"data", "stopped", "heartbeat"})
EXIT_REASONS = frozenset({"exit", "signal", "client", "protocol", "broker"})


class ProtocolError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def new_request_id() -> str:
    return str(uuid.uuid4())


def encode_message(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(payload) + b"\n"


def decode_message(raw: bytes | str) -> dict[str, Any]:
    frame = raw.encode("ascii") if isinstance(raw, str) else raw
    payload = base64.b64decode(frame.strip(), validate=True)
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ProtocolError("BAD_MESSAGE", "frame payload must decode to an object")
    return decoded


def encode_blob(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_blob(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ProtocolError("BAD_BLOB", str(exc)) from exc


def build_start_message(
    *,
    tool: str,
    argv: list[str],
    cwd: str,
    timeout_ms: int,
    env_delta: dict[str, str],
) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "id": new_request_id(),
        "kind": "start",
        "tool": tool,
        "argv": argv,
        "cwd": cwd,
        "timeout_ms": timeout_ms,
        "env_delta": env_delta,
    }


def build_heartbeat_message(request_id: str) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "kind": "heartbeat",
    }


def build_data_message(
    request_id: str,
    *,
    channel: str,
    data: bytes | None = None,
    eof: bool | None = None,
    signal_name: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "kind": "data",
        "channel": channel,
    }
    if data is not None:
        message["data_b64"] = encode_blob(data)
    if eof is not None:
        message["eof"] = eof
    if signal_name is not None:
        message["signal"] = signal_name
    return message


def build_stopped_message(request_id: str, *, reason: str, **fields: Any) -> dict[str, Any]:
    message = {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "kind": "stopped",
        "reason": reason,
    }
    message.update(fields)
    return message


def build_protocol_stop(request_id: str, *, code: str, detail: str) -> dict[str, Any]:
    return build_stopped_message(request_id, reason="protocol", code=code, detail=detail)


def build_broker_stop(request_id: str, *, code: str, detail: str) -> dict[str, Any]:
    return build_stopped_message(request_id, reason="broker", code=code, detail=detail)


def build_client_stop(request_id: str, *, code: str, detail: str) -> dict[str, Any]:
    return build_stopped_message(request_id, reason="client", code=code, detail=detail)


def validate_client_message(message: dict[str, Any]) -> dict[str, Any]:
    kind = validate_envelope(message, allowed_kinds=CLIENT_KINDS)
    if kind == "start":
        return validate_start_message(message)
    if kind == "data":
        return validate_client_data_message(message)
    return validate_heartbeat_message(message)


def validate_broker_message(message: dict[str, Any]) -> dict[str, Any]:
    kind = validate_envelope(message, allowed_kinds=BROKER_KINDS)
    if kind == "data":
        return validate_broker_data_message(message)
    if kind == "stopped":
        return validate_stopped_message(message)
    return validate_heartbeat_message(message)


def validate_envelope(message: dict[str, Any], *, allowed_kinds: set[str] | frozenset[str]) -> str:
    version = message.get("version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("BAD_VERSION", f"expected version {PROTOCOL_VERSION}")

    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise ProtocolError("BAD_ID", "id must be a non-empty string")

    kind = message.get("kind")
    if not isinstance(kind, str):
        raise ProtocolError("BAD_KIND", "kind must be a string")
    if kind not in allowed_kinds:
        raise ProtocolError("BAD_KIND", f"unexpected kind {kind!r}")
    return kind


def validate_start_message(message: dict[str, Any]) -> dict[str, Any]:
    tool = message.get("tool")
    argv = message.get("argv")
    cwd = message.get("cwd")
    timeout_ms = message.get("timeout_ms")
    env_delta = message.get("env_delta")

    if not isinstance(tool, str):
        raise ProtocolError("BAD_TOOL", "tool must be a string")
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise ProtocolError("BAD_ARGV", "argv must be a list of strings")
    if not isinstance(cwd, str) or not cwd:
        raise ProtocolError("BAD_CWD", "cwd must be a non-empty string")
    if not isinstance(timeout_ms, int) or timeout_ms < 0:
        raise ProtocolError("BAD_TIMEOUT", "timeout_ms must be a non-negative integer")
    if not isinstance(env_delta, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in env_delta.items()
    ):
        raise ProtocolError("BAD_ENV", "env_delta must be a string-to-string object")
    return message


def validate_client_data_message(message: dict[str, Any]) -> dict[str, Any]:
    channel = message.get("channel")
    if channel == "stdin":
        data_b64 = message.get("data_b64")
        eof = message.get("eof")
        if not isinstance(data_b64, str):
            raise ProtocolError("BAD_DATA", "stdin frames must include data_b64")
        if not isinstance(eof, bool):
            raise ProtocolError("BAD_EOF", "stdin frames must include eof")
        decode_blob(data_b64)
        return message
    if channel == "signal":
        signal_name = message.get("signal")
        if not isinstance(signal_name, str) or not signal_name:
            raise ProtocolError("BAD_SIGNAL", "signal frames must include signal")
        if "data_b64" in message or "eof" in message:
            raise ProtocolError("BAD_SIGNAL", "signal frames must not include data_b64 or eof")
        return message
    raise ProtocolError("BAD_CHANNEL", "client data channel must be stdin or signal")


def validate_broker_data_message(message: dict[str, Any]) -> dict[str, Any]:
    channel = message.get("channel")
    if channel not in {"stdout", "stderr"}:
        raise ProtocolError("BAD_CHANNEL", "broker data channel must be stdout or stderr")
    data_b64 = message.get("data_b64")
    eof = message.get("eof")
    if not isinstance(data_b64, str):
        raise ProtocolError("BAD_DATA", "output frames must include data_b64")
    if not isinstance(eof, bool):
        raise ProtocolError("BAD_EOF", "output frames must include eof")
    decode_blob(data_b64)
    return message


def validate_stopped_message(message: dict[str, Any]) -> dict[str, Any]:
    reason = message.get("reason")
    if reason not in EXIT_REASONS:
        raise ProtocolError("BAD_REASON", "stopped reason must be exit, signal, client, protocol, or broker")

    if reason == "exit":
        exit_code = message.get("exit_code")
        if not isinstance(exit_code, int):
            raise ProtocolError("BAD_EXIT_CODE", "exit stops must include exit_code")
        _reject_fields(message, {"signal", "code", "detail"})
        return message

    if reason == "signal":
        signal_name = message.get("signal")
        if not isinstance(signal_name, str) or not signal_name:
            raise ProtocolError("BAD_SIGNAL", "signal stops must include signal")
        _reject_fields(message, {"exit_code", "code", "detail"})
        return message

    code = message.get("code")
    if not isinstance(code, str) or not code:
        raise ProtocolError("BAD_CODE", "client/protocol/broker stops must include code")
    detail = message.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise ProtocolError("BAD_DETAIL", "detail must be a string when present")
    _reject_fields(message, {"exit_code", "signal"})
    return message


def validate_heartbeat_message(message: dict[str, Any]) -> dict[str, Any]:
    extra_fields = set(message) - {"version", "id", "kind"}
    if extra_fields:
        raise ProtocolError("BAD_HEARTBEAT", "heartbeat must not include additional fields")
    return message


def _reject_fields(message: dict[str, Any], forbidden: set[str]) -> None:
    present = forbidden.intersection(message)
    if present:
        field_list = ", ".join(sorted(present))
        raise ProtocolError("BAD_FIELDS", f"unexpected fields for message: {field_list}")
