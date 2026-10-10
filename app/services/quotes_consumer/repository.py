"""DB reads (existing read-only sources) + append-only row inserts.

Reads feed the shadow evaluator: previous close from
``market_quote_snapshots``, the holdings universe from
``manual_holdings`` + ``protected_positions`` (the "core" tier), open
rung anchors from the three live order ledgers, and own fills from
``review.execution_ledger``.  Writes are INSERT … ON CONFLICT DO
NOTHING on a deterministic ``dedupe_key`` — there is no UPDATE, DELETE,
or ledger-write code path in this package.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Integer, cast, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.manual_holdings import BrokerAccount, ManualHolding
from app.models.market_quote_snapshot import MarketQuoteSnapshot
from app.models.protected_positions import ProtectedPosition
from app.models.quotes_consumer import LadderTouchEvent, QuotesTriggerFiring
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)
from app.models.trading import InstrumentType

from .triggers import HoldingsView
from .types import LadderRow, OwnFill, RungAnchor, TriggerRow

OPEN_ORDER_STATUSES = ("accepted", "pending", "partial")
LIVE_ORDER_LEDGERS = (
    "kis_live_order_ledger",
    "toss_live_order_ledger",
    "live_order_ledger",
)

_MANUAL_MARKET = {"KR": "kr", "US": "us", "CRYPTO": "crypto"}
_INSTRUMENT_MARKET = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}


def _trigger_values(row: TriggerRow) -> dict:
    return {
        "dedupe_key": row.dedupe_key,
        "trigger_type": row.trigger_type,
        "outcome": row.outcome,
        "symbol": row.symbol,
        "source_symbol": row.source_symbol,
        "market": row.market,
        "session": row.session,
        "reference_price": row.reference_price,
        "current_price": row.current_price,
        "window": row.window,
        "event_ts": row.event_ts,
        "kst_date": row.kst_date,
        "would_kick": row.would_kick,
        "suppress_reason": row.suppress_reason,
        "daily_would_kick_count": row.daily_would_kick_count,
        "last_would_kick_at": row.last_would_kick_at,
        "not_evaluable_reason": row.not_evaluable_reason,
        "source_ref": row.source_ref,
        "detail": row.detail,
    }


def _ladder_values(row: LadderRow) -> dict:
    return {
        "dedupe_key": row.dedupe_key,
        "order_ledger": row.order_ledger,
        "order_ledger_id": row.order_ledger_id,
        "broker_order_id": row.broker_order_id,
        "client_order_id": row.client_order_id,
        "correlation_id": row.correlation_id,
        "event_type": row.event_type,
        "market": row.market,
        "symbol": row.symbol,
        "side": row.side,
        "session": row.session,
        "anchor_price": row.anchor_price,
        "event_price": row.event_price,
        "distance_pct": row.distance_pct,
        "event_ts": row.event_ts,
        "received_at": row.received_at,
        "nxt_tradable": row.nxt_tradable,
        "died_at": row.died_at,
        "stream_entry_id": row.stream_entry_id,
        "fill_ledger_id": row.fill_ledger_id,
        "detail": row.detail,
    }


class QuotesConsumerRepository:
    """Append-only writer + read-only context queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Writes — append-only
    # ------------------------------------------------------------------
    async def insert_firings(self, rows: list[TriggerRow]) -> int:
        if not rows:
            return 0
        stmt = (
            insert(QuotesTriggerFiring)
            .values([_trigger_values(row) for row in rows])
            .on_conflict_do_nothing(constraint="uq_quotes_trigger_firings_dedupe")
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def insert_ladder_events(self, rows: list[LadderRow]) -> int:
        if not rows:
            return 0
        stmt = (
            insert(LadderTouchEvent)
            .values([_ladder_values(row) for row in rows])
            .on_conflict_do_nothing(constraint="uq_ladder_touch_events_dedupe")
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Reads — read-only sources only
    # ------------------------------------------------------------------
    async def previous_closes(
        self, market: str, symbols: list[str]
    ) -> dict[str, Decimal | None]:
        """Latest previous_close per symbol; absent when unknown."""
        if not symbols:
            return {}
        upper = sorted({s.strip().upper() for s in symbols if s.strip()})
        stmt = (
            select(
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.previous_close,
            )
            .where(
                MarketQuoteSnapshot.market == market,
                MarketQuoteSnapshot.symbol.in_(upper),
                MarketQuoteSnapshot.previous_close.is_not(None),
            )
            .distinct(MarketQuoteSnapshot.symbol)
            .order_by(
                MarketQuoteSnapshot.symbol,
                MarketQuoteSnapshot.snapshot_at.desc(),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return {row.symbol: row.previous_close for row in rows}

    async def holdings_universe(self) -> HoldingsView:
        """Held symbols + the core (long-term protected) subset.

        ``protected_positions`` is the operator-declared long-term floor —
        the existing holdings source that marks core inventory.
        """
        held: dict[str, str] = {}
        stmt = (
            select(ManualHolding.ticker, ManualHolding.market_type)
            .join(BrokerAccount, ManualHolding.broker_account_id == BrokerAccount.id)
            .where(BrokerAccount.is_active.is_(True))
            .where(ManualHolding.quantity > 0)
        )
        for ticker, market_type in (await self._session.execute(stmt)).all():
            market = _MANUAL_MARKET.get(str(market_type))
            if market is not None:
                held[str(ticker)] = market
        core: set[str] = set()
        stmt = select(ProtectedPosition.symbol, ProtectedPosition.market).where(
            ProtectedPosition.protected_quantity > 0
        )
        for symbol, market in (await self._session.execute(stmt)).all():
            held.setdefault(str(symbol), str(market))
            core.add(str(symbol))
        return HoldingsView(held=held, core=frozenset(core))

    async def open_rungs(self) -> list[RungAnchor]:
        """All resting (non-terminal) limit rows across the live ledgers."""
        rungs: list[RungAnchor] = []
        kis_rows = (
            await self._session.execute(
                select(KISLiveOrderLedger).where(
                    KISLiveOrderLedger.status.in_(OPEN_ORDER_STATUSES),
                    KISLiveOrderLedger.order_type == "limit",
                    KISLiveOrderLedger.price.is_not(None),
                    KISLiveOrderLedger.price > 0,
                )
            )
        ).scalars()
        for row in kis_rows:
            market = "kr" if row.instrument_type == InstrumentType.equity_kr else "us"
            rungs.append(
                RungAnchor(
                    ledger_name="kis_live_order_ledger",
                    ledger_id=row.id,
                    symbol=row.symbol,
                    market=market,
                    side=row.side,
                    anchor_price=row.price,
                    broker_order_id=row.order_no,
                    client_order_id=None,
                    correlation_id=row.correlation_id,
                    received_at=row.trade_date,
                    died_at=None,
                )
            )
        toss_rows = (
            await self._session.execute(
                select(TossLiveOrderLedger).where(
                    TossLiveOrderLedger.status.in_(OPEN_ORDER_STATUSES),
                    TossLiveOrderLedger.order_type == "limit",
                    TossLiveOrderLedger.price.is_not(None),
                    TossLiveOrderLedger.price > 0,
                )
            )
        ).scalars()
        for row in toss_rows:
            rungs.append(
                RungAnchor(
                    ledger_name="toss_live_order_ledger",
                    ledger_id=row.id,
                    symbol=row.symbol,
                    market=row.market,
                    side=row.side,
                    anchor_price=row.price,
                    broker_order_id=row.broker_order_id,
                    client_order_id=row.client_order_id,
                    correlation_id=row.correlation_id,
                    received_at=row.trade_date,
                    died_at=None,
                )
            )
        live_rows = (
            await self._session.execute(
                select(LiveOrderLedger).where(
                    LiveOrderLedger.status.in_(OPEN_ORDER_STATUSES),
                    LiveOrderLedger.order_kind == "limit",
                    LiveOrderLedger.price.is_not(None),
                    LiveOrderLedger.price > 0,
                )
            )
        ).scalars()
        for row in live_rows:
            rungs.append(
                RungAnchor(
                    ledger_name="live_order_ledger",
                    ledger_id=row.id,
                    symbol=row.symbol,
                    market=row.market,
                    side=row.side,
                    anchor_price=row.price,
                    broker_order_id=row.order_no,
                    client_order_id=None,
                    correlation_id=row.correlation_id,
                    received_at=row.trade_date,
                    died_at=None,
                )
            )
        return rungs

    async def rung_terminal_states(
        self, requests: list[tuple[str, int]]
    ) -> dict[tuple[str, int], tuple[str, datetime | None, Decimal | None]]:
        """(status, reconciled_at, avg_fill_price) for specific order rows."""
        result: dict[tuple[str, int], tuple[str, datetime | None, Decimal | None]] = {}
        by_ledger: dict[str, list[int]] = {}
        for name, row_id in requests:
            by_ledger.setdefault(name, []).append(row_id)
        models = {
            "kis_live_order_ledger": KISLiveOrderLedger,
            "toss_live_order_ledger": TossLiveOrderLedger,
            "live_order_ledger": LiveOrderLedger,
        }
        for name, ids in by_ledger.items():
            model = models.get(name)
            if model is None or not ids:
                continue
            stmt = select(
                model.id, model.status, model.reconciled_at, model.avg_fill_price
            ).where(model.id.in_(ids))
            for row in (await self._session.execute(stmt)).all():
                result[(name, int(row.id))] = (
                    str(row.status),
                    row.reconciled_at,
                    row.avg_fill_price,
                )
        return result

    async def fills_after(self, watermark: int, *, limit: int = 500) -> list[OwnFill]:
        """New execution-ledger rows past the id watermark (own fills)."""
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.id > watermark)
            .where(execution_ledger_in_effect())
            .order_by(ExecutionLedger.id)
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars()
        fills: list[OwnFill] = []
        for row in rows:
            market = _INSTRUMENT_MARKET.get(str(row.instrument_type), "other")
            fills.append(
                OwnFill(
                    ledger_id=row.id,
                    symbol=row.symbol,
                    market=market,
                    side=row.side,
                    price=row.filled_price,
                    qty=row.filled_qty,
                    filled_at=row.filled_at,
                    broker_order_id=row.broker_order_id,
                    broker=row.broker,
                )
            )
        return fills

    async def fills_watermark(self) -> int:
        """Current max execution_ledger id — install boundary for fills."""
        value = await self._session.scalar(select(func.max(ExecutionLedger.id)))
        return int(value or 0)

    async def fills_seed_watermark(self) -> int | None:
        """Last execution_ledger id already recorded as own_fill, or None.

        On restart this resumes from the last recorded fill instead of
        the install boundary, so fills that landed while the consumer was
        down are still recorded — dedupe keys make re-observing them safe.
        """
        stmt = select(func.max(cast(QuotesTriggerFiring.source_ref, Integer))).where(
            QuotesTriggerFiring.trigger_type == "own_fill",
            QuotesTriggerFiring.source_ref.op("~")("^[0-9]+$"),
        )
        value = await self._session.scalar(stmt)
        return int(value) if value is not None else None

    async def gate_seed(self, kst_date: str) -> list[tuple[str, int, datetime | None]]:
        """(market, today's would_kick_count, latest would_kick_at ever).

        The cap is per KST day but the cooldown timestamp is per market
        and carries across midnight — matching #906 ``cooldowns[market]``.
        """
        stmt = (
            select(
                QuotesTriggerFiring.market,
                func.count(QuotesTriggerFiring.id)
                .filter(QuotesTriggerFiring.kst_date == kst_date)
                .label("n"),
                func.max(QuotesTriggerFiring.event_ts).label("last_at"),
            )
            .where(QuotesTriggerFiring.would_kick.is_(True))
            .group_by(QuotesTriggerFiring.market)
        )
        return [
            (row.market or "other", int(row.n), row.last_at)
            for row in (await self._session.execute(stmt)).all()
        ]

    async def breach_seed(self, since: datetime) -> set[tuple[str, str, str]]:
        """(trigger_type, symbol, kst_date) triples with a committed firing.

        The evaluator treats these as still-in-breach until an
        inside-band tick clears them — replayed pending entries then
        conflict instead of writing a second row for the same breach.
        The tuple carries each row's own KST day so a replay after
        midnight still matches its event-day key; the ``since`` window
        keeps the scan bounded while covering any plausible redelivery.
        """
        stmt = (
            select(
                QuotesTriggerFiring.trigger_type,
                QuotesTriggerFiring.symbol,
                QuotesTriggerFiring.kst_date,
            )
            .where(
                QuotesTriggerFiring.outcome == "fired",
                QuotesTriggerFiring.event_ts >= since,
                QuotesTriggerFiring.trigger_type.in_(("holding_spike", "vi_proxy")),
            )
            .distinct()
        )
        return {
            (row.trigger_type, row.symbol, row.kst_date)
            for row in (await self._session.execute(stmt)).all()
        }

    async def rung_event_states(
        self, keys: list[tuple[str, int]]
    ) -> dict[tuple[str, int], set[str]]:
        """Committed event_types per rung — restart state reconstruction."""
        if not keys:
            return {}
        pairs = [(name, row_id) for name, row_id in keys]
        stmt = select(
            LadderTouchEvent.order_ledger,
            LadderTouchEvent.order_ledger_id,
            LadderTouchEvent.event_type,
        ).where(
            tuple_(LadderTouchEvent.order_ledger, LadderTouchEvent.order_ledger_id).in_(
                pairs
            )
        )
        states: dict[tuple[str, int], set[str]] = {}
        for row in (await self._session.execute(stmt)).all():
            states.setdefault((row.order_ledger, int(row.order_ledger_id)), set()).add(
                row.event_type
            )
        return states


__all__ = [
    "LIVE_ORDER_LEDGERS",
    "OPEN_ORDER_STATUSES",
    "QuotesConsumerRepository",
]
