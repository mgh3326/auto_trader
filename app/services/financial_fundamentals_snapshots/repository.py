from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot

_UPSERTABLE_COLUMNS = (
    "period_type",
    "period_end_date",
    "filing_date",
    "effective_at",
    "source_collected_at",
    "currency",
    "revenue",
    "net_income",
    "gross_profit",
    "cost_of_sales",
    "roe",
    "payout_ratio",
    "dividend_per_share",
    "discrete_revenue",
    "discrete_net_income",
    "data_state",
    "raw_payload",
    "schema_version",
)


class FinancialFundamentalsUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: str
    symbol: str
    fiscal_period: str
    period_type: str
    period_end_date: dt.date
    source: str
    source_collected_at: dt.datetime
    filing_date: dt.date | None = None
    effective_at: dt.date | None = None
    currency: str | None = None
    revenue: Decimal | None = None
    net_income: Decimal | None = None
    gross_profit: Decimal | None = None
    cost_of_sales: Decimal | None = None
    roe: Decimal | None = None
    payout_ratio: Decimal | None = None
    dividend_per_share: Decimal | None = None
    discrete_revenue: Decimal | None = None
    discrete_net_income: Decimal | None = None
    data_state: str = "fresh"
    raw_payload: dict | None = None
    schema_version: int = 1


def _normalize_payload(row: FinancialFundamentalsUpsert) -> dict:
    values = row.model_dump()
    values["market"] = values["market"].strip().lower()
    values["symbol"] = values["symbol"].strip().upper()
    values["source"] = values["source"].strip().lower()
    return values


class FinancialFundamentalsSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[FinancialFundamentalsUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        stmt = insert(FinancialFundamentalsSnapshot).values(payload)
        set_ = {col: getattr(stmt.excluded, col) for col in _UPSERTABLE_COLUMNS}
        set_["computed_at"] = func.now()
        set_["updated_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            constraint="uq_financial_fundamentals_snapshots_msfs",
            set_=set_,
        )
        result = await self._session.execute(stmt)
        return result.rowcount or 0

    async def periods_for_symbol(
        self, *, market: str, symbol: str, period_type: str | None = None
    ) -> list[FinancialFundamentalsSnapshot]:
        stmt = select(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.market == market.strip().lower(),
            FinancialFundamentalsSnapshot.symbol == symbol.strip().upper(),
        )
        if period_type is not None:
            stmt = stmt.where(FinancialFundamentalsSnapshot.period_type == period_type)
        stmt = stmt.order_by(
            FinancialFundamentalsSnapshot.period_end_date.asc(),
            FinancialFundamentalsSnapshot.fiscal_period.asc(),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def latest_periods_for_symbols(
        self,
        *,
        market: str,
        symbols: Iterable[str],
        period_type: str | None = None,
    ) -> dict[str, list[FinancialFundamentalsSnapshot]]:
        """symbol -> period_end_date-ascending rows. One query (no N+1).

        Missing symbols are simply absent from the returned dict (no error).
        """
        norm_market = market.strip().lower()
        norm_symbols = {s.strip().upper() for s in symbols if s.strip()}
        if not norm_symbols:
            return {}
        stmt = select(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.market == norm_market,
            FinancialFundamentalsSnapshot.symbol.in_(norm_symbols),
        )
        if period_type is not None:
            stmt = stmt.where(FinancialFundamentalsSnapshot.period_type == period_type)
        stmt = stmt.order_by(
            FinancialFundamentalsSnapshot.symbol.asc(),
            FinancialFundamentalsSnapshot.period_end_date.asc(),
            FinancialFundamentalsSnapshot.fiscal_period.asc(),
        )
        result = await self._session.execute(stmt)
        grouped: dict[str, list[FinancialFundamentalsSnapshot]] = {}
        for row in result.scalars().all():
            grouped.setdefault(row.symbol, []).append(row)
        return grouped
