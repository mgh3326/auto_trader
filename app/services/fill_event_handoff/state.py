"""Locked JSON state for the fill-event handoff poller."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any


class HandoffState:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "state.json"
        self._lock: Any | None = None
        self.data: dict[str, Any] = {}

    def __enter__(self) -> HandoffState:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = (self.directory / "poller.lock").open("a+", encoding="utf-8")
        fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX)
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("fill handoff state must be an object")
            self.data = loaded
        self.data.setdefault("version", 1)
        self.data.setdefault("watermark", 0)
        self.data.setdefault("seen", {})
        self.data.setdefault("cooldowns", {})
        return self

    def save(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def __exit__(self, *_: object) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
