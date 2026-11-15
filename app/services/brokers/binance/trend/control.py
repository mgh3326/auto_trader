"""Kill-switch alert classification and runner liveness for TREND-TSMOM-v1.

Two small pieces that the CLI and the heartbeat watcher share. Nothing here
sends an order or mutates trading state.

* :func:`tick_failures` turns the executor's event names into alert summaries.
  The kill switch itself lives in ``entry_gate``/``state`` (design §4.4): the
  conflict-week latch refuses new entries with
  ``EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED``. The refusal is not a broker
  error, so it gets its own alert bucket here — the design requires an alert
  when it trips, and this is the only place it is surfaced to the operator.
* :class:`RunnerHeartbeat` / :class:`RunnerLiveness` are the liveness pair.
  ``--loop`` holds a session advisory lock for its whole life and touches that
  connection after every tick; the watcher reads the lock and the backend's
  ``pg_stat_activity.state_change``. A missing lock or an old state change
  means the runner is dead or hung. The watcher only runs SELECTs.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .entry_gate import EntryRefusal

logger = logging.getLogger(__name__)

#: Advisory-lock key of the single ``--loop`` runner. Its presence is the
#: runner's liveness; the CLI refuses a second loop while it is held.
RUNNER_LOCK_KEY = "auto_trader:binance_trend_runner"

_LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
# A one-bigint advisory lock appears in ``pg_locks`` as
# classid = key >> 32, objid = key & 0xFFFFFFFF, objsubid = 1 (PostgreSQL
# "Advisory Locks" docs). This one query is both the heartbeat stamp (any query
# on the pinned connection moves ``state_change``) and the ownership proof.
_LOCK_OWNED_SQL = text(
    """
    SELECT count(*)
      FROM pg_locks
     WHERE pid = pg_backend_pid()
       AND locktype = 'advisory'
       AND granted
       AND objsubid = 1
       AND classid::bigint = ((hashtextextended(:key, 0) >> 32) & 4294967295)
       AND objid::bigint = (hashtextextended(:key, 0) & 4294967295)
    """
)
# The watcher sees the same lock from outside the backend.
_LIVENESS_SQL = text(
    """
    SELECT max(a.state_change) AS last_tick_at
      FROM pg_locks l
      JOIN pg_stat_activity a ON a.pid = l.pid
     WHERE l.locktype = 'advisory'
       AND l.granted
       AND l.objsubid = 1
       AND a.datname = current_database()
       AND l.classid::bigint = ((hashtextextended(:key, 0) >> 32) & 4294967295)
       AND l.objid::bigint = (hashtextextended(:key, 0) & 4294967295)
    """
)

#: Event names (the part before ``:``) that mean a tick needs an operator.
_ABORTED_EVENTS = frozenset({"entry_aborted", "exit_aborted"})
_ALERT_EVENTS = frozenset(
    {
        "blocked",
        "exposure_mismatch",
        "partial_entry_cancel_unconfirmed",
        "partial_entry_exposure_open",
        "partial_entry_stop_breached",
    }
)
_UNCERTAIN_SUFFIX = "_uncertain"
_CONFLICT_WEEK_EVENT = "entry_refused"
_CONFLICT_WEEK_BUCKET = "kill_switch_conflict_week"
_DETAIL_MAX = 120


@dataclass(frozen=True)
class TickFailure:
    """``bucket`` keys the alert episode; ``signature`` is what the operator reads."""

    signature: str
    bucket: str


def _split(event: str) -> tuple[str, str]:
    name, _, detail = str(event).partition(":")
    return name, detail


def classify_event(event: str) -> str | None:
    """Return the alert bucket for one executor event, or ``None`` if healthy.

    Closed vocabulary: every event the executor emits is either listed here,
    ends in ``_uncertain``, or is health. ``entry_refused`` is normal sizing and
    capacity behaviour except for the conflict-week kill switch, which must
    alert (design §4.4/§4.5).
    """
    name, detail = _split(event)
    if name in _ALERT_EVENTS or name in _ABORTED_EVENTS:
        return name
    if name.endswith(_UNCERTAIN_SUFFIX):
        return name
    if name == _CONFLICT_WEEK_EVENT and EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED in (
        detail.split(",")
    ):
        return _CONFLICT_WEEK_BUCKET
    return None


def tick_failures(events: Iterable[object]) -> tuple[TickFailure, ...]:
    """One failure per alert bucket, first occurrence wins (order preserved)."""
    seen: set[str] = set()
    failures: list[TickFailure] = []
    for event in events:
        name = getattr(event, "event", None)
        if not isinstance(name, str):
            continue
        bucket = classify_event(name)
        if bucket is None or bucket in seen:
            continue
        seen.add(bucket)
        detail = getattr(event, "detail", None)
        signature = f"{name}:{detail}" if detail else name
        failures.append(TickFailure(signature[: _DETAIL_MAX + 64], bucket))
    return tuple(failures)


def stop_signature(exc: BaseException) -> str:
    return f"exception:{type(exc).__name__}"


class RunnerHeartbeat:
    """Session-level liveness lock held by the ``--loop`` runner.

    The lock lives on one **checked-out connection** for the runner's whole
    life. A session advisory lock outlives a commit and stays on the physical
    backend, so using a pooled ``AsyncSession`` (which returns its connection
    between transactions) would let unrelated queries refresh the stamp, move
    the stamp to another backend, or leave the lock behind in the pool. This
    object therefore owns an ``AsyncConnection`` and never releases it until
    ``close``.

    ``open`` returns ``False`` when another loop already holds the lock, so a
    second runner cannot be started by accident. ``stamp`` runs one ownership
    query on the pinned connection: it both refreshes ``state_change`` (the
    watcher's heartbeat) and proves this backend still holds the lock. A lost
    connection is reopened once; a lost lock reports ``False`` and the caller
    stops the loop.

    Transactions are committed after each statement so the connection never
    sits ``idle in transaction``; the commit does not release the session lock
    because the same physical connection stays checked out.
    """

    def __init__(self, engine: AsyncEngine, *, key: str = RUNNER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: Any | None = None

    async def open(self) -> bool:
        try:
            conn = await self._engine.connect()
        except Exception:
            logger.exception("trend runner liveness connection unavailable")
            return False
        try:
            got = await conn.scalar(_LOCK_SQL, {"key": self._key})
            await conn.commit()
        except Exception:
            logger.exception("trend runner liveness lock unavailable")
            await self._close_conn(conn)
            return False
        if got is not True:
            await self._close_conn(conn)
            return False
        self._conn = conn
        return True

    async def stamp(self) -> bool:
        """Refresh the heartbeat; ``False`` means the lock is no longer ours."""
        if self._conn is None and not await self.open():
            return False
        owned = await self._ownership()
        if owned is False:
            await self.close()
            return False
        if owned is None:
            # The pinned connection died: reacquire once, then prove ownership.
            await self._close_conn(self._conn)
            self._conn = None
            if not await self.open():
                return False
            owned = await self._ownership()
            if owned is not True:
                await self.close()
                return False
        return True

    async def _ownership(self) -> bool | None:
        """True/False from the ownership proof, ``None`` on a connection error."""
        conn = self._conn
        if conn is None:
            return None
        try:
            owned = await conn.scalar(_LOCK_OWNED_SQL, {"key": self._key})
            await conn.commit()
        except Exception:
            logger.exception("trend runner heartbeat stamp failed")
            return None
        return owned is not None and int(owned) >= 1

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.scalar(_UNLOCK_SQL, {"key": self._key})
            await conn.commit()
        except Exception:
            logger.exception("trend runner heartbeat unlock failed")
        await self._close_conn(conn)

    @staticmethod
    async def _close_conn(conn: Any) -> None:
        try:
            await conn.close()
        except Exception:
            logger.exception("trend runner heartbeat connection close failed")


class RunnerLiveness:
    """Read-only watcher side: when did the runner last touch its connection?"""

    def __init__(
        self, session_factory: Callable[[], AsyncSession], *, key: str = RUNNER_LOCK_KEY
    ) -> None:
        self._factory = session_factory
        self._key = key

    async def last_tick_at(self) -> dt.datetime | None:
        async with self._factory() as db:
            value = await db.scalar(_LIVENESS_SQL, {"key": self._key})
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise ValueError("liveness stamp is not a timestamp")
        return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
