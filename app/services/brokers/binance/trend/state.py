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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, Inexact, InvalidOperation, Overflow, localcontext

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_trend import BinanceTrendIntent, BinanceTrendSignal

from .constants import (
    EXIT_REASONS,
    LOCKED_LIMITS,
    TrendLimits,
    UnknownExitReason,
    assert_exit_reason,
    assert_limits_locked,
)
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
_TICK_LEASE_SQL = text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))")
_TICK_UNLEASE_SQL = text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))")
_TICK_LEASE_KEY = "auto_trader:binance_trend_tick"
_ACTIVE_INTENTS = ("reserved", "sending", "acknowledged", "evidenced", "uncertain")
_SENT_INTENTS = ("sending", "acknowledged", "evidenced", "uncertain")
_MAX_DETAIL = 200


class TrendLeaseLost(RuntimeError):
    """The tick no longer owns the lane; stop before any further send."""


class TrendTickLease:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def check(self) -> None:
        """Re-take (re-entrantly) and release the lock on the lease connection.

        A dropped connection raises; a lock somehow no longer ours returns
        false. Either way ownership is gone.
        """
        try:
            held = await self._db.scalar(_TICK_LEASE_SQL, {"key": _TICK_LEASE_KEY})
            if held is not True:
                raise TrendLeaseLost("trend tick lease lost")
            await self._db.scalar(_TICK_UNLEASE_SQL, {"key": _TICK_LEASE_KEY})
        except TrendLeaseLost:
            raise
        except Exception as exc:
            raise TrendLeaseLost("trend tick lease connection lost") from exc


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
    entry_client_order_id: str | None = None
    entry_qty: Decimal = Decimal(0)
    entry_price: Decimal | None = None
    entered_at: dt.datetime | None = None
    closed_qty: Decimal = Decimal(0)
    realized_pnl_usdt: Decimal = Decimal(0)

    @property
    def remaining_qty(self) -> Decimal:
        return self.entry_qty - self.closed_qty


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
    avg_price: Decimal | None = None
    created_at: dt.datetime | None = None


