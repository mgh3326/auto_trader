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
  The two facts are kept apart on purpose: **lock ownership** is a state of one
  pinned connection (mutual exclusion), and **heartbeat** is a benign query on
  that same connection (the watcher reads its ``state_change``). A failed
  observation is not evidence that the lock was lost, so a transient statement
  error never disposes the connection and never kills the loop; only a proven
  dead backend (the lock is gone with it) does. A connection whose unlock fails
  is invalidated, never returned to the pool while a session lock could ride on
  it. ``open`` reports acquired / held-by-another / database-unavailable as
  three distinct outcomes. The watcher only runs SELECTs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .entry_gate import EntryRefusal

logger = logging.getLogger(__name__)

#: Advisory-lock key of the single ``--loop`` runner. Its presence is the
#: runner's liveness; the CLI refuses a second loop while it is held.
RUNNER_LOCK_KEY = "auto_trader:binance_trend_runner"

_LOCK_SQL = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
_UNLOCK_SQL = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
# The heartbeat is deliberately not an ownership test: any query on the pinned
# connection moves ``pg_stat_activity.state_change`` (what the watcher reads),
# and a failure only means the heartbeat was missed, not that the lock was lost.
_STAMP_SQL = text("SELECT 1")
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
        "entry_conflict_blocked",
        "entry_conflict_rejected",
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

    A conflict record itself also alerts. The week latch trips on the record
    that reaches the limit, and that tick emits ``entry_conflict_blocked`` /
    ``entry_conflict_rejected`` — not ``entry_refused``. Alerting only on the
    later refusal would hide the moment the permanent latch tripped.
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


class LivenessOpen(StrEnum):
    """Three distinct outcomes; only ``HELD_BY_OTHER`` means a competing runner."""

    ACQUIRED = "acquired"
    HELD_BY_OTHER = "held_by_other"
    UNAVAILABLE = "unavailable"


def _connection_lost(exc: BaseException) -> bool:
    """True only when the backend is known gone (so its session lock is gone too).

    A statement timeout, a cancelled statement or any other error on a live
    backend proves nothing about the lock and must not dispose the connection.
    ``TimeoutError``/``asyncio.TimeoutError`` are deliberately NOT treated as
    dead: asyncpg's ``command_timeout`` and caller-side ``wait_for`` raise them
    while the connection stays healthy. Only a builtin ``ConnectionError``
    (reset/refused/aborted/broken pipe) or a DBAPI error SQLAlchemy marked
    ``connection_invalidated`` proves the backend is gone.
    """
    if isinstance(exc, ConnectionError):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


class RunnerHeartbeat:
    """Session-level liveness lock held by the ``--loop`` runner.

    One **checked-out connection** owns the lock for the runner's whole life.
    A session advisory lock outlives a commit and stays on the physical backend,
    so this object never returns that connection to the pool while it may still
    hold the lock; disposal always invalidates a backend whose unlock failed.

    The two concepts are separate:

    * ``open`` is the only ownership decision. It reports acquired,
      held-by-another or database-unavailable, so the caller never blames a
      phantom runner for an outage.
    * ``stamp`` is only the heartbeat: a benign ``SELECT 1`` on the pinned
      connection that moves ``pg_stat_activity.state_change``. A transient
      statement error is logged and the loop continues (the next tick retries;
      if stamps keep failing the watcher raises the stale alert). ``False`` is
      returned only when the connection is proven dead, which means the
      backend — and therefore the lock — is gone; the caller must stop.

    Transactions are committed after each statement so the connection never
    sits ``idle in transaction``.
    """

    def __init__(self, engine: AsyncEngine, *, key: str = RUNNER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: Any | None = None

    async def open(self) -> LivenessOpen:
        try:
            conn = await self._engine.connect()
        except Exception:
            logger.exception("trend runner liveness database unavailable")
            return LivenessOpen.UNAVAILABLE
        try:
            got = await conn.scalar(_LOCK_SQL, {"key": self._key})
            await conn.commit()
        except Exception:
            logger.exception("trend runner liveness lock query failed")
            await self._discard(conn)
            return LivenessOpen.UNAVAILABLE
        if got is not True:
            await self._discard(conn)
            return LivenessOpen.HELD_BY_OTHER
        self._conn = conn
        return LivenessOpen.ACQUIRED

    async def stamp(self) -> bool:
        """Refresh the heartbeat. ``False`` only when the backend is proven dead."""
        conn = self._conn
        if conn is None:
            return False
        try:
            await conn.scalar(_STAMP_SQL)
            await conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _connection_lost(exc):
                logger.exception("trend runner liveness connection lost")
                self._conn = None
                await self._discard(conn)
                return False
            logger.warning(
                "trend runner heartbeat stamp deferred error_class=%s",
                type(exc).__name__,
            )
            # The failed statement left the transaction aborted. Without this
            # rollback every later stamp would fail (SQLAlchemy
            # PendingRollbackError / "current transaction is aborted") and the
            # heartbeat would silently stop while this method kept saying True.
            if not await self._recover(conn):
                return False
            return True
        return True

    async def _recover(self, conn: Any) -> bool:
        """End the aborted transaction; ``False`` only if the backend is gone."""
        try:
            await conn.rollback()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _connection_lost(exc):
                logger.exception("trend runner liveness connection lost in rollback")
                self._conn = None
                await self._discard(conn)
                return False
            logger.warning(
                "trend runner heartbeat rollback deferred error_class=%s",
                type(exc).__name__,
            )
        return True

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            # A stamp may have left an aborted transaction open; end it so the
            # unlock is not refused with "current transaction is aborted".
            await conn.rollback()
        except Exception:
            logger.warning("trend runner heartbeat rollback before unlock failed")
        try:
            await conn.scalar(_UNLOCK_SQL, {"key": self._key})
            await conn.commit()
        except Exception:
            logger.exception("trend runner heartbeat unlock failed; invalidating")
            await self._discard(conn)
            return
        try:
            await conn.close()
        except Exception:
            logger.exception("trend runner heartbeat connection close failed")

    @staticmethod
    async def _discard(conn: Any) -> None:
        """Kill the backend so a session lock can never ride a pooled connection."""
        try:
            await conn.invalidate()
        except Exception:
            logger.exception("trend runner heartbeat connection invalidate failed")
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
