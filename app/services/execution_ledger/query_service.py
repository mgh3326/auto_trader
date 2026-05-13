"""Read-only projection service for execution ledger /invest fill endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.schemas.execution_ledger import (
    DataState,
    ExecutionLedgerFreshnessEntry,
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerListResponse,
    ExecutionLedgerRead,
    SourceBreakdown,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository

_FRESH_HOURS = 48
_STALE_HOURS = 72


def _compute_source_breakdown(items: list[ExecutionLedgerRead]) -> SourceBreakdown:
    bd = SourceBreakdown()
    for item in items:
        if item.source == "reconciler":
            bd.reconciler += 1
        elif item.source == "websocket":
            bd.websocket += 1
        elif item.source == "manual_import":
            bd.manual_import += 1
    return bd


def _data_state_from_lag(lag_minutes: float | None) -> DataState:
    if lag_minutes is None:
        return "missing"
    if lag_minutes <= _FRESH_HOURS * 60:
        return "fresh"
    if lag_minutes <= _STALE_HOURS * 60:
        return "stale"
    return "missing"


def _state_from_items_and_freshness(
    items: list[ExecutionLedgerRead],
    freshness: ExecutionLedgerFreshnessReport,
    market: str | None,
) -> tuple[DataState | None, str | None]:
    """Return (data_state, empty_reason) for a list response."""
    # Determine which brokers are relevant to the market filter
    if market == "crypto":
        relevant_brokers = {"upbit"}
    elif market in ("kr", "us"):
        relevant_brokers = {"kis"}
    else:
        relevant_brokers = {"kis", "upbit"}

    relevant_entries = [e for e in freshness.items if e.broker in relevant_brokers]

    # Worst state across relevant brokers
    states: list[DataState] = [e.dataState for e in relevant_entries]
    if not states:
        overall: DataState = "missing"
    elif "missing" in states:
        overall = "missing"
    elif "stale" in states:
        overall = "stale"
    else:
        overall = "fresh"

    if items:
        return overall, None

    # Empty results — explain why
    if overall == "missing":
        return overall, "no reconcile data available yet"
    return overall, "no fills in the requested window"


class ExecutionLedgerQueryService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ExecutionLedgerRepository(db)

    async def list_recent(
        self, *, limit: int = 50, market: str | None = None
    ) -> ExecutionLedgerListResponse:
        stmt = (
            select(ExecutionLedger)
            .order_by(ExecutionLedger.filled_at.desc())
            .limit(limit)
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, market
        )
        return ExecutionLedgerListResponse(
            count=len(items),
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def list_by_symbol(
        self, *, symbol: str, days: int = 30
    ) -> ExecutionLedgerListResponse:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.symbol == symbol)
            .where(ExecutionLedger.filled_at >= cutoff)
            .order_by(ExecutionLedger.filled_at.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, None
        )
        # For a symbol query, be specific about why it's empty
        if not items and empty_reason == "no fills in the requested window":
            empty_reason = f"no fills for {symbol} in the last {days} days"
        return ExecutionLedgerListResponse(
            count=len(items),
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def list_sell_history(
        self, *, days: int = 30, market: str | None = None, limit: int = 100
    ) -> ExecutionLedgerListResponse:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.side == "sell")
            .where(ExecutionLedger.filled_at >= cutoff)
            .order_by(ExecutionLedger.filled_at.desc())
            .limit(limit)
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, market
        )
        return ExecutionLedgerListResponse(
            count=len(items),
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def freshness(
        self, *, freshness_window_hours: int = 24
    ) -> ExecutionLedgerFreshnessReport:
        latest = await self.repo.latest_run_per_broker()
        now = datetime.now(UTC)
        items: list[ExecutionLedgerFreshnessEntry] = []
        for broker in ("kis", "upbit"):
            run = latest.get(broker)
            if run is None or run.finished_at is None:
                items.append(
                    ExecutionLedgerFreshnessEntry(
                        broker=broker,
                        dataState="missing",
                        notes="no successful reconcile run",
                    )
                )
                continue
            finished_at = (
                run.finished_at.astimezone(UTC)
                if run.finished_at.tzinfo
                else run.finished_at.replace(tzinfo=UTC)
            )
            lag_minutes = (now - finished_at).total_seconds() / 60
            if lag_minutes <= freshness_window_hours * 2 * 60:
                state: DataState = "fresh"
            elif lag_minutes <= 24 * 3 * 60:
                state = "stale"
            else:
                state = "missing"
            items.append(
                ExecutionLedgerFreshnessEntry(
                    broker=broker,
                    last_run_at=run.finished_at,
                    lag_minutes=round(lag_minutes, 2),
                    dataState=state,
                    last_run_id=run.run_id,
                )
            )
        return ExecutionLedgerFreshnessReport(items=items)
