"""Read-only projection service for execution ledger /invest fill endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.schemas.execution_ledger import (
    ExecutionLedgerFreshnessEntry,
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerRead,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository


class ExecutionLedgerQueryService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ExecutionLedgerRepository(db)

    async def list_recent(
        self, *, limit: int = 50, market: str | None = None
    ) -> list[ExecutionLedgerRead]:
        stmt = (
            select(ExecutionLedger)
            .order_by(ExecutionLedger.filled_at.desc())
            .limit(limit)
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        rows = (await self.db.execute(stmt)).scalars().all()
        return [ExecutionLedgerRead.model_validate(row) for row in rows]

    async def list_by_symbol(
        self, *, symbol: str, days: int = 30
    ) -> list[ExecutionLedgerRead]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.symbol == symbol)
            .where(ExecutionLedger.filled_at >= cutoff)
            .order_by(ExecutionLedger.filled_at.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        return [ExecutionLedgerRead.model_validate(row) for row in rows]

    async def list_sell_history(
        self, *, days: int = 30, market: str | None = None, limit: int = 100
    ) -> list[ExecutionLedgerRead]:
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
        return [ExecutionLedgerRead.model_validate(row) for row in rows]

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
                state = "fresh"
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
