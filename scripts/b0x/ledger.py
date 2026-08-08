"""B0-X observation ledger + writer-singleton lock.

Contract §2-3: *writer = 1 — 계좌당 주문 생성 주체는 B0-X 어댑터 하나뿐.*

Two distinct things enforce that, and they are not interchangeable:

  * :func:`writer_lock` stops **two B0-X processes** from deriving against the
    same lane concurrently. An ``flock``-based exclusive lock, held for the
    whole cycle, non-blocking — a second process fails closed immediately
    rather than queueing behind the first and acting on stale state.
  * ``foreign_*`` fields on :class:`~scripts.b0x.state.LaneAccountState` carry
    the **other** direction: venue state B0-X did not create. That is what
    marks a cycle ``CONTAMINATED``.

Records are append-only JSONL. Nothing in this module ever rewrites or deletes
a prior line: an observation log that can be edited in place is not evidence.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Default artifact root. Deliberately NOT inside
#: ``~/services/auto_trader-operator`` — that repo is PR-only and already
#: tracks ``policy-tables/``; a cycle writing there would dirty an operator
#: worktree on every run.
DEFAULT_OBSERVATION_DIR: Final[Path] = Path.home() / "work" / "herdr-artifacts" / "b0x"

CYCLE_LOG_NAME = "cycles.jsonl"
NOTICE_LOG_NAME = "operator-notices.jsonl"


class WriterLockUnavailable(RuntimeError):
    """Another B0-X process already holds this lane's writer lock."""


@contextmanager
def writer_lock(*, lane: str, root: Path) -> Iterator[Path]:
    """Hold the exclusive single-writer lock for ``lane`` or fail closed."""

    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{lane}.writer.lock"
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WriterLockUnavailable(
                f"lane {lane!r} writer lock held by another process "
                f"({lock_path}) — B0-X allows exactly one writer per account "
                "(contract §2-3). Refusing to derive."
            ) from exc
        os.ftruncate(handle, 0)
        os.write(handle, f"pid={os.getpid()}\n".encode())
        yield lock_path
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


@dataclass(frozen=True, slots=True)
class ObservationLedger:
    """Append-only JSONL writer, one directory per lane."""

    lane: str
    root: Path

    @property
    def lane_dir(self) -> Path:
        return self.root / self.lane

    def ensure(self) -> None:
        self.lane_dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, record: dict[str, Any]) -> None:
        self.ensure()
        line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
        with (self.lane_dir / name).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def record_cycle(self, record: dict[str, Any]) -> None:
        self._append(CYCLE_LOG_NAME, record)

    def record_notice(self, *, at: dt.datetime, text: str, **extra: Any) -> None:
        self._append(NOTICE_LOG_NAME, {"at": at.isoformat(), "notice": text, **extra})

    def write_artifact(self, *, name: str, content: str) -> Path:
        self.ensure()
        path = self.lane_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def read_cycles(self) -> list[dict[str, Any]]:
        path = self.lane_dir / CYCLE_LOG_NAME
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records


def load_json_state(path: Path) -> dict[str, Any] | None:
    path = Path(path).expanduser()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def store_json_state(path: Path, payload: dict[str, Any]) -> None:
    """Atomic replace so a crash mid-write cannot truncate lane state."""

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


__all__ = [
    "DEFAULT_OBSERVATION_DIR",
    "CYCLE_LOG_NAME",
    "NOTICE_LOG_NAME",
    "WriterLockUnavailable",
    "writer_lock",
    "ObservationLedger",
    "load_json_state",
    "store_json_state",
]
