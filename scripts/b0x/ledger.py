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
import time
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
OWNER_RELEASE_LOG_NAME = "owner-releases.jsonl"

# This is an observation owner, not a send-authority owner.  In particular,
# the record emitted below is never read by a gate or a broker adapter.
WRITER_LOCK_RECOVERY_OWNER: Final[str] = "scripts.b0x.ledger.writer_lock"
OWNER_RELEASE_EVENT: Final[str] = "owner_release"
OWNER_RELEASE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "scope_exit",
        "scope_exception",
        "setup_exception",
        "unlock_exception",
        "close_exception",
    }
)
MAX_OWNER_RELEASE_RECORD_BYTES: Final[int] = 2048


class WriterLockUnavailable(RuntimeError):
    """Another B0-X process already holds this lane's writer lock."""


@contextmanager
def writer_lock(*, lane: str, root: Path) -> Iterator[Path]:
    """Hold the exclusive single-writer lock for ``lane`` or fail closed."""

    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / f".{lane}.writer.lock"
    handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    release_reason = "scope_exit"
    claim_acquired_at = 0
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WriterLockUnavailable(
                f"lane {lane!r} writer lock held by another process "
                f"({lock_path}) — B0-X allows exactly one writer per account "
                "(contract §2-3). Refusing to derive."
            ) from exc
        acquired = True
        claim_acquired_at = time.monotonic_ns()
        try:
            os.ftruncate(handle, 0)
            os.write(handle, f"pid={os.getpid()}\n".encode())
        except BaseException:
            release_reason = "setup_exception"
            raise
        try:
            yield lock_path
        except BaseException:
            release_reason = "scope_exception"
            raise
    finally:
        unlock_error: BaseException | None = None
        close_error: BaseException | None = None
        observation_error: BaseException | None = None
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except BaseException as exc:
                release_reason = "unlock_exception"
                unlock_error = exc
        finally:
            try:
                os.close(handle)
            except BaseException as exc:
                release_reason = "close_exception"
                close_error = exc

        if acquired:
            try:
                ObservationLedger(root=root, lane=lane).record_owner_release(
                    monotonic_ts_ns=time.monotonic_ns(),
                    wall_clock_ts=dt.datetime.now(dt.UTC).isoformat(),
                    release_reason=release_reason,
                    lock_correlation={
                        "claim_id": (
                            f"{lane}:pid-{os.getpid()}:acquired-{claim_acquired_at}"
                        ),
                        "lane": lane,
                        "lock_path": str(lock_path),
                    },
                )
            except Exception:
                # Observation is deliberately fail-open with respect to
                # the lock's existing release/close behavior.
                pass
            except BaseException as exc:
                # Preserve interruption semantics, but only after the fd has
                # already been closed and any close failure has been captured.
                observation_error = exc

        if close_error is not None:
            raise close_error
        if unlock_error is not None:
            raise unlock_error
        if observation_error is not None:
            raise observation_error


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

    def record_owner_release(
        self,
        *,
        monotonic_ts_ns: int,
        wall_clock_ts: str,
        release_reason: str,
        lock_correlation: dict[str, str],
    ) -> None:
        """Append one bounded terminal owner-release observation.

        This is intentionally write-only evidence.  The fixed fields and
        closed reason vocabulary prevent an unbounded diagnostic payload, and
        the append-only JSONL shape follows the existing cycle/event journals.
        """

        if not isinstance(monotonic_ts_ns, int) or isinstance(monotonic_ts_ns, bool):
            raise TypeError("owner release monotonic timestamp must be an int")
        if type(wall_clock_ts) is not str or not wall_clock_ts.strip():
            raise TypeError("owner release wall-clock timestamp must be a string")
        try:
            parsed_wall_clock_ts = dt.datetime.fromisoformat(wall_clock_ts)
        except ValueError as exc:
            raise ValueError(
                "owner release wall-clock timestamp must be ISO-8601"
            ) from exc
        if (
            parsed_wall_clock_ts.tzinfo is None
            or parsed_wall_clock_ts.utcoffset() is None
        ):
            raise ValueError(
                "owner release wall-clock timestamp must include a timezone"
            )
        if release_reason not in OWNER_RELEASE_REASONS:
            raise ValueError("owner release reason is outside the closed vocabulary")
        required_correlation_keys = {"claim_id", "lane", "lock_path"}
        if set(lock_correlation) != required_correlation_keys or not all(
            isinstance(value, str) and value for value in lock_correlation.values()
        ):
            raise ValueError("owner release lock correlation is not canonical")
        record = {
            "event": OWNER_RELEASE_EVENT,
            "owner": WRITER_LOCK_RECOVERY_OWNER,
            "monotonic_ts_ns": monotonic_ts_ns,
            "wall_clock_ts": wall_clock_ts,
            "release_reason": release_reason,
            "lock_correlation": dict(lock_correlation),
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if len(line.encode("utf-8")) > MAX_OWNER_RELEASE_RECORD_BYTES:
            raise ValueError("owner release observation exceeds its bounded size")
        self._append(OWNER_RELEASE_LOG_NAME, record)

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
    "OWNER_RELEASE_LOG_NAME",
    "WRITER_LOCK_RECOVERY_OWNER",
    "OWNER_RELEASE_EVENT",
    "OWNER_RELEASE_REASONS",
    "MAX_OWNER_RELEASE_RECORD_BYTES",
    "WriterLockUnavailable",
    "writer_lock",
    "ObservationLedger",
    "load_json_state",
    "store_json_state",
]
