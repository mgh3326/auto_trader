"""Read-only ledger / proposal / retro loaders for ROB-1297.

SELECT only. Callers attach :func:`attach_mutation_counter` to prove AC3.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.models.trading import InstrumentType
from app.services.market_close_digest.types import (
    PROPOSAL_MARKET,
    LedgerFill,
    Market,
    ProposalRow,
    RetroRow,
)

_FILLED_STATUSES = ("filled", "partial")


def _in_window(column, start: datetime, end: datetime):  # noqa: ANN001
    return and_(column >= start, column < end)


class DigestSources(Protocol):
    async def list_fills(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LedgerFill, ...]: ...

    async def list_proposals(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[ProposalRow, ...]: ...

    async def list_retros(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[RetroRow, ...]: ...


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _auto_approved(source_asof: object) -> bool:
    return isinstance(source_asof, dict) and isinstance(
        source_asof.get("auto_approved"), dict
    )


def _instrument_type(market: Market) -> InstrumentType:
    if market == "us":
        return InstrumentType.equity_us
    if market == "kr":
        return InstrumentType.equity_kr
    return InstrumentType.crypto


class SqlAlchemyDigestSources:
    """SELECT-only reader over ledger · proposal · retro tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_fills(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LedgerFill, ...]:
        rows: list[LedgerFill] = []
        if market in ("us", "kr"):
            rows.extend(await self._toss_fills(market, window_start, window_end))
        if market in ("us", "crypto"):
            rows.extend(await self._live_fills(market, window_start, window_end))
        if market == "kr":
            rows.extend(await self._kis_fills(window_start, window_end))
        rows.extend(await self._execution_fills(market, window_start, window_end))
        return _dedupe_fills(tuple(rows))

    async def list_proposals(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[ProposalRow, ...]:
        proposal_market = PROPOSAL_MARKET[market]
        stmt = (
            select(OrderProposal, OrderProposalRung)
            .outerjoin(
                OrderProposalRung,
                OrderProposalRung.proposal_pk == OrderProposal.id,
            )
            .where(
                OrderProposal.market == proposal_market,
                OrderProposal.created_at >= window_start,
                OrderProposal.created_at < window_end,
            )
            .order_by(OrderProposal.created_at.asc(), OrderProposal.id.asc())
        )
        result = await self._session.execute(stmt)
        rows: list[ProposalRow] = []
        seen_groups: set[int] = set()
        for group, rung in result.all():
            void_reason = None
            if rung is not None and rung.void_reason:
                void_reason = str(rung.void_reason)
            elif group.void_reason:
                void_reason = str(group.void_reason)
            if group.id in seen_groups and rung is None:
                continue
            if rung is None:
                seen_groups.add(group.id)
            rows.append(
                ProposalRow(
                    symbol=group.symbol,
                    side=group.side if rung is None else rung.side,
                    market=group.market,
                    auto_approved=_auto_approved(group.source_asof),
                    card_kind=group.approval_dispatch_card_kind,
                    lifecycle_state=(
                        group.lifecycle_state if rung is None else rung.state
                    ),
                    void_reason=void_reason,
                    created_at=group.created_at,
                )
            )
        return tuple(rows)

    async def list_retros(
        self,
        market: Market,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[RetroRow, ...]:
        instrument = _instrument_type(market)
        stmt = (
            select(TradeRetrospective)
            .where(
                TradeRetrospective.instrument_type == instrument,
                TradeRetrospective.created_at >= window_start,
                TradeRetrospective.created_at < window_end,
            )
            .order_by(TradeRetrospective.created_at.asc())
        )
        result = await self._session.scalars(stmt)
        return tuple(
            RetroRow(
                symbol=row.symbol,
                side=row.side,
                realized_pnl=_as_decimal(row.realized_pnl),
                pnl_pct=_as_decimal(row.pnl_pct),
                pnl_currency=row.realized_pnl_currency,
                correlation_id=row.correlation_id,
                created_at=row.created_at,
            )
            for row in result.all()
        )

    async def _toss_fills(
        self, market: Market, window_start: datetime, window_end: datetime
    ) -> list[LedgerFill]:
        stmt = (
            select(TossLiveOrderLedger)
            .where(
                TossLiveOrderLedger.market == market,
                TossLiveOrderLedger.operation_kind == "place",
                TossLiveOrderLedger.status.in_(_FILLED_STATUSES),
                or_(
                    _in_window(
                        TossLiveOrderLedger.reconciled_at,
                        window_start,
                        window_end,
                    ),
                    _in_window(
                        TossLiveOrderLedger.trade_date,
                        window_start,
                        window_end,
                    ),
                ),
            )
            .order_by(TossLiveOrderLedger.trade_date.asc())
        )
        result = await self._session.scalars(stmt)
        fills: list[LedgerFill] = []
        for row in result.all():
            qty = _as_decimal(row.filled_qty) or _as_decimal(row.quantity)
            price = _as_decimal(row.avg_fill_price) or _as_decimal(row.price)
            notional = None
            if qty is not None and price is not None:
                notional = qty * price
            pnl = _as_decimal(row.security_pnl_usd)
            currency = "USD"
            if pnl is None:
                pnl = _as_decimal(row.total_pnl_krw)
                currency = "KRW" if pnl is not None else row.currency
            fills.append(
                LedgerFill(
                    source="toss_live_order_ledger",
                    broker="toss",
                    symbol=row.symbol,
                    side=row.side,  # type: ignore[arg-type]
                    qty=qty or Decimal("0"),
                    price=price,
                    notional=notional,
                    pnl=pnl,
                    pnl_pct=None,
                    pnl_currency=currency,
                    filled_at=row.reconciled_at or row.trade_date,
                    correlation_id=row.correlation_id,
                )
            )
        return fills

    async def _live_fills(
        self, market: Market, window_start: datetime, window_end: datetime
    ) -> list[LedgerFill]:
        stmt = (
            select(LiveOrderLedger)
            .where(
                LiveOrderLedger.market == market,
                LiveOrderLedger.status.in_(_FILLED_STATUSES),
                or_(
                    _in_window(LiveOrderLedger.reconciled_at, window_start, window_end),
                    _in_window(LiveOrderLedger.trade_date, window_start, window_end),
                ),
            )
            .order_by(LiveOrderLedger.trade_date.asc())
        )
        result = await self._session.scalars(stmt)
        fills: list[LedgerFill] = []
        for row in result.all():
            qty = _as_decimal(row.filled_qty) or _as_decimal(row.quantity)
            price = _as_decimal(row.avg_fill_price) or _as_decimal(row.price)
            notional = _as_decimal(row.amount)
            if notional is None and qty is not None and price is not None:
                notional = qty * price
            pnl = _as_decimal(row.security_pnl_usd)
            currency = "USD"
            if pnl is None:
                pnl = _as_decimal(row.total_pnl_krw)
                currency = "KRW" if pnl is not None else row.currency
            fills.append(
                LedgerFill(
                    source="live_order_ledger",
                    broker=row.broker,
                    symbol=row.symbol,
                    side=row.side,  # type: ignore[arg-type]
                    qty=qty or Decimal("0"),
                    price=price,
                    notional=notional,
                    pnl=pnl,
                    pnl_pct=None,
                    pnl_currency=currency,
                    filled_at=row.reconciled_at or row.trade_date,
                    correlation_id=row.correlation_id,
                )
            )
        return fills

    async def _kis_fills(
        self, window_start: datetime, window_end: datetime
    ) -> list[LedgerFill]:
        stmt = (
            select(KISLiveOrderLedger)
            .where(
                KISLiveOrderLedger.status.in_(_FILLED_STATUSES),
                or_(
                    _in_window(
                        KISLiveOrderLedger.reconciled_at,
                        window_start,
                        window_end,
                    ),
                    _in_window(KISLiveOrderLedger.trade_date, window_start, window_end),
                ),
            )
            .order_by(KISLiveOrderLedger.trade_date.asc())
        )
        result = await self._session.scalars(stmt)
        fills: list[LedgerFill] = []
        for row in result.all():
            qty = _as_decimal(row.filled_qty) or _as_decimal(row.quantity)
            price = _as_decimal(row.avg_fill_price) or _as_decimal(row.price)
            notional = _as_decimal(row.amount)
            if notional is None and qty is not None and price is not None:
                notional = qty * price
            fills.append(
                LedgerFill(
                    source="kis_live_order_ledger",
                    broker="kis",
                    symbol=row.symbol,
                    side=row.side,  # type: ignore[arg-type]
                    qty=qty or Decimal("0"),
                    price=price,
                    notional=notional,
                    pnl=None,
                    pnl_pct=None,
                    pnl_currency=row.currency,
                    filled_at=row.reconciled_at or row.trade_date,
                    correlation_id=row.correlation_id,
                )
            )
        return fills

    async def _execution_fills(
        self, market: Market, window_start: datetime, window_end: datetime
    ) -> list[LedgerFill]:
        instrument = _instrument_type(market)
        stmt = (
            select(ExecutionLedger)
            .where(
                ExecutionLedger.instrument_type == instrument,
                ExecutionLedger.filled_at >= window_start,
                ExecutionLedger.filled_at < window_end,
            )
            .order_by(ExecutionLedger.filled_at.asc())
        )
        result = await self._session.scalars(stmt)
        return [
            LedgerFill(
                source="execution_ledger",
                broker=row.broker,
                symbol=row.symbol,
                side=row.side,  # type: ignore[arg-type]
                qty=_as_decimal(row.filled_qty) or Decimal("0"),
                price=_as_decimal(row.filled_price),
                notional=_as_decimal(row.filled_notional),
                pnl=None,
                pnl_pct=None,
                pnl_currency=row.currency,
                filled_at=row.filled_at,
                correlation_id=row.correlation_id,
            )
            for row in result.all()
        ]


def merge_retro_pnl(
    fills: Sequence[LedgerFill], retros: Sequence[RetroRow]
) -> tuple[LedgerFill, ...]:
    """Fill missing PnL from retrospectives. Never invents a number."""
    by_corr: dict[str, RetroRow] = {
        row.correlation_id: row
        for row in retros
        if row.correlation_id and row.realized_pnl is not None
    }
    merged: list[LedgerFill] = []
    for fill in fills:
        if fill.pnl is not None:
            merged.append(fill)
            continue
        retro = by_corr.get(fill.correlation_id or "")
        if retro is None:
            merged.append(fill)
            continue
        merged.append(
            LedgerFill(
                source=fill.source,
                broker=fill.broker,
                symbol=fill.symbol,
                side=fill.side,
                qty=fill.qty,
                price=fill.price,
                notional=fill.notional,
                pnl=retro.realized_pnl,
                pnl_pct=retro.pnl_pct,
                pnl_currency=retro.pnl_currency or fill.pnl_currency,
                filled_at=fill.filled_at,
                correlation_id=fill.correlation_id,
            )
        )
    return tuple(merged)


def _dedupe_fills(fills: tuple[LedgerFill, ...]) -> tuple[LedgerFill, ...]:
    """Keep the first row per (broker, symbol, side, qty, filled_at minute)."""
    seen: set[tuple[object, ...]] = set()
    out: list[LedgerFill] = []
    for fill in fills:
        key = (
            fill.broker,
            fill.symbol,
            fill.side,
            str(fill.qty),
            fill.filled_at.replace(second=0, microsecond=0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(fill)
    return tuple(out)
