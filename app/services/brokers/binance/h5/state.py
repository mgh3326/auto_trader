"""H5 service layer: durable signal, exposure reservation and send fencing.

All writes to the H5 tables are owned here. A committed sending intent is
never dispatched again after restart; only positive order-id/client-id broker
evidence can advance it. The shared Demo order ledger is written separately
through BinanceDemoLedgerService by the executor.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_h5 import (
    BinanceH5Intent,
    BinanceH5LaneState,
    BinanceH5NavSample,
    BinanceH5Opportunity,
    BinanceH5Signal,
)
from app.services.brokers.binance.demo_strategy_loop.strategy import Signal

from .constants import TAKER_FEE_RATE
from .holding import entry_kill_reasons
from .strategy import IDENTITY, UNIVERSE, make_signal_key

_KST = ZoneInfo("Asia/Seoul")
_LOCK_KEY = 847005
_ACTIVE_SIGNALS = ("entry_reserved", "holding", "uncertain")
_ACTIVE_INTENTS = ("reserved", "sending", "acknowledged", "evidenced", "uncertain")
_TERMINAL_BROKER = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})
_BROKER_STATUSES = _TERMINAL_BROKER | {"NEW", "PARTIALLY_FILLED"}


class H5StateBlocked(RuntimeError):
    """An H5 safety gate refused an entry, close or state transition."""


@dataclass(frozen=True)
class H5SignalSnapshot:
    signal_key: str
    correlation_id: str
    symbol: str
    side: str
    decision_ts: int
    signal_price_text: str
    state: str
    entry_client_order_id: str | None
    entry_nav_usdt: Decimal | None
    entry_qty: Decimal
    entry_price: Decimal | None
    entered_at: dt.datetime | None
    closed_qty: Decimal
    realized_pnl_usdt: Decimal
    fees_usdt: Decimal
    exit_reason: str | None
    exit_at: dt.datetime | None
    exit_bar_close_ts: int | None
    forecast_id: str | None
    forecast_resolved_at: dt.datetime | None

    @property
    def remaining_qty(self) -> Decimal:
        return self.entry_qty - self.closed_qty


@dataclass(frozen=True)
class H5IntentSnapshot:
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
    avg_price: Decimal | None


@dataclass(frozen=True)
class H5OrderEvidence:
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
    order_created_at: dt.datetime | None = None
    order_updated_at: dt.datetime | None = None


def _signal_snapshot(row: BinanceH5Signal) -> H5SignalSnapshot:
    return H5SignalSnapshot(
        signal_key=row.signal_key,
        correlation_id=row.correlation_id,
        symbol=row.symbol,
        side=row.side,
        decision_ts=row.decision_ts,
        signal_price_text=row.signal_price_text,
        state=row.state,
        entry_client_order_id=row.entry_client_order_id,
        entry_nav_usdt=row.entry_nav_usdt,
        entry_qty=row.entry_qty,
        entry_price=row.entry_price,
        entered_at=row.entered_at,
        closed_qty=row.closed_qty,
        realized_pnl_usdt=row.realized_pnl_usdt,
        fees_usdt=row.fees_usdt,
        exit_reason=row.exit_reason,
        exit_at=row.exit_at,
        exit_bar_close_ts=row.exit_bar_close_ts,
        forecast_id=row.forecast_id,
        forecast_resolved_at=row.forecast_resolved_at,
    )


def _intent_snapshot(row: BinanceH5Intent) -> H5IntentSnapshot:
    return H5IntentSnapshot(
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
    )


def h5_correlation_id(signal_key: str) -> str:
    return "binance-h5:" + hashlib.sha256(signal_key.encode()).hexdigest()[:24]


def h5_client_order_id(signal_key: str, leg_key: str) -> str:
    digest = hashlib.sha256(f"{signal_key}|{leg_key}".encode()).hexdigest()[:28]
    return "h5-" + digest


class H5StateService:
    """Each mutation commits in its own transaction before any broker send."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._factory = session_factory

    @staticmethod
    async def _lock(db: AsyncSession) -> None:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})

    async def observe_signal(self, signal: Signal, price_text: str) -> H5SignalSnapshot:
        if signal.strategy_id != IDENTITY or signal.symbol not in UNIVERSE:
            raise H5StateBlocked("non-H5 signal refused")
        key = make_signal_key(
            signal.symbol, signal.decision_ts, signal.side, price_text
        )
        async with self._factory() as db, db.begin():
            await self._lock(db)
            await db.execute(
                pg_insert(BinanceH5Signal)
                .values(
                    signal_key=key,
                    correlation_id=h5_correlation_id(key),
                    symbol=signal.symbol,
                    side=signal.side,
                    decision_ts=signal.decision_ts,
                    signal_price_text=price_text,
                    state="observed",
                    entry_qty=0,
                    closed_qty=0,
                    realized_pnl_usdt=0,
                    fees_usdt=0,
                )
                .on_conflict_do_nothing(index_elements=["signal_key"])
            )
            row = await db.get(BinanceH5Signal, key)
            assert row is not None
            if (
                row.symbol != signal.symbol
                or row.side != signal.side
                or row.decision_ts != signal.decision_ts
                or row.signal_price_text != price_text
            ):
                raise H5StateBlocked("signal key collision")
            return _signal_snapshot(row)

    async def get_signal(self, signal_key: str) -> H5SignalSnapshot | None:
        async with self._factory() as db:
            row = await db.get(BinanceH5Signal, signal_key)
            return _signal_snapshot(row) if row else None

    async def list_active_signals(self) -> tuple[H5SignalSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceH5Signal).where(
                        BinanceH5Signal.state.in_(_ACTIVE_SIGNALS)
                    )
                )
            ).all()
            return tuple(_signal_snapshot(row) for row in rows)

    async def list_forecast_recovery_signals(self) -> tuple[H5SignalSnapshot, ...]:
        """Retry failed writes and resolutions from durable actual fills."""
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceH5Signal).where(
                        BinanceH5Signal.entry_qty > 0,
                        BinanceH5Signal.state.in_(("holding", "closed")),
                        BinanceH5Signal.signal_key.in_(
                            select(BinanceH5Intent.signal_key).where(
                                BinanceH5Intent.reduce_only.is_(False),
                                BinanceH5Intent.state == "settled",
                            )
                        ),
                        (
                            (BinanceH5Signal.forecast_id.is_(None))
                            | (
                                (BinanceH5Signal.state == "closed")
                                & (BinanceH5Signal.forecast_resolved_at.is_(None))
                            )
                        ),
                    )
                )
            ).all()
            return tuple(_signal_snapshot(row) for row in rows)

    async def list_intents(self, signal_key: str) -> tuple[H5IntentSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceH5Intent)
                    .where(BinanceH5Intent.signal_key == signal_key)
                    .order_by(BinanceH5Intent.created_at)
                )
            ).all()
            return tuple(_intent_snapshot(row) for row in rows)

    async def list_unresolved_intents(self) -> tuple[H5IntentSnapshot, ...]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    select(BinanceH5Intent).where(
                        BinanceH5Intent.state.in_(_ACTIVE_INTENTS)
                    )
                )
            ).all()
            return tuple(_intent_snapshot(row) for row in rows)

    async def record_nav(self, *, nav_usdt: Decimal, now: dt.datetime) -> None:
        if not nav_usdt.is_finite() or nav_usdt <= 0 or now.tzinfo is None:
            raise H5StateBlocked("valid NAV and time required")
        day = now.astimezone(_KST).date()
        async with self._factory() as db, db.begin():
            await self._lock(db)
            await db.execute(
                pg_insert(BinanceH5NavSample)
                .values(observed_at=now, nav_usdt=nav_usdt)
                .on_conflict_do_nothing(index_elements=["observed_at"])
            )
            row = await db.get(BinanceH5LaneState, 1)
            if row is None:
                db.add(
                    BinanceH5LaneState(
                        id=1,
                        day_kst=day,
                        day_start_nav_usdt=nav_usdt,
                        peak_nav_usdt=nav_usdt,
                        last_nav_usdt=nav_usdt,
                        day_entry_halted=False,
                    )
                )
                return
            if day != row.day_kst:
                row.day_kst = day
                row.day_start_nav_usdt = nav_usdt
                row.day_entry_halted = False
            row.peak_nav_usdt = max(row.peak_nav_usdt, nav_usdt)
            row.last_nav_usdt = nav_usdt
            if nav_usdt <= row.day_start_nav_usdt * Decimal("0.97"):
                row.day_entry_halted = True
            if nav_usdt <= row.peak_nav_usdt * Decimal("0.85"):
                row.halt_reason = "mdd_lane_stop"
            row.updated_at = now

    async def last_tick_at(self) -> dt.datetime | None:
        """Read-only runner liveness: ``record_nav`` stamps this row each tick."""
        async with self._factory() as db:
            row = await db.get(BinanceH5LaneState, 1)
            return row.updated_at if row is not None else None

    async def decision_processed(self, decision_ts: int) -> bool:
        async with self._factory() as db:
            row = await db.get(BinanceH5LaneState, 1)
            return (
                row is not None
                and row.last_decision_ts is not None
                and row.last_decision_ts >= decision_ts
            )

    async def mark_decision_processed(
        self, decision_ts: int, *, now: dt.datetime
    ) -> None:
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5LaneState, 1)
            if row is None:
                raise H5StateBlocked("H5 lane state missing")
            row.last_decision_ts = max(row.last_decision_ts or 0, decision_ts)
            row.updated_at = now

    async def reserve_entry(
        self,
        *,
        signal_key: str,
        completed_bar_closes: Sequence[int],
        entry_nav_usdt: Decimal,
        now: dt.datetime,
    ) -> H5SignalSnapshot:
        if now.tzinfo is None:
            raise H5StateBlocked("aware timestamp required")
        day_start = dt.datetime.combine(
            now.astimezone(_KST).date(), dt.time.min, tzinfo=_KST
        )
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Signal, signal_key, with_for_update=True)
            if row is None or row.state != "observed":
                raise H5StateBlocked("signal already processed or unavailable")
            lane = await db.get(BinanceH5LaneState, 1)
            if lane is None or lane.day_kst != now.astimezone(_KST).date():
                raise H5StateBlocked("fresh daily NAV baseline required")
            stop_count = await db.scalar(
                select(func.count())
                .select_from(BinanceH5Signal)
                .where(
                    BinanceH5Signal.exit_at >= day_start,
                    BinanceH5Signal.exit_reason.in_(("hard_stop", "bar_close_stop")),
                )
            )
            reasons = entry_kill_reasons(
                stop_losses_today=stop_count or 0,
                daily_pnl_usdt=lane.last_nav_usdt - lane.day_start_nav_usdt,
                day_start_nav_usdt=lane.day_start_nav_usdt,
                peak_nav_usdt=lane.peak_nav_usdt,
                current_nav_usdt=lane.last_nav_usdt,
            )
            if lane.halt_reason:
                reasons += (lane.halt_reason,)
            if lane.day_entry_halted:
                reasons += ("daily_loss_entry_stop",)
            if reasons:
                raise H5StateBlocked(";".join(sorted(set(reasons))))
            active_count = await db.scalar(
                select(func.count())
                .select_from(BinanceH5Signal)
                .where(BinanceH5Signal.state.in_(_ACTIVE_SIGNALS))
            )
            if (active_count or 0) >= 2:
                raise H5StateBlocked("H5 global position cap reached")
            symbol_count = await db.scalar(
                select(func.count())
                .select_from(BinanceH5Signal)
                .where(
                    BinanceH5Signal.symbol == row.symbol,
                    BinanceH5Signal.state.in_(_ACTIVE_SIGNALS),
                )
            )
            if symbol_count:
                raise H5StateBlocked("H5 symbol already active")
            last_exit = await db.scalar(
                select(func.max(BinanceH5Signal.exit_bar_close_ts)).where(
                    BinanceH5Signal.symbol == row.symbol,
                    BinanceH5Signal.state == "closed",
                )
            )
            if last_exit is not None:
                complete_since = sum(
                    last_exit < close_ts <= row.decision_ts
                    for close_ts in set(completed_bar_closes)
                )
                if complete_since <= 5:
                    raise H5StateBlocked("five complete-bar re-entry ban")
            row.state = "entry_reserved"
            row.entry_nav_usdt = entry_nav_usdt
            row.updated_at = now
            return _signal_snapshot(row)

    async def record_opportunity_grid(
        self,
        entries: Sequence[
            tuple[str, int, Decimal, Decimal, Decimal, str, Decimal, Decimal]
        ],
    ) -> None:
        """Commit the predeclared complete-bar/quote grid before signal orders."""
        if len(entries) != len(UNIVERSE) or {item[0] for item in entries} != set(
            UNIVERSE
        ):
            raise H5StateBlocked("H5 opportunity grid must cover all three symbols")
        if len({item[1] for item in entries}) != 1:
            raise H5StateBlocked("H5 opportunity grid must share one decision time")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            for (
                symbol,
                decision_ts,
                bar_open,
                bar_high,
                bar_low,
                close_text,
                bid,
                ask,
            ) in entries:
                await db.execute(
                    pg_insert(BinanceH5Opportunity)
                    .values(
                        symbol=symbol,
                        decision_ts=decision_ts,
                        bar_open=bar_open,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        bar_close_text=close_text,
                        bid=bid,
                        ask=ask,
                    )
                    .on_conflict_do_nothing(index_elements=["symbol", "decision_ts"])
                )

    async def block_unsent(self, signal_key: str, *, now: dt.datetime) -> None:
        """Release only a signal that has no sending, ack or uncertain intent."""
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Signal, signal_key, with_for_update=True)
            if row is None or row.state not in {"observed", "entry_reserved"}:
                raise H5StateBlocked("cannot release a sent or held signal")
            active = await db.scalar(
                select(func.count())
                .select_from(BinanceH5Intent)
                .where(
                    BinanceH5Intent.signal_key == signal_key,
                    BinanceH5Intent.state.in_(_ACTIVE_INTENTS),
                )
            )
            if active:
                raise H5StateBlocked("unresolved send intent blocks release")
            row.state = "blocked"
            row.updated_at = now

    async def reserve_intent(
        self,
        *,
        signal_key: str,
        leg_key: str,
        side: str,
        qty: Decimal,
        reduce_only: bool,
        now: dt.datetime,
    ) -> H5IntentSnapshot:
        if qty <= 0 or not qty.is_finite() or side not in {"BUY", "SELL"}:
            raise H5StateBlocked("invalid H5 intent")
        cid = h5_client_order_id(signal_key, leg_key)
        async with self._factory() as db, db.begin():
            await self._lock(db)
            signal = await db.get(BinanceH5Signal, signal_key, with_for_update=True)
            if signal is None:
                raise H5StateBlocked("signal missing")
            if reduce_only:
                if (
                    signal.state != "holding"
                    or signal.entry_qty - signal.closed_qty <= 0
                ):
                    raise H5StateBlocked("no broker-proven holding for close")
                if side == signal.side or qty > signal.entry_qty - signal.closed_qty:
                    raise H5StateBlocked("invalid reduceOnly close")
            elif (
                signal.state != "entry_reserved"
                or leg_key != "entry"
                or side != signal.side
            ):
                raise H5StateBlocked("entry intent requires reserved H5 signal")
            active = await db.scalar(
                select(func.count())
                .select_from(BinanceH5Intent)
                .where(
                    BinanceH5Intent.signal_key == signal_key,
                    BinanceH5Intent.state.in_(_ACTIVE_INTENTS),
                )
            )
            if active:
                raise H5StateBlocked("previous intent unresolved")
            if await db.get(BinanceH5Intent, cid):
                raise H5StateBlocked("H5 leg already attempted")
            row = BinanceH5Intent(
                client_order_id=cid,
                signal_key=signal_key,
                leg_key=leg_key,
                side=side,
                qty=qty,
                reduce_only=reduce_only,
                state="reserved",
                executed_qty=0,
            )
            db.add(row)
            await db.flush()
            if not reduce_only:
                signal.entry_client_order_id = cid
            return _intent_snapshot(row)

    async def fence_send(
        self, client_order_id: str, *, now: dt.datetime
    ) -> H5IntentSnapshot:
        """Commit immediately before dispatch. No retry from sending state."""
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Intent, client_order_id, with_for_update=True)
            if row is None or row.state != "reserved":
                raise H5StateBlocked("send fence missing or already consumed")
            row.state = "sending"
            row.updated_at = now
            return _intent_snapshot(row)

    async def mark_uncertain(self, client_order_id: str, *, now: dt.datetime) -> None:
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Intent, client_order_id, with_for_update=True)
            if row is None or row.state not in {
                "sending",
                "acknowledged",
                "evidenced",
                "settled",
                "uncertain",
            }:
                raise H5StateBlocked("only a fenced send can be uncertain")
            row.state = "uncertain"
            row.updated_at = now
            signal = await db.get(BinanceH5Signal, row.signal_key, with_for_update=True)
            assert signal is not None
            signal.state = "uncertain"
            signal.updated_at = now

    async def apply_order_evidence(
        self,
        evidence: H5OrderEvidence,
        *,
        broker_position_amt: Decimal,
        exit_bar_close_ts: int | None,
        now: dt.datetime,
    ) -> tuple[H5SignalSnapshot, H5IntentSnapshot]:
        """Apply only matching positive broker order and position evidence.

        A NEW or partially filled order stays active. Unknown or contradictory
        evidence leaves the committed intent blocked for manual reconciliation.
        """
        if evidence.status not in _BROKER_STATUSES:
            raise H5StateBlocked("unknown broker order status")
        if evidence.executed_qty > 0 and (
            evidence.order_created_at is None
            or evidence.order_updated_at is None
            or evidence.order_created_at.tzinfo is None
            or evidence.order_updated_at.tzinfo is None
            or not evidence.order_created_at <= evidence.order_updated_at <= now
        ):
            raise H5StateBlocked("broker execution clock evidence unavailable")
        if not all(
            value.is_finite()
            for value in (
                evidence.orig_qty,
                evidence.executed_qty,
                evidence.avg_price,
                broker_position_amt,
            )
        ):
            raise H5StateBlocked("non-finite broker order evidence")
        if evidence.status == "NEW" and evidence.executed_qty != 0:
            raise H5StateBlocked("NEW status contradicts executed quantity")
        if evidence.status == "PARTIALLY_FILLED" and not (
            0 < evidence.executed_qty < evidence.orig_qty
        ):
            raise H5StateBlocked("partial status contradicts executed quantity")
        async with self._factory() as db, db.begin():
            await self._lock(db)
            intent = await db.get(
                BinanceH5Intent, evidence.client_order_id, with_for_update=True
            )
            if intent is None or intent.state not in {
                "sending",
                "acknowledged",
                "evidenced",
                "uncertain",
            }:
                raise H5StateBlocked("no fenced H5 intent for order evidence")
            signal = await db.get(
                BinanceH5Signal, intent.signal_key, with_for_update=True
            )
            assert signal is not None
            if (
                evidence.symbol != signal.symbol
                or evidence.side != intent.side
                or evidence.orig_qty != intent.qty
                or evidence.reduce_only != intent.reduce_only
                or evidence.position_side != "BOTH"
                or not evidence.broker_order_id
                or (
                    intent.broker_order_id is not None
                    and evidence.broker_order_id != intent.broker_order_id
                )
                or not (intent.executed_qty <= evidence.executed_qty <= intent.qty)
                or (evidence.executed_qty > 0 and evidence.avg_price <= 0)
            ):
                raise H5StateBlocked("broker order echo mismatch")
            if evidence.status == "FILLED" and evidence.executed_qty != intent.qty:
                raise H5StateBlocked("FILLED without full quantity")
            old_quote = intent.executed_qty * (intent.avg_price or Decimal(0))
            new_quote = evidence.executed_qty * evidence.avg_price
            delta_qty = evidence.executed_qty - intent.executed_qty
            delta_quote = new_quote - old_quote
            if (
                delta_qty == 0
                and intent.avg_price is not None
                and evidence.avg_price != intent.avg_price
            ):
                raise H5StateBlocked("fill price changed without new quantity")
            if delta_qty > 0 and delta_quote <= 0:
                raise H5StateBlocked("invalid incremental fill price")
            if delta_qty > 0:
                signal.fees_usdt += delta_quote * TAKER_FEE_RATE
                if intent.reduce_only:
                    if signal.entry_price is None or signal.entry_qty <= 0:
                        raise H5StateBlocked("close fill has no entry evidence")
                    signal.closed_qty += delta_qty
                    if signal.closed_qty > signal.entry_qty:
                        raise H5StateBlocked("close exceeds entry fill")
                    sign = Decimal(1) if signal.side == "BUY" else Decimal(-1)
                    signal.realized_pnl_usdt += sign * (
                        delta_quote - signal.entry_price * delta_qty
                    )
                else:
                    prior_cost = signal.entry_qty * (signal.entry_price or Decimal(0))
                    signal.entry_qty += delta_qty
                    signal.entry_price = (prior_cost + delta_quote) / signal.entry_qty
                    # Order creation is the conservative earliest possible
                    # fill time when a restart first sees cumulative fills.
                    # A late lookup must never restart the 24h holding clock.
                    signal.entered_at = signal.entered_at or evidence.order_created_at
                    signal.state = "holding"
            intent.broker_order_id = evidence.broker_order_id
            intent.broker_status = evidence.status
            intent.executed_qty = evidence.executed_qty
            intent.avg_price = evidence.avg_price if evidence.executed_qty else None
            intent.state = (
                "evidenced" if evidence.status in _TERMINAL_BROKER else "acknowledged"
            )
            expected = signal.entry_qty - signal.closed_qty
            signed_expected = expected if signal.side == "BUY" else -expected
            if broker_position_amt != signed_expected:
                intent.state = "uncertain"
                signal.state = "uncertain"
            elif (
                expected == 0
                and signal.entry_qty == 0
                and not intent.reduce_only
                and evidence.status in _TERMINAL_BROKER
            ):
                signal.state = "blocked"
            elif expected == 0 and signal.entry_qty > 0 and intent.reduce_only:
                signal.state = "closed"
                signal.exit_reason = intent.leg_key.split(":", 1)[0]
                signal.exit_at = evidence.order_updated_at
                signal.exit_bar_close_ts = exit_bar_close_ts
            elif expected > 0:
                signal.state = "holding"
            intent.updated_at = now
            signal.updated_at = now
            return _signal_snapshot(signal), _intent_snapshot(intent)

    async def settle_intent(
        self, client_order_id: str, *, now: dt.datetime
    ) -> H5IntentSnapshot:
        """Release only after the shared ledger and root release have committed."""
        async with self._factory() as db, db.begin():
            await self._lock(db)
            intent = await db.get(
                BinanceH5Intent, client_order_id, with_for_update=True
            )
            if intent is None or intent.state != "evidenced":
                raise H5StateBlocked("intent lacks reconciled terminal evidence")
            signal = await db.get(BinanceH5Signal, intent.signal_key)
            if signal is None or signal.state == "uncertain":
                raise H5StateBlocked("uncertain position blocks intent release")
            intent.state = "settled"
            intent.updated_at = now
            return _intent_snapshot(intent)

    async def mark_forecast_id(
        self, signal_key: str, forecast_id: str, *, now: dt.datetime
    ) -> None:
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Signal, signal_key, with_for_update=True)
            if row is None or row.state not in {"holding", "closed"}:
                raise H5StateBlocked("forecast requires actual H5 fill")
            if row.forecast_id not in (None, forecast_id):
                raise H5StateBlocked("forecast identity mismatch")
            row.forecast_id = forecast_id
            row.updated_at = now

    async def mark_forecast_resolved(
        self, signal_key: str, forecast_id: str, *, now: dt.datetime
    ) -> None:
        async with self._factory() as db, db.begin():
            await self._lock(db)
            row = await db.get(BinanceH5Signal, signal_key, with_for_update=True)
            if row is None or row.state != "closed" or row.forecast_id != forecast_id:
                raise H5StateBlocked("H5 forecast resolution identity mismatch")
            row.forecast_resolved_at = now
            row.updated_at = now
