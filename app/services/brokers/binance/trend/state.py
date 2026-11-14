"""Trend lane service layer: durable signal, exposure reservation, send fence.

This module is the only writer of ``review.binance_trend_signals`` and
``review.binance_trend_intents``. Every mutation commits in its own transaction
under one lane advisory lock. A committed ``sending`` intent is never dispatched
again after a restart: nothing here moves an intent back to ``reserved`` or
``sending``, and ``uncertain`` intents block new entries on their symbol.

There is no broker access, order submission or execution client in this
package yet (CT-4 adds the executor); resolving an ``uncertain`` intent needs
positive broker evidence and is also CT-4.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_trend import BinanceTrendIntent, BinanceTrendSignal

from .constants import LOCKED_LIMITS, TrendLimits, assert_limits_locked
from .entry_gate import (
    ACTIVE_SIGNAL_STATES,
    CONFLICT_SIGNAL_STATES,
    ActiveSlot,
    EntryBook,
    EntryDecision,
    conflict_stop_tripped,
    decide_entry,
    max_consecutive_hard_stops,
    utc_day_start,
)
from .keys import SIDES, make_signal_key, trend_client_order_id, trend_correlation_id

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_LOCK_KEY = "auto_trader:binance_trend_state"
_ACTIVE_INTENTS = ("reserved", "sending", "acknowledged", "evidenced", "uncertain")
_SENT_INTENTS = ("sending", "acknowledged", "evidenced", "uncertain")
_MAX_DETAIL = 200


class TrendStateBlocked(RuntimeError):
    """A trend safety gate refused an entry or state transition."""


class TrendEntryRefused(TrendStateBlocked):
    """The entry gate refused; ``reasons`` lists every tripped reason."""

    def __init__(self, decision: EntryDecision) -> None:
        self.reasons = decision.reasons
        super().__init__(";".join(decision.reasons))


@dataclass(frozen=True)
class TrendSignalSnapshot:
    signal_key: str
    correlation_id: str
    symbol: str
    side: str
    decision_ts: int
    signal_price_text: str
    state: str
    entry_notional_usdt: Decimal | None
    conflict_at: dt.datetime | None
    conflict_detail: str | None
    exit_reason: str | None
    exit_at: dt.datetime | None


@dataclass(frozen=True)
class TrendIntentSnapshot:
    client_order_id: str
    signal_key: str
    leg_key: str
    side: str
    qty: Decimal
    reduce_only: bool
    state: str
    broker_order_id: str | None
    broker_status: str | None
    executed_qty: Decimal


def _signal_snapshot(row: BinanceTrendSignal) -> TrendSignalSnapshot:
    return TrendSignalSnapshot(
        signal_key=row.signal_key,
        correlation_id=row.correlation_id,
        symbol=row.symbol,
        side=row.side,
        decision_ts=row.decision_ts,
        signal_price_text=row.signal_price_text,
        state=row.state,
        entry_notional_usdt=row.entry_notional_usdt,
        conflict_at=row.conflict_at,
        conflict_detail=row.conflict_detail,
        exit_reason=row.exit_reason,
        exit_at=row.exit_at,
    )


def _intent_snapshot(row: BinanceTrendIntent) -> TrendIntentSnapshot:
    return TrendIntentSnapshot(
        client_order_id=row.client_order_id,
        signal_key=row.signal_key,
        leg_key=row.leg_key,
        side=row.side,
        qty=row.qty,
        reduce_only=row.reduce_only,
        state=row.state,
        broker_order_id=row.broker_order_id,
        broker_status=row.broker_status,
        executed_qty=row.executed_qty,
    )


_SCALE = Decimal("1e-12")


def _stored_exactly(value: Decimal) -> bool:
    """True when NUMERIC(28, 12) stores ``value`` unchanged (no rounding)."""
    if not value.is_finite():
        return False
    with localcontext() as ctx:
        ctx.prec = 60
        try:
            return value.quantize(_SCALE) == value and abs(value) < Decimal(10) ** 16
        except InvalidOperation:
            return False


def _require_aware(now: dt.datetime) -> None:
    if now.tzinfo is None:
        raise TrendStateBlocked("aware timestamp required")


class TrendStateService:
    """Each mutation commits in its own transaction before any broker send."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        limits: TrendLimits = LOCKED_LIMITS,
    ) -> None:
        # Fails closed before any DB use if the envelope was widened.
        assert_limits_locked(limits)
        self._factory = session_factory
        self._limits = limits

    @staticmethod
    async def _lock(db: AsyncSession) -> None:
        await db.execute(_LOCK_SQL, {"key": _LOCK_KEY})

    async def observe_signal(
        self, *, symbol: str, side: str, decision_ts: int, price_text: str
    ) -> TrendSignalSnapshot:
        if symbol not in self._limits.universe or side not in SIDES:
            raise TrendStateBlocked("non-trend signal refused")
        try:
            key = make_signal_key(symbol, decision_ts, side, price_text)
        except ValueError as exc:
            raise TrendStateBlocked(str(exc)) from exc
        async with self._factory() as db, db.begin():
            await self._lock(db)
            await db.execute(
                pg_insert(BinanceTrendSignal)
                .values(
                    signal_key=key,
                    correlation_id=trend_correlation_id(key),
                    symbol=symbol,
                    side=side,
                    decision_ts=decision_ts,
                    signal_price_text=price_text,
                    state="observed",
                    entry_qty=0,
                    closed_qty=0,
                    realized_pnl_usdt=0,
                    fees_usdt=0,
                )
                .on_conflict_do_nothing(index_elements=["signal_key"])
            )
            row = await db.get(BinanceTrendSignal, key)
            if row is None:
                raise TrendStateBlocked("signal row missing after insert")
            if (
                row.symbol != symbol
                or row.side != side
                or row.decision_ts != decision_ts
                or row.signal_price_text != price_text
            ):
                raise TrendStateBlocked("signal key collision")
            return _signal_snapshot(row)

    async def get_signal(self, signal_key: str) -> TrendSignalSnapshot | None:
        async with self._factory() as db:
            row = await db.get(BinanceTrendSignal, signal_key)
            return _signal_snapshot(row) if row else None

    async def list_active_signals(self) -> tuple[TrendSignalSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceTrendSignal).where(
                        BinanceTrendSignal.state.in_(ACTIVE_SIGNAL_STATES)
                    )
                )
            ).all()
            return tuple(_signal_snapshot(row) for row in rows)

    async def list_intents(self, signal_key: str) -> tuple[TrendIntentSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceTrendIntent)
                    .where(BinanceTrendIntent.signal_key == signal_key)
                    .order_by(BinanceTrendIntent.created_at)
                )
            ).all()
            return tuple(_intent_snapshot(row) for row in rows)

    async def list_unresolved_intents(self) -> tuple[TrendIntentSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceTrendIntent).where(
                        BinanceTrendIntent.state.in_(_ACTIVE_INTENTS)
                    )
                )
            ).all()
            return tuple(_intent_snapshot(row) for row in rows)

    async def _load_book(
        self,
        db: AsyncSession,
        now: dt.datetime,
        *,
        exclude_signal_key: str | None = None,
    ) -> EntryBook:
        """Read the durable book; the caller holds the lane lock.

        ``exclude_signal_key`` leaves one signal's own reservation out so the
        send fence can re-judge it without counting it against itself.
        """
        active_query = select(
            BinanceTrendSignal.symbol, BinanceTrendSignal.entry_notional_usdt
        ).where(BinanceTrendSignal.state.in_(ACTIVE_SIGNAL_STATES))
        if exclude_signal_key is not None:
            active_query = active_query.where(
                BinanceTrendSignal.signal_key != exclude_signal_key
            )
        active_rows = (await db.execute(active_query)).all()
        uncertain_symbols = frozenset(
            (
                await db.scalars(
                    select(BinanceTrendSignal.symbol)
                    .join(
                        BinanceTrendIntent,
                        BinanceTrendIntent.signal_key == BinanceTrendSignal.signal_key,
                    )
                    .where(BinanceTrendIntent.state == "uncertain")
                )
            ).all()
        )
        day_start = utc_day_start(now)
        exits = (
            await db.scalars(
                select(BinanceTrendSignal.exit_reason)
                .where(
                    BinanceTrendSignal.state == "closed",
                    BinanceTrendSignal.exit_at >= day_start,
                    BinanceTrendSignal.exit_at < day_start + dt.timedelta(days=1),
                )
                .order_by(BinanceTrendSignal.exit_at.asc())
            )
        ).all()
        conflict_times = (
            await db.scalars(
                select(BinanceTrendSignal.conflict_at).where(
                    BinanceTrendSignal.state.in_(CONFLICT_SIGNAL_STATES),
                    BinanceTrendSignal.conflict_resolved_at.is_(None),
                )
            )
        ).all()
        return EntryBook(
            active=tuple(
                ActiveSlot(symbol=symbol, notional_usdt=notional or Decimal(0))
                for symbol, notional in active_rows
            ),
            uncertain_symbols=uncertain_symbols,
            max_consecutive_hard_stops_today=max_consecutive_hard_stops(exits),
            conflict_stop_tripped=conflict_stop_tripped(
                [moment for moment in conflict_times if moment is not None],
                limit=self._limits.max_conflicts_per_utc_week,
            ),
        )

    async def reserve_entry(
        self, *, signal_key: str, notional_usdt: Decimal, now: dt.datetime
    ) -> TrendSignalSnapshot:
        """Reserve one exposure slot or refuse with every tripped reason."""
        _require_aware(now)
        if not _stored_exactly(notional_usdt):
            # The judged value must be the value the book will count.
            raise TrendStateBlocked("notional must be exact to 12 decimals")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceTrendSignal, signal_key, with_for_update=True)
            if row is None or row.state != "observed":
                raise TrendStateBlocked("signal already processed or unavailable")
            book = await self._load_book(db, now)
            decision = decide_entry(
                book=book,
                symbol=row.symbol,
                notional_usdt=notional_usdt,
                limits=self._limits,
            )
            if not decision.allowed:
                raise TrendEntryRefused(decision)
            row.state = "entry_reserved"
            row.entry_notional_usdt = notional_usdt
            row.updated_at = now
            return _signal_snapshot(row)

    async def _count_intents(
        self, db: AsyncSession, signal_key: str, states: tuple[str, ...]
    ) -> int:
        return (
            await db.scalar(
                select(func.count())
                .select_from(BinanceTrendIntent)
                .where(
                    BinanceTrendIntent.signal_key == signal_key,
                    BinanceTrendIntent.state.in_(states),
                )
            )
        ) or 0

    async def _release_unsent_intents(
        self, db: AsyncSession, signal_key: str, *, status: str, now: dt.datetime
    ) -> None:
        """A never-sent reservation becomes a terminal ``rejected`` (no order)."""
        rows = (
            await db.scalars(
                select(BinanceTrendIntent)
                .where(
                    BinanceTrendIntent.signal_key == signal_key,
                    BinanceTrendIntent.state == "reserved",
                )
                .with_for_update()
            )
        ).all()
        for intent in rows:
            intent.state = "rejected"
            intent.broker_status = status
            intent.updated_at = now

    async def block_unsent(self, signal_key: str, *, now: dt.datetime) -> None:
        """Release only a signal that has no sent, acknowledged or uncertain intent."""
        _require_aware(now)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceTrendSignal, signal_key, with_for_update=True)
            if row is None or row.state not in {"observed", "entry_reserved"}:
                raise TrendStateBlocked("cannot release a sent or held signal")
            if await self._count_intents(db, signal_key, _SENT_INTENTS):
                raise TrendStateBlocked("unresolved send intent blocks release")
            await self._release_unsent_intents(
                db, signal_key, status="unsent_released", now=now
            )
            row.state = "blocked"
            row.updated_at = now

    async def reserve_intent(
        self,
        *,
        signal_key: str,
        leg_key: str,
        side: str,
        qty: Decimal,
        reference_price: Decimal,
        reduce_only: bool,
        now: dt.datetime,
    ) -> TrendIntentSnapshot:
        """Reserve the one entry leg. Closing legs arrive with CT-4.

        ``qty * reference_price`` must fit inside the notional reserved for the
        signal, so a fenced intent never carries exposure the book does not
        count. The executor re-checks against a fresh executable price at
        dispatch (CT-4).
        """
        _require_aware(now)
        if not qty.is_finite() or qty <= 0 or side not in SIDES:
            raise TrendStateBlocked("invalid trend intent")
        if not _stored_exactly(qty):
            # Judged against the exact quantity that will be persisted.
            raise TrendStateBlocked("qty must be exact to 12 decimals")
        if not reference_price.is_finite() or reference_price <= 0:
            raise TrendStateBlocked("invalid reference price")
        if reduce_only or leg_key != "entry":
            raise TrendStateBlocked("only the entry leg can be reserved")
        client_order_id = trend_client_order_id(signal_key, leg_key)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            signal = await db.get(BinanceTrendSignal, signal_key, with_for_update=True)
            if signal is None or signal.state != "entry_reserved":
                raise TrendStateBlocked("entry intent requires reserved trend signal")
            if side != signal.side:
                raise TrendStateBlocked("entry side must match the signal")
            if (
                signal.entry_notional_usdt is None
                or qty * reference_price > signal.entry_notional_usdt
            ):
                raise TrendStateBlocked("intent exceeds the reserved notional")
            if await self._count_intents(db, signal_key, _ACTIVE_INTENTS):
                raise TrendStateBlocked("previous intent unresolved")
            if await db.get(BinanceTrendIntent, client_order_id):
                raise TrendStateBlocked("trend leg already attempted")
            row = BinanceTrendIntent(
                client_order_id=client_order_id,
                signal_key=signal_key,
                leg_key=leg_key,
                side=side,
                qty=qty,
                reduce_only=False,
                state="reserved",
                executed_qty=0,
            )
            db.add(row)
            signal.entry_client_order_id = client_order_id
            signal.updated_at = now
            await db.flush()
            return _intent_snapshot(row)

    async def mark_sending(
        self, client_order_id: str, *, now: dt.datetime
    ) -> TrendIntentSnapshot:
        """Commit the send fence. Only a ``reserved`` intent can be fenced."""
        _require_aware(now)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if row is None or row.state != "reserved":
                raise TrendStateBlocked("intent is not an unsent reservation")
            signal = await db.get(
                BinanceTrendSignal, row.signal_key, with_for_update=True
            )
            if (
                signal is None
                or signal.state != "entry_reserved"
                or signal.entry_notional_usdt is None
            ):
                raise TrendStateBlocked("signal no longer holds the reservation")
            # The fence is the authorization boundary: re-judge every entry
            # rule under the lane lock, without counting this reservation
            # against itself.
            book = await self._load_book(db, now, exclude_signal_key=signal.signal_key)
            decision = decide_entry(
                book=book,
                symbol=signal.symbol,
                notional_usdt=signal.entry_notional_usdt,
                limits=self._limits,
            )
            if not decision.allowed:
                raise TrendEntryRefused(decision)
            row.state = "sending"
            row.updated_at = now
            return _intent_snapshot(row)

    async def mark_uncertain(
        self, client_order_id: str, *, now: dt.datetime
    ) -> TrendIntentSnapshot:
        """Keep an ambiguous send as uncertain; it is never resent."""
        _require_aware(now)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if row is None or row.state not in {"sending", "acknowledged"}:
                raise TrendStateBlocked("only a sent intent can become uncertain")
            signal = await db.get(
                BinanceTrendSignal, row.signal_key, with_for_update=True
            )
            if signal is None or signal.state not in {"entry_reserved", "holding"}:
                raise TrendStateBlocked("signal cannot carry an uncertain intent")
            row.state = "uncertain"
            row.updated_at = now
            signal.state = "uncertain"
            signal.updated_at = now
            return _intent_snapshot(row)

    async def resolve_conflicts(self, *, resolved_by: str, now: dt.datetime) -> int:
        """Operator release of the conflict stop (design §4.5).

        Marks every unresolved conflict record resolved, once, and returns how
        many. Nothing else about those terminal rows can change.
        """
        _require_aware(now)
        if not resolved_by.strip() or len(resolved_by) > 100:
            raise TrendStateBlocked("resolver identity required (<=100 chars)")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            rows = (
                await db.scalars(
                    select(BinanceTrendSignal)
                    .where(
                        BinanceTrendSignal.state.in_(CONFLICT_SIGNAL_STATES),
                        BinanceTrendSignal.conflict_resolved_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()
            for row in rows:
                row.conflict_resolved_at = now
                row.conflict_resolved_by = resolved_by
                row.updated_at = now
            return len(rows)

    @staticmethod
    def _detail(detail: str | None) -> str | None:
        if detail is None:
            return None
        if len(detail) > _MAX_DETAIL:
            raise TrendStateBlocked("conflict detail too long")
        return detail

    async def record_conflict_blocked(
        self, signal_key: str, *, now: dt.datetime, detail: str | None = None
    ) -> TrendSignalSnapshot:
        """Our own pre-send block on foreign account exposure (design §4.5)."""
        _require_aware(now)
        detail = self._detail(detail)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceTrendSignal, signal_key, with_for_update=True)
            if row is None or row.state not in {"observed", "entry_reserved"}:
                raise TrendStateBlocked("conflict_blocked needs an unsent signal")
            if await self._count_intents(db, signal_key, _SENT_INTENTS):
                raise TrendStateBlocked("a sent intent cannot be conflict_blocked")
            await self._release_unsent_intents(
                db, signal_key, status="unsent_conflict_blocked", now=now
            )
            row.state = "conflict_blocked"
            row.conflict_at = now
            row.conflict_detail = detail
            row.updated_at = now
            return _signal_snapshot(row)

    async def record_conflict_rejected(
        self,
        client_order_id: str,
        *,
        broker_status: str,
        now: dt.datetime,
        detail: str | None = None,
    ) -> TrendSignalSnapshot:
        """A broker rejection of a sent entry that conflicts with foreign exposure."""
        _require_aware(now)
        detail = self._detail(detail)
        if not broker_status:
            raise TrendStateBlocked("broker rejection status required")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            intent = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if intent is None or intent.state != "sending" or intent.executed_qty != 0:
                raise TrendStateBlocked("conflict_rejected needs a sending entry")
            signal = await db.get(
                BinanceTrendSignal, intent.signal_key, with_for_update=True
            )
            if signal is None or signal.state != "entry_reserved":
                raise TrendStateBlocked("signal is not awaiting its entry")
            intent.state = "rejected"
            intent.broker_status = broker_status
            intent.updated_at = now
            signal.state = "conflict_rejected"
            signal.conflict_at = now
            signal.conflict_detail = detail
            signal.updated_at = now
            return _signal_snapshot(signal)


__all__ = [
    "TrendEntryRefused",
    "TrendIntentSnapshot",
    "TrendSignalSnapshot",
    "TrendStateBlocked",
    "TrendStateService",
]
