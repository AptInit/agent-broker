from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


SESSION_ENV_VAR = "BROKER_SESSION_DIR"


@dataclass(frozen=True)
class SessionPaths:
    session_dir: Path
    req_fifo: Path
    resp_fifo: Path
    client_lock: Path
    logs_dir: Path
    shim_bin_dir: Path

    @classmethod
    def from_dir(cls, session_dir: str | Path) -> "SessionPaths":
        root = Path(session_dir).resolve()
        return cls(
            session_dir=root,
            req_fifo=root / "req.fifo",
            resp_fifo=root / "resp.fifo",
            client_lock=root / "client.lock",
            logs_dir=root / "logs",
            shim_bin_dir=root / "bin",
        )

    @classmethod
    def from_env(cls) -> "SessionPaths":
        session_dir = os.environ[SESSION_ENV_VAR]
        return cls.from_dir(session_dir)
