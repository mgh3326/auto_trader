from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot

logger = logging.getLogger(__name__)

# asyncpg caps bound arguments per statement at 32767 (signed int16 wire
# field). A bounded-by-symbol DART backfill can build thousands of annual rows,
# and with ~22 inserted columns the single-statement upsert overflows that
# ceiling (ROB-442 evidence: $118404 placeholders). Stay well under the limit so
# future column growth never silently breaches it mid-backfill; chunk size is
# derived from the actual column count.
_MAX_BIND_PARAMS = 30_000

# Columns of the uq_financial_fundamentals_snapshots_msfs unique constraint.
_CONFLICT_KEY_FIELDS = ("market", "symbol", "fiscal_period", "source")

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


def _chunk_rows_for_columns(column_count: int) -> int:
    """Rows per upsert statement that keep bind params under the asyncpg ceiling."""
    return max(1, _MAX_BIND_PARAMS // max(1, column_count))


def _dedupe_payload(payload: list[dict]) -> tuple[list[dict], int]:
    """Collapse duplicate conflict keys, keeping the latest-collected row.

    A single multi-row ``VALUES`` upsert cannot reference the same ON CONFLICT
    key twice (Postgres raises "command cannot affect row a second time"), so
    duplicates must be removed before any statement is built. Dedup runs over
    the whole payload up front, so chunk boundaries never split a key.

    Winner per key: the row with the greatest ``source_collected_at``; on an
    exact timestamp tie the last occurrence in ``payload`` wins (last-write-wins
    — deterministic for a given payload, and a key's duplicates come from one
    symbol's sequential parse so their relative order is stable). Surviving keys
    keep their first-seen position (only the value is replaced), so output order
    is stable. Returns ``(deduped_rows, dropped_count)``.
    """
    by_key: dict[tuple, dict] = {}
    dropped = 0
    for values in payload:
        key = tuple(values[field] for field in _CONFLICT_KEY_FIELDS)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = values
            continue
        dropped += 1
        if values["source_collected_at"] >= existing["source_collected_at"]:
            by_key[key] = values
    return list(by_key.values()), dropped


class FinancialFundamentalsSnapshotsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, rows: Iterable[FinancialFundamentalsUpsert]) -> int:
        payload = [_normalize_payload(row) for row in rows]
        if not payload:
            return 0
        deduped, dropped = _dedupe_payload(payload)
        if dropped:
            logger.info(
                "financial_fundamentals upsert collapsed %d duplicate conflict "
                "key(s) from %d payload rows (kept latest source_collected_at)",
                dropped,
                len(payload),
            )
        chunk_rows = _chunk_rows_for_columns(len(deduped[0]))
        total = 0
        for start in range(0, len(deduped), chunk_rows):
            total += await self._upsert_chunk(deduped[start : start + chunk_rows])
        return total

    async def _upsert_chunk(self, chunk: list[dict]) -> int:
        stmt = insert(FinancialFundamentalsSnapshot).values(chunk)
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