@dataclass(frozen=True)
class TrendOrderEvidence:
    """Broker order status read by exact client order id (identity-checked)."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    orig_qty: Decimal
    executed_qty: Decimal
    avg_price: Decimal
    status: str
    reduce_only: bool
    position_side: str | None


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
        entry_client_order_id=row.entry_client_order_id,
        entry_qty=row.entry_qty,
        entry_price=row.entry_price,
        entered_at=row.entered_at,
        closed_qty=row.closed_qty,
        realized_pnl_usdt=row.realized_pnl_usdt,
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
        avg_price=row.avg_price,
        created_at=row.created_at,
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


def _notional_exceeds(qty: Decimal, price: Decimal, reserved: Decimal | None) -> bool:
    """Exact ``qty * price > reserved``; anything not exactly computable refuses."""
    if reserved is None:
        return True
    with localcontext() as ctx:
        ctx.prec = 400
        ctx.traps[Inexact] = True
        ctx.traps[Overflow] = True
        try:
            return qty * price > reserved
        except ArithmeticError:
            return True


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

    @asynccontextmanager
    async def tick_lease(self) -> AsyncIterator[TrendTickLease]:
        """Exclusive ownership of the lane for one executor tick.

        A session-level advisory lock on a connection held for the whole tick
        (the database releases it if the process dies). It keeps two ticks from
        churning each other's reservations; it is NOT what makes recovery safe:
        every release is an atomic state-first claim (a fenced intent can no
        longer be released), so even a lost lease cannot yield a sent order
        with a released ledger root. ``lease.check()`` re-verifies ownership on
        the lease connection and raises when it is gone; the executor calls it
        before every send fence. A second tick refuses instead of waiting.
        """
        async with self._factory() as db:
            got = await db.scalar(_TICK_LEASE_SQL, {"key": _TICK_LEASE_KEY})
            if got is not True:
                raise TrendStateBlocked("another trend tick owns the lane")
            try:
                yield TrendTickLease(db)
            finally:
                try:
                    await db.scalar(_TICK_UNLEASE_SQL, {"key": _TICK_LEASE_KEY})
                except Exception:  # connection already gone: the lock went with it
                    pass

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
            if _notional_exceeds(qty, reference_price, signal.entry_notional_usdt):
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
                created_at=now,
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
            if row.reduce_only:
                # An exit never adds exposure, so the entry rules (caps, kill
                # switches) must not be able to hold a stop-out back; it only
                # needs the position it closes.
                if signal is None or signal.state != "holding":
                    raise TrendStateBlocked("exit needs a held signal")
                row.state = "sending"
                row.updated_at = now
                return _intent_snapshot(row)
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

    # ------------------------------------------------------------------
    # CT-4: exit legs, broker acknowledgement and evidence-only resolution.
    # ------------------------------------------------------------------

    async def list_released_client_order_ids(self) -> tuple[str, ...]:
        """Every client order id whose demo ledger row must not stay pre-send.

        Unbounded on purpose: a failed release must never age out of the scan
        (the lane's history is small; a cursor would need a migration).

        Rejected intents (an exit leg or an entry) plus the deterministic entry
        id of every blocked/conflict signal. Recovery releases any of them whose
        ledger row is still planned/previewed/validated, so a failed ledger
        release after a state release is retried instead of stranding a root.
        """
        async with self._factory() as db:
            rejected = (
                await db.scalars(
                    select(BinanceTrendIntent.client_order_id)
                    .where(BinanceTrendIntent.state == "rejected")
                    .order_by(BinanceTrendIntent.updated_at.desc())
                )
            ).all()
            blocked = (
                await db.scalars(
                    select(BinanceTrendSignal.signal_key)
                    .where(
                        BinanceTrendSignal.state.in_(
                            ("blocked", "conflict_blocked", "conflict_rejected")
                        )
                    )
                    .order_by(BinanceTrendSignal.updated_at.desc())
                )
            ).all()
        return tuple(rejected) + tuple(
            trend_client_order_id(key, "entry") for key in blocked
        )

    async def get_intent(self, client_order_id: str) -> TrendIntentSnapshot | None:
        async with self._factory() as db:
            row = await db.get(BinanceTrendIntent, client_order_id)
            return _intent_snapshot(row) if row else None

    @staticmethod
    def _exit_reason(reason: object) -> str:
        try:
            return assert_exit_reason(reason)
        except UnknownExitReason as exc:
            raise TrendStateBlocked(str(exc)) from exc

    async def reserve_exit_intent(
        self,
        *,
        signal_key: str,
        reason: str,
        qty: Decimal,
        now: dt.datetime,
    ) -> TrendIntentSnapshot:
        """Reserve one reduceOnly exit leg for a held signal.

        The reason is checked against the closed vocabulary before any write.
        ``qty`` may not exceed what the signal still holds; the executor bounds
        it further by the broker-proven remainder.
        """
        _require_aware(now)
        reason = self._exit_reason(reason)
        if not qty.is_finite() or qty <= 0:
            raise TrendStateBlocked("invalid trend intent")
        if not _stored_exactly(qty):
            raise TrendStateBlocked("qty must be exact to 12 decimals")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            signal = await db.get(BinanceTrendSignal, signal_key, with_for_update=True)
            if signal is None or signal.state != "holding":
                raise TrendStateBlocked("exit intent requires a held signal")
            if qty > signal.entry_qty - signal.closed_qty:
                raise TrendStateBlocked("exit exceeds the held quantity")
            # One fill stream at a time: any unresolved intent (including a
            # partially filled, still-working entry) blocks a new exit leg.
            if await self._count_intents(db, signal_key, _ACTIVE_INTENTS):
                raise TrendStateBlocked("previous intent unresolved")
            attempts = (
                await db.scalar(
                    select(func.count())
                    .select_from(BinanceTrendIntent)
                    .where(
                        BinanceTrendIntent.signal_key == signal_key,
                        BinanceTrendIntent.reduce_only.is_(True),
                    )
                )
            ) or 0
            leg_key = f"{reason}:{attempts}"
            client_order_id = trend_client_order_id(signal_key, leg_key)
            row = BinanceTrendIntent(
                client_order_id=client_order_id,
                signal_key=signal_key,
                leg_key=leg_key,
                side="SELL" if signal.side == "BUY" else "BUY",
                qty=qty,
                reduce_only=True,
                state="reserved",
                executed_qty=0,
                created_at=now,
            )
            db.add(row)
            signal.updated_at = now
            await db.flush()
            return _intent_snapshot(row)

    async def release_unsent_exit(
        self, client_order_id: str, *, now: dt.datetime
    ) -> None:
        """A never-fenced exit reservation is released; the position stays held."""
        _require_aware(now)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if row is None or row.state != "reserved" or not row.reduce_only:
                raise TrendStateBlocked("only an unsent exit reservation is released")
            row.state = "rejected"
            row.broker_status = "unsent_released"
            row.updated_at = now

    async def acknowledge(
        self,
        client_order_id: str,
        *,
        broker_order_id: str,
        broker_status: str,
        now: dt.datetime,
    ) -> TrendIntentSnapshot:
        """Record the broker's answer to a fenced send (``sending`` only)."""
        _require_aware(now)
        if not broker_order_id or not broker_status:
            raise TrendStateBlocked("broker order id and status required")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if row is None or row.state != "sending":
                raise TrendStateBlocked("only a sending intent can be acknowledged")
            row.state = "acknowledged"
            row.broker_order_id = broker_order_id
            row.broker_status = broker_status
            row.updated_at = now
            return _intent_snapshot(row)

    async def record_rejected(
        self, client_order_id: str, *, broker_status: str, now: dt.datetime
    ) -> TrendSignalSnapshot:
        """An affirmative broker rejection of a fenced send (no order exists).

        An entry leaves its signal ``blocked``; a rejected exit leaves the
        position held so the next tick can try a new leg.
        """
        _require_aware(now)
        if not broker_status:
            raise TrendStateBlocked("broker rejection status required")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            intent = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if intent is None or intent.state != "sending" or intent.executed_qty != 0:
                raise TrendStateBlocked("rejection needs a sending intent")
            signal = await db.get(
                BinanceTrendSignal, intent.signal_key, with_for_update=True
            )
            if signal is None:
                raise TrendStateBlocked("intent signal missing")
            if intent.reduce_only:
                if signal.state != "holding":
                    raise TrendStateBlocked("exit rejection needs a held signal")
            else:
                if signal.state != "entry_reserved":
                    raise TrendStateBlocked("signal is not awaiting its entry")
                signal.state = "blocked"
            intent.state = "rejected"
            intent.broker_status = broker_status
            intent.updated_at = now
            signal.updated_at = now
            return _signal_snapshot(signal)

    async def settle_intent(
        self, client_order_id: str, *, now: dt.datetime
    ) -> TrendIntentSnapshot:
        """``evidenced`` -> ``settled`` once the demo ledger caught up."""
        _require_aware(now)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(
                BinanceTrendIntent, client_order_id, with_for_update=True
            )
            if row is None or row.state not in {"evidenced", "settled"}:
                raise TrendStateBlocked("only an evidenced intent can settle")
            row.state = "settled"
            row.updated_at = now
            return _intent_snapshot(row)

    async def apply_order_evidence(
        self,
        evidence: TrendOrderEvidence,
        *,
        broker_position_amt: Decimal,
        now: dt.datetime,
        filled_at: dt.datetime | None = None,
    ) -> tuple[TrendSignalSnapshot, TrendIntentSnapshot]:
        """Resolve a sent intent from broker evidence, and only from evidence.

        The order must be this intent's (client id, symbol, side, reduceOnly,
        ``BOTH``, original quantity) and any fill must be confirmed by the
        broker position read taken after it. Anything else raises; the caller
        then keeps the intent ``uncertain``. A non-terminal status only records
        progress. Replaying evidence for an already evidenced intent is a no-op.
        """
        _require_aware(now)
        numbers = (
            evidence.orig_qty,
            evidence.executed_qty,
            evidence.avg_price,
            broker_position_amt,
        )
        if any(not value.is_finite() for value in numbers):
            raise TrendStateBlocked("order evidence numbers must be finite")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            intent = await db.get(
                BinanceTrendIntent, evidence.client_order_id, with_for_update=True
            )
            if intent is None:
                raise TrendStateBlocked("evidence for an unknown intent")
            signal = await db.get(
                BinanceTrendSignal, intent.signal_key, with_for_update=True
            )
            if signal is None:
                raise TrendStateBlocked("intent signal missing")
            if (
                evidence.symbol != signal.symbol
                or evidence.side != intent.side
                or evidence.reduce_only is not intent.reduce_only
                or evidence.position_side != "BOTH"
                or evidence.orig_qty != intent.qty
                or evidence.executed_qty < 0
                or evidence.executed_qty > evidence.orig_qty
                or (evidence.executed_qty > 0 and evidence.avg_price <= 0)
            ):
                raise TrendStateBlocked("order evidence does not match the intent")
            if intent.state in {"evidenced", "settled"}:
                if evidence.executed_qty != intent.executed_qty:
                    raise TrendStateBlocked("evidence contradicts the recorded fill")
                return _signal_snapshot(signal), _intent_snapshot(intent)
            if intent.state not in {"sending", "acknowledged", "uncertain"}:
                raise TrendStateBlocked("intent is not awaiting broker evidence")
            if evidence.executed_qty < intent.executed_qty:
                raise TrendStateBlocked("executed quantity cannot decrease")
            intent.broker_order_id = evidence.broker_order_id
            intent.broker_status = evidence.status
            intent.executed_qty = evidence.executed_qty
            intent.avg_price = evidence.avg_price if evidence.executed_qty > 0 else None
            intent.updated_at = now
            if evidence.status in _PENDING_ORDER_STATUSES:
                if intent.state == "sending":
                    intent.state = "acknowledged"
                return _signal_snapshot(signal), _intent_snapshot(intent)
            if evidence.status == "FILLED":
                if evidence.executed_qty != evidence.orig_qty:
                    raise TrendStateBlocked("FILLED without the full quantity")
            elif evidence.status not in _TERMINAL_NO_FILL_STATUSES:
                raise TrendStateBlocked("unknown order status")
            if intent.reduce_only:
                self._apply_exit(
                    signal,
                    intent,
                    evidence,
                    broker_position_amt,
                    when=filled_at or now,
                )
            else:
                self._apply_entry(
                    signal,
                    intent,
                    evidence,
                    broker_position_amt,
                    when=filled_at or now,
                )
            intent.state = "evidenced"
            signal.updated_at = now
            return _signal_snapshot(signal), _intent_snapshot(intent)

    @staticmethod
    def _apply_entry(
        signal: BinanceTrendSignal,
        intent: BinanceTrendIntent,
        evidence: TrendOrderEvidence,
        broker_position_amt: Decimal,
        *,
        when: dt.datetime,
    ) -> None:
        if signal.state not in {"entry_reserved", "uncertain"}:
            raise TrendStateBlocked("signal is not awaiting its entry")
        if evidence.executed_qty == 0:
            signal.state = "blocked"
            return
        expected = evidence.executed_qty
        if intent.side == "SELL":
            expected = -expected
        if broker_position_amt != expected:
            raise TrendStateBlocked("broker position does not confirm the entry fill")
        signal.state = "holding"
        signal.entry_qty = evidence.executed_qty
        signal.entry_price = evidence.avg_price
        signal.entered_at = when

    @staticmethod
    def _apply_exit(
        signal: BinanceTrendSignal,
        intent: BinanceTrendIntent,
        evidence: TrendOrderEvidence,
        broker_position_amt: Decimal,
        *,
        when: dt.datetime,
    ) -> None:
        if signal.state not in {"holding", "uncertain"}:
            raise TrendStateBlocked("signal is not holding an exit")
        if evidence.executed_qty == 0:
            signal.state = "holding"
            return
        if signal.entry_price is None:
            raise TrendStateBlocked("held signal has no entry price")
        closed = signal.closed_qty + evidence.executed_qty
        if closed > signal.entry_qty:
            raise TrendStateBlocked("exit fill exceeds the held quantity")
        remaining = signal.entry_qty - closed
        expected = remaining if signal.side == "BUY" else -remaining
        if broker_position_amt != expected:
            raise TrendStateBlocked("broker position does not confirm the exit fill")
        direction = 1 if signal.side == "BUY" else -1
        pnl = (evidence.avg_price - signal.entry_price) * evidence.executed_qty
        signal.realized_pnl_usdt = signal.realized_pnl_usdt + (
            pnl * direction
        ).quantize(_SCALE)
        signal.closed_qty = closed
        if remaining == 0:
            reason = TrendStateService._exit_reason(intent.leg_key.split(":", 1)[0])
            signal.state = "closed"
            signal.exit_reason = reason
            signal.exit_at = when
        else:
            signal.state = "holding"


_PENDING_ORDER_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
_TERMINAL_NO_FILL_STATUSES = frozenset(
    {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
)
# The closed exit vocabulary is validated by ``assert_exit_reason``; re-exported
# so callers and tests can name it from the state module.
EXIT_REASON_VOCABULARY = EXIT_REASONS

__all__ = [
    "EXIT_REASON_VOCABULARY",
    "TrendEntryRefused",
    "TrendIntentSnapshot",
    "TrendLeaseLost",
    "TrendTickLease",
    "TrendOrderEvidence",
    "TrendSignalSnapshot",
    "TrendStateBlocked",
    "TrendStateService",
]
