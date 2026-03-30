from __future__ import annotations

import fcntl
import os
import selectors
import signal
import stat
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from agent_broker_v2.protocol import (
    HEARTBEAT_INTERVAL_MS,
    HEARTBEAT_TIMEOUT_MS,
    build_data_message,
    build_heartbeat_message,
    build_start_message,
    build_stopped_message,
    decode_blob,
    decode_message,
    encode_message,
    validate_broker_message,
    ProtocolError,
)
from agent_broker_v2.session import SESSION_ENV_VAR, SessionPaths


class BrokerShim:
    def __init__(
        self,
        paths: SessionPaths,
        *,
        heartbeat_interval_ms: int = HEARTBEAT_INTERVAL_MS,
        heartbeat_timeout_ms: int = HEARTBEAT_TIMEOUT_MS,
    ) -> None:
        self.paths = paths
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.heartbeat_timeout_ms = heartbeat_timeout_ms

    @classmethod
    def from_env(cls) -> "BrokerShim":
        return cls(SessionPaths.from_env())

    def run_from_process(
        self,
        *,
        invoked_as: str,
        argv: list[str],
        cwd: str,
        timeout_ms: int,
        env_delta: dict[str, str] | None = None,
        tool_override: str | None = None,
        stdin_file: Any | None = None,
        stdout_file: Any | None = None,
        stderr_file: Any | None = None,
    ) -> dict[str, Any]:
        tool_name = tool_override or infer_tool_name(invoked_as)
        return self.run(
            tool=tool_name,
            argv=argv,
            cwd=cwd,
            timeout_ms=timeout_ms,
            env_delta=env_delta or {},
            stdin_file=stdin_file or sys.stdin.buffer,
            stdout_file=stdout_file or sys.stdout.buffer,
            stderr_file=stderr_file or sys.stderr.buffer,
        )

    def run(
        self,
        *,
        tool: str,
        argv: list[str],
        cwd: str,
        timeout_ms: int,
        env_delta: dict[str, str],
        stdin_file: Any,
        stdout_file: Any,
        stderr_file: Any,
    ) -> dict[str, Any]:
        start_message = build_start_message(
            tool=tool,
            argv=list(argv),
            cwd=cwd,
            timeout_ms=timeout_ms,
            env_delta=dict(env_delta),
        )

        missing_paths = [
            path
            for path in (self.paths.client_lock, self.paths.req_fifo, self.paths.resp_fifo)
            if not path.exists()
        ]
        if missing_paths:
            detail = "session files not found: " + ", ".join(str(path) for path in missing_paths)
            return build_stopped_message(
                start_message["id"],
                reason="broker",
                code="BROKER_UNAVAILABLE",
                detail=detail,
            )

        try:
            with self.paths.client_lock.open("r+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                resp_fd = os.open(self.paths.resp_fifo, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    req_fd = os.open(self.paths.req_fifo, os.O_WRONLY)
                    try:
                        return self._run_locked(
                            req_fd=req_fd,
                            resp_fd=resp_fd,
                            start_message=start_message,
                            timeout_ms=timeout_ms,
                            stdin_file=stdin_file,
                            stdout_file=stdout_file,
                            stderr_file=stderr_file,
                        )
                    finally:
                        os.close(req_fd)
                finally:
                    os.close(resp_fd)
        except FileNotFoundError as exc:
            return build_stopped_message(
                start_message["id"],
                reason="broker",
                code="BROKER_UNAVAILABLE",
                detail=str(exc),
            )
        except OSError as exc:
            return build_stopped_message(
                start_message["id"],
                reason="broker",
                code="BROKER_IO_ERROR",
                detail=str(exc),
            )

    def _run_locked(
        self,
        *,
        req_fd: int,
        resp_fd: int,
        start_message: dict[str, Any],
        timeout_ms: int,
        stdin_file: Any,
        stdout_file: Any,
        stderr_file: Any,
    ) -> dict[str, Any]:
        request_id = start_message["id"]
        selector = selectors.DefaultSelector()
        read_buffer = b""
        pending_signals: deque[str] = deque()
        stdin_eof_sent = False
        stdout_closed = False
        stderr_closed = False
        last_peer_activity = time.monotonic()
        last_send_at = time.monotonic()
        start_at = time.monotonic()
        sent_timeout_sigterm = False
        sent_timeout_sigkill = False
        timeout_grace_ms = 500

        self._write_all(req_fd, encode_message(start_message))

        stdin_fd = None
        stdin_mode = _classify_stdin(stdin_file)
        if stdin_mode == "stream":
            stdin_fd = stdin_file.fileno()
            os.set_blocking(stdin_fd, False)
            selector.register(stdin_fd, selectors.EVENT_READ, "stdin")
        elif stdin_mode == "drain":
            while True:
                chunk = stdin_file.read(4096)
                if not chunk:
                    break
                self._write_all(
                    req_fd,
                    encode_message(
                        build_data_message(
                            request_id,
                            channel="stdin",
                            data=bytes(chunk),
                            eof=False,
                        )
                    ),
                )
                last_send_at = time.monotonic()
            self._write_all(
                req_fd,
                encode_message(
                    build_data_message(
                        request_id,
                        channel="stdin",
                        data=b"",
                        eof=True,
                    )
                ),
            )
            last_send_at = time.monotonic()
            stdin_eof_sent = True
        else:
            self._write_all(
                req_fd,
                encode_message(
                    build_data_message(
                        request_id,
                        channel="stdin",
                        data=b"",
                        eof=True,
                    )
                ),
            )
            last_send_at = time.monotonic()
            stdin_eof_sent = True

        selector.register(resp_fd, selectors.EVENT_READ, "resp")
        previous_handlers = self._install_signal_handlers(pending_signals)
        try:
            while True:
                now = time.monotonic()
                if now - last_peer_activity > self.heartbeat_timeout_ms / 1000.0:
                    return build_stopped_message(
                        request_id,
                        reason="broker",
                        code="BROKER_UNAVAILABLE",
                        detail="broker heartbeat lease expired",
                    )

                elapsed_ms = int((now - start_at) * 1000)
                if timeout_ms > 0 and elapsed_ms >= timeout_ms and not sent_timeout_sigterm:
                    self._write_all(
                        req_fd,
                        encode_message(
                            build_data_message(
                                request_id,
                                channel="signal",
                                signal_name="SIGTERM",
                            )
                        ),
                    )
                    last_send_at = time.monotonic()
                    sent_timeout_sigterm = True
                if (
                    sent_timeout_sigterm
                    and not sent_timeout_sigkill
                    and elapsed_ms >= timeout_ms + timeout_grace_ms
                ):
                    self._write_all(
                        req_fd,
                        encode_message(
                            build_data_message(
                                request_id,
                                channel="signal",
                                signal_name="SIGKILL",
                            )
                        ),
                    )
                    last_send_at = time.monotonic()
                    sent_timeout_sigkill = True

                while pending_signals:
                    signal_name = pending_signals.popleft()
                    self._write_all(
                        req_fd,
                        encode_message(
                            build_data_message(
                                request_id,
                                channel="signal",
                                signal_name=signal_name,
                            )
                        ),
                    )
                    last_send_at = time.monotonic()

                if now - last_send_at > self.heartbeat_interval_ms / 1000.0:
                    self._write_all(req_fd, encode_message(build_heartbeat_message(request_id)))
                    last_send_at = time.monotonic()

                timeout = min(
                    0.2,
                    max(
                        0.0,
                        (last_peer_activity + self.heartbeat_timeout_ms / 1000.0) - now,
                    ),
                    max(
                        0.0,
                        (last_send_at + self.heartbeat_interval_ms / 1000.0) - now,
                    ),
                )
                events = selector.select(timeout)
                for key, _mask in events:
                    if key.data == "stdin":
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            if not stdin_eof_sent:
                                self._write_all(
                                    req_fd,
                                    encode_message(
                                        build_data_message(
                                            request_id,
                                            channel="stdin",
                                            data=b"",
                                            eof=True,
                                        )
                                    ),
                                )
                                last_send_at = time.monotonic()
                                stdin_eof_sent = True
                            selector.unregister(key.fd)
                        else:
                            self._write_all(
                                req_fd,
                                encode_message(
                                    build_data_message(
                                        request_id,
                                        channel="stdin",
                                        data=chunk,
                                        eof=False,
                                    )
                                ),
                            )
                            last_send_at = time.monotonic()
                    elif key.data == "resp":
                        try:
                            chunk = os.read(resp_fd, 4096)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            return build_stopped_message(
                                request_id,
                                reason="broker",
                                code="BROKER_UNAVAILABLE",
                                detail="broker closed the response FIFO without sending stopped",
                            )
                        read_buffer += chunk
                        while b"\n" in read_buffer:
                            raw_line, read_buffer = read_buffer.split(b"\n", 1)
                            line = raw_line.strip()
                            if not line:
                                continue
                            try:
                                frame = decode_message(line)
                                validated = validate_broker_message(frame)
                            except (ProtocolError, ValueError) as exc:
                                detail = exc.detail if isinstance(exc, ProtocolError) else str(exc)
                                return build_stopped_message(
                                    request_id,
                                    reason="protocol",
                                    code="BAD_RESPONSE",
                                    detail=detail,
                                )
                            if validated["id"] != request_id:
                                continue
                            last_peer_activity = time.monotonic()
                            kind = validated["kind"]
                            if kind == "heartbeat":
                                continue
                            if kind == "data":
                                channel = validated["channel"]
                                payload = decode_blob(validated["data_b64"])
                                if payload:
                                    if channel == "stdout":
                                        stdout_file.write(payload)
                                        stdout_file.flush()
                                    else:
                                        stderr_file.write(payload)
                                        stderr_file.flush()
                                if validated["eof"]:
                                    if channel == "stdout":
                                        stdout_closed = True
                                    else:
                                        stderr_closed = True
                                continue
                            if kind == "stopped":
                                if stdin_fd is not None and stdin_fd in selector.get_map():
                                    selector.unregister(stdin_fd)
                                return validated
        finally:
            self._restore_signal_handlers(previous_handlers)
            selector.close()

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

    def _install_signal_handlers(
        self,
        pending_signals: deque[str],
    ) -> dict[int, Any]:
        handled = [signal.SIGINT, signal.SIGTERM]
        previous: dict[int, Any] = {}

        def handler(signum, frame) -> None:  # type: ignore[no-untyped-def]
            pending_signals.append(signal.Signals(signum).name)

        for signum in handled:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        return previous

    def _restore_signal_handlers(self, previous_handlers: dict[int, Any]) -> None:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def infer_tool_name(invoked_as: str) -> str:
    return Path(invoked_as).name


def make_missing_session_env_error() -> dict[str, Any]:
    return build_stopped_message(
        "missing-session",
        reason="broker",
        code="BROKER_UNAVAILABLE",
        detail=f"{SESSION_ENV_VAR} is not set",
    )


def _classify_stdin(stdin_file: Any) -> str:
    try:
        fd = stdin_file.fileno()
    except (AttributeError, OSError):
        return "none"
    try:
        if os.isatty(fd):
            return "none"
        mode = os.fstat(fd).st_mode
    except OSError:
        return "none"
    if stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
        return "stream"
    return "drain"
