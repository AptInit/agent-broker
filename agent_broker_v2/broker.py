from __future__ import annotations

import os
import select
import selectors
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_broker_v2.protocol import (
    HEARTBEAT_INTERVAL_MS,
    HEARTBEAT_TIMEOUT_MS,
    TRANSPORT_CHUNK_SIZE,
    build_broker_stop,
    build_client_stop,
    build_data_message,
    build_heartbeat_message,
    build_protocol_stop,
    build_stopped_message,
    decode_blob,
    decode_message,
    encode_message,
    validate_client_data_message,
    validate_client_message,
    validate_start_message,
    ProtocolError,
)
from agent_broker_v2.session import SessionPaths


@dataclass(frozen=True)
class ToolSpec:
    executable: str


@dataclass
class ActiveRequest:
    request_id: str
    child: subprocess.Popen[bytes]
    stdout_log: Any
    stderr_log: Any
    stdout_open: bool = True
    stderr_open: bool = True
    stdin_closed: bool = False
    child_exited: bool = False
    terminal_message: dict[str, Any] | None = None
    pending_stdin: deque[bytes] | None = None
    close_stdin_when_flushed: bool = False

    def __post_init__(self) -> None:
        if self.pending_stdin is None:
            self.pending_stdin = deque()


class Broker:
    def __init__(
        self,
        paths: SessionPaths,
        allowed_tools: dict[str, ToolSpec],
        *,
        heartbeat_interval_ms: int = HEARTBEAT_INTERVAL_MS,
        heartbeat_timeout_ms: int = HEARTBEAT_TIMEOUT_MS,
    ) -> None:
        self.paths = paths
        self.allowed_tools = allowed_tools
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.heartbeat_timeout_ms = heartbeat_timeout_ms
        self._request_buffer = b""

    def serve_until(self, stop_event: threading.Event) -> None:
        req_fd = self._open_request_fifo()
        resp_fd = self._open_response_fifo()
        try:
            while not stop_event.is_set():
                frame, req_fd = self._read_next_idle_frame(req_fd, stop_event)
                if frame is None:
                    continue
                try:
                    validate_start_message(frame)
                except ProtocolError:
                    request_id = frame.get("id")
                    if isinstance(request_id, str) and request_id:
                        self._send_message(
                            resp_fd,
                            build_protocol_stop(
                                request_id,
                                code="EXPECTED_START",
                                detail="idle broker expects a start frame",
                            ),
                        )
                    continue

                self._serve_request(req_fd, resp_fd, frame, stop_event)
        finally:
            os.close(req_fd)
            os.close(resp_fd)

    def _open_request_fifo(self) -> int:
        return os.open(self.paths.req_fifo, os.O_RDONLY | os.O_NONBLOCK)

    def _open_response_fifo(self) -> int:
        resp_fd = os.open(self.paths.resp_fifo, os.O_RDWR | os.O_NONBLOCK)
        os.set_blocking(resp_fd, True)
        return resp_fd

    def _read_next_idle_frame(
        self,
        req_fd: int,
        stop_event: threading.Event,
    ) -> tuple[dict[str, Any] | None, int]:
        while not stop_event.is_set():
            try:
                frame = self._pop_buffered_frame()
            except Exception:
                continue
            if frame is not None:
                try:
                    return validate_client_message(frame), req_fd
                except ProtocolError:
                    return frame, req_fd

            readable, _, _ = select.select([req_fd], [], [], 0.2)
            if not readable:
                return None, req_fd
            chunk = os.read(req_fd, 4096)
            if not chunk:
                self._request_buffer = b""
                os.close(req_fd)
                req_fd = self._open_request_fifo()
                continue
            self._request_buffer += chunk
        return None, req_fd

    def _serve_request(
        self,
        req_fd: int,
        resp_fd: int,
        start_message: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        request_id = start_message["id"]
        tool = start_message["tool"]
        argv = list(start_message["argv"])
        cwd = str(Path(start_message["cwd"]).resolve())
        env_delta = dict(start_message["env_delta"])

        tool_spec = self.allowed_tools.get(tool)
        if tool_spec is None:
            self._send_message(
                resp_fd,
                build_broker_stop(
                    request_id,
                    code="NOT_ALLOWED",
                    detail=f"tool {tool!r} is not in the broker allowlist",
                ),
            )
            return

        env = os.environ.copy()
        env.update(env_delta)
        command = [tool_spec.executable, *argv]
        stdout_log = self.paths.logs_dir / f"{request_id}.stdout.log"
        stderr_log = self.paths.logs_dir / f"{request_id}.stderr.log"

        try:
            child = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._send_message(
                resp_fd,
                build_broker_stop(request_id, code="TOOL_NOT_FOUND", detail=str(exc)),
            )
            return
        except Exception as exc:
            self._send_message(
                resp_fd,
                build_broker_stop(request_id, code="SPAWN_FAILED", detail=repr(exc)),
            )
            return

        assert child.stdin is not None
        assert child.stdout is not None
        assert child.stderr is not None
        os.set_blocking(child.stdin.fileno(), False)
        os.set_blocking(child.stdout.fileno(), False)
        os.set_blocking(child.stderr.fileno(), False)

        active = ActiveRequest(
            request_id=request_id,
            child=child,
            stdout_log=stdout_log.open("wb"),
            stderr_log=stderr_log.open("wb"),
        )

        selector = selectors.DefaultSelector()
        selector.register(req_fd, selectors.EVENT_READ, "req")
        selector.register(child.stdout.fileno(), selectors.EVENT_READ, "stdout")
        selector.register(child.stderr.fileno(), selectors.EVENT_READ, "stderr")

        last_peer_activity = time.monotonic()
        last_send_at = time.monotonic()

        try:
            while True:
                now = time.monotonic()
                child_stdin_fd = None
                if child.stdin is not None and not child.stdin.closed:
                    child_stdin_fd = child.stdin.fileno()
                if stop_event.is_set() and active.terminal_message is None:
                    active.terminal_message = build_broker_stop(
                        request_id,
                        code="BROKER_SHUTDOWN",
                        detail="broker shutdown requested",
                    )
                    self._request_child_signal(active, signal.SIGTERM)

                if (
                    active.terminal_message is None
                    and now - last_peer_activity > self.heartbeat_timeout_ms / 1000.0
                ):
                    active.terminal_message = build_client_stop(
                        request_id,
                        code="CLIENT_LOST",
                        detail="heartbeat lease expired",
                    )
                    self._request_child_signal(active, signal.SIGTERM)

                if active.child.poll() is not None:
                    active.child_exited = True

                try:
                    buffered_frames = self._drain_buffered_request_frames()
                except ProtocolError as exc:
                    buffered_frames = []
                    if active.terminal_message is None:
                        active.terminal_message = build_protocol_stop(
                            request_id,
                            code=exc.code,
                            detail=exc.detail,
                        )
                        self._request_child_signal(active, signal.SIGTERM)
                else:
                    if buffered_frames:
                        last_peer_activity = self._process_client_frames(
                            active=active,
                            request_id=request_id,
                            request_frames=buffered_frames,
                            last_peer_activity=last_peer_activity,
                        )

                if active.child_exited and not active.stdout_open and not active.stderr_open:
                    if active.terminal_message is None:
                        active.terminal_message = self._build_terminal_message(active)
                    self._send_message(resp_fd, active.terminal_message)
                    break

                if active.terminal_message is not None and active.child_exited:
                    if active.stdout_open or active.stderr_open:
                        pass
                    else:
                        self._send_message(resp_fd, active.terminal_message)
                        break

                if active.pending_stdin and child_stdin_fd is not None:
                    if child_stdin_fd not in selector.get_map():
                        selector.register(child_stdin_fd, selectors.EVENT_WRITE, "stdin_write")
                elif child_stdin_fd is not None and child_stdin_fd in selector.get_map():
                    selector.unregister(child_stdin_fd)
                    if active.close_stdin_when_flushed and not active.stdin_closed:
                        child.stdin.close()
                        active.stdin_closed = True

                if now - last_send_at > self.heartbeat_interval_ms / 1000.0:
                    self._send_message(resp_fd, build_heartbeat_message(request_id))
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
                    if key.data == "req":
                        try:
                            request_frames, request_open = self._read_request_frames(req_fd)
                        except ProtocolError as exc:
                            request_frames = []
                            request_open = True
                            if active.terminal_message is None:
                                active.terminal_message = build_protocol_stop(
                                    request_id,
                                    code=exc.code,
                                    detail=exc.detail,
                                )
                                self._request_child_signal(active, signal.SIGTERM)
                        if not request_open and active.terminal_message is None:
                            active.terminal_message = build_client_stop(
                                request_id,
                                code="CLIENT_LOST",
                                detail="request FIFO closed while request was active",
                            )
                            self._request_child_signal(active, signal.SIGTERM)
                        last_peer_activity = self._process_client_frames(
                            active=active,
                            request_id=request_id,
                            request_frames=request_frames,
                            last_peer_activity=last_peer_activity,
                        )
                    elif key.data == "stdout":
                        self._read_child_output(
                            resp_fd,
                            active,
                            channel="stdout",
                        )
                        last_send_at = time.monotonic()
                    elif key.data == "stderr":
                        self._read_child_output(
                            resp_fd,
                            active,
                            channel="stderr",
                        )
                        last_send_at = time.monotonic()
                    elif key.data == "stdin_write":
                        self._flush_child_stdin(active, selector)
        finally:
            selector.close()
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.stdout and not child.stdout.closed:
                child.stdout.close()
            if child.stderr and not child.stderr.closed:
                child.stderr.close()
            active.stdout_log.close()
            active.stderr_log.close()
            if active.child.poll() is None:
                active.child.kill()
                try:
                    active.child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def _flush_child_stdin(self, active: ActiveRequest, selector: selectors.BaseSelector) -> None:
        child_stdin = active.child.stdin
        assert child_stdin is not None
        while active.pending_stdin:
            view = memoryview(active.pending_stdin[0])
            try:
                written = os.write(child_stdin.fileno(), view)
            except BlockingIOError:
                return
            except BrokenPipeError:
                active.pending_stdin.clear()
                active.close_stdin_when_flushed = True
                break
            if written == len(view):
                active.pending_stdin.popleft()
            else:
                active.pending_stdin[0] = bytes(view[written:])
                return

        if not active.pending_stdin:
            if child_stdin.fileno() in selector.get_map():
                selector.unregister(child_stdin.fileno())
            if active.close_stdin_when_flushed and not active.stdin_closed:
                child_stdin.close()
                active.stdin_closed = True

    def _handle_client_data(self, active: ActiveRequest, message: dict[str, Any]) -> None:
        if message["kind"] != "data":
            return
        channel = message["channel"]
        if channel == "stdin":
            if active.close_stdin_when_flushed or active.stdin_closed:
                raise ProtocolError("BAD_CHANNEL_STATE", "stdin is already closed")
            data = decode_blob(message["data_b64"])
            if data:
                active.pending_stdin.append(data)
            if message["eof"]:
                active.close_stdin_when_flushed = True
            return

        if channel == "signal":
            signum = signal_name_to_number(message["signal"])
            self._request_child_signal(active, signum)
            return

        raise ProtocolError("BAD_CHANNEL", "unexpected data channel")

    def _request_child_signal(self, active: ActiveRequest, signum: int) -> None:
        if active.child.poll() is not None:
            return
        try:
            active.child.send_signal(signum)
        except ProcessLookupError:
            return

    def _read_child_output(self, resp_fd: int, active: ActiveRequest, *, channel: str) -> None:
        pipe = active.child.stdout if channel == "stdout" else active.child.stderr
        log_file = active.stdout_log if channel == "stdout" else active.stderr_log
        assert pipe is not None
        try:
            chunk = os.read(pipe.fileno(), 4096)
        except BlockingIOError:
            return
        if not chunk:
            if channel == "stdout" and active.stdout_open:
                active.stdout_open = False
                self._send_message(
                    resp_fd,
                    build_data_message(active.request_id, channel=channel, data=b"", eof=True),
                )
            elif channel == "stderr" and active.stderr_open:
                active.stderr_open = False
                self._send_message(
                    resp_fd,
                    build_data_message(active.request_id, channel=channel, data=b"", eof=True),
                )
            return
        log_file.write(chunk)
        log_file.flush()
        self._send_message(
            resp_fd,
            build_data_message(active.request_id, channel=channel, data=chunk, eof=False),
        )

    def _build_terminal_message(self, active: ActiveRequest) -> dict[str, Any]:
        returncode = active.child.returncode
        assert returncode is not None
        if returncode < 0:
            return build_stopped_message(
                active.request_id,
                reason="signal",
                signal=signal.Signals(-returncode).name,
            )
        return build_stopped_message(
            active.request_id,
            reason="exit",
            exit_code=returncode,
        )

    def _read_request_frames(self, req_fd: int) -> tuple[list[dict[str, Any]], bool]:
        try:
            chunk = os.read(req_fd, 4096)
        except BlockingIOError:
            return [], True
        if not chunk:
            self._request_buffer = b""
            return [], False
        self._request_buffer += chunk
        frames: list[dict[str, Any]] = []
        while True:
            try:
                frame = self._pop_buffered_frame()
            except Exception as exc:
                raise ProtocolError("BAD_FRAME", str(exc)) from exc
            if frame is None:
                break
            frames.append(frame)
        return frames, True

    def _drain_buffered_request_frames(self) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        while True:
            try:
                frame = self._pop_buffered_frame()
            except Exception as exc:
                raise ProtocolError("BAD_FRAME", str(exc)) from exc
            if frame is None:
                break
            frames.append(frame)
        return frames

    def _process_client_frames(
        self,
        *,
        active: ActiveRequest,
        request_id: str,
        request_frames: list[dict[str, Any]],
        last_peer_activity: float,
    ) -> float:
        for frame in request_frames:
            if frame.get("id") != request_id:
                continue
            try:
                validated = validate_client_message(frame)
            except ProtocolError as exc:
                if active.terminal_message is None:
                    active.terminal_message = build_protocol_stop(
                        request_id,
                        code=exc.code,
                        detail=exc.detail,
                    )
                    self._request_child_signal(active, signal.SIGTERM)
                continue
            last_peer_activity = time.monotonic()
            if validated["kind"] == "heartbeat":
                continue
            try:
                self._handle_client_data(active, validated)
            except ProtocolError as exc:
                if active.terminal_message is None:
                    active.terminal_message = build_protocol_stop(
                        request_id,
                        code=exc.code,
                        detail=exc.detail,
                    )
                    self._request_child_signal(active, signal.SIGTERM)
        return last_peer_activity

    def _pop_buffered_frame(self) -> dict[str, Any] | None:
        if b"\n" not in self._request_buffer:
            return None
        raw_line, self._request_buffer = self._request_buffer.split(b"\n", 1)
        line = raw_line.strip()
        if not line:
            return None
        return decode_message(line)

    def _send_message(self, resp_fd: int, message: dict[str, Any]) -> None:
        encoded = encode_message(message)
        for offset in range(0, len(encoded), TRANSPORT_CHUNK_SIZE):
            self._write_all(resp_fd, encoded[offset : offset + TRANSPORT_CHUNK_SIZE])

    def _write_all(self, fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short write to shim transport")
            view = view[written:]


def signal_name_to_number(signal_name: str) -> int:
    try:
        return int(getattr(signal, signal_name))
    except AttributeError as exc:
        raise ProtocolError("BAD_SIGNAL", f"unknown signal {signal_name!r}") from exc
