"""ROB-441: US fundamentals from yfinance income statements → FinancialFundamentalsUpsert.

KR uses DART (``builder.py``). US has no DART; this parses yfinance annual
income-statement data (a ``{period_date: {row_label: value}}`` dict, the shape
``_fetch_financials_yfinance`` returns) into the SAME period-row model so the
market-agnostic derive layer (``derive.py``) is reused unchanged.

yfinance exposes no filing date, so ``filing_date`` is approximated as
``period_end + 90 days`` (US 10-K SEC deadline is ~60-90 days after fiscal
year-end). derive's PIT visibility (``_visible_annual`` requires
``filing_date <= report_date``) needs it set, and the lag avoids look-ahead — a
period is not visible until it would plausibly have been filed. Fail-closed: a
period with neither revenue nor net_income is skipped (no empty row written).

This module is the data-parsing layer only (PR1). Writing to the DB (repository
upsert), the CLI/operator orchestration, and the US display path are follow-ups.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa

from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsUpsert,
)

logger = logging.getLogger(__name__)

# US 10-K is due ~60-90 days after fiscal year-end; 90d is the conservative bound
# that keeps recent periods PIT-invisible until they would plausibly be filed.
_ANNUAL_FILING_LAG_DAYS = 90

# yfinance income-statement row labels vary by ticker; try candidates in order.
_REVENUE_LABELS = ("Total Revenue", "Operating Revenue", "Revenue")
_NET_INCOME_LABELS = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Continuous Operations",
    "Net Income From Continuing Operation Net Minority Interest",
    "Net Income Including Noncontrolling Interests",
)
_GROSS_PROFIT_LABELS = ("Gross Profit",)
_COST_OF_SALES_LABELS = (
    "Cost Of Revenue",
    "Reconciled Cost Of Revenue",
    "Cost Of Goods Sold",
)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not dec.is_finite():
        return None
    return dec


def _first_present(
    period_data: dict[str, Any], candidates: tuple[str, ...]
) -> Decimal | None:
    for label in candidates:
        if label in period_data:
            dec = _to_decimal(period_data[label])
            if dec is not None:
                return dec
    return None


def _parse_period_end(key: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(key)[:10])
    except (ValueError, TypeError):
        return None


def parse_us_annual_income_periods(
    *, symbol: str, data: dict[str, Any], collected_at: dt.datetime
) -> list[FinancialFundamentalsUpsert]:
    """yfinance annual income-statement ``data`` → annual upsert rows (sorted by date).

    ``data`` is the ``{period_date: {row_label: value}}`` mapping. Robust
    label-matching per field; fail-closed (skip a period with no revenue AND no
    net_income); filing_date = period_end + 90d (PIT, no look-ahead).
    """
    out: list[FinancialFundamentalsUpsert] = []
    for key, period_data in (data or {}).items():
        if not isinstance(period_data, dict):
            continue
        period_end = _parse_period_end(str(key))
        if period_end is None:
            continue
        revenue = _first_present(period_data, _REVENUE_LABELS)
        net_income = _first_present(period_data, _NET_INCOME_LABELS)
        if revenue is None and net_income is None:
            continue  # fail-closed: nothing usable for this period
        gross_profit = _first_present(period_data, _GROSS_PROFIT_LABELS)
        cost_of_sales = _first_present(period_data, _COST_OF_SALES_LABELS)
        filing_date = period_end + dt.timedelta(days=_ANNUAL_FILING_LAG_DAYS)
        out.append(
            FinancialFundamentalsUpsert(
                market="us",
                symbol=symbol.upper(),
                fiscal_period=f"{period_end.year}A",
                period_type="annual",
                period_end_date=period_end,
                filing_date=filing_date,
                effective_at=filing_date,
                source="yfinance",
                source_collected_at=collected_at,
                currency="USD",
                revenue=revenue,
                net_income=net_income,
                gross_profit=gross_profit,
                cost_of_sales=cost_of_sales,
                # annual: the discrete (single-period) facts equal the reported ones.
                discrete_revenue=revenue,
                discrete_net_income=net_income,
                data_state="fresh",
                raw_payload={
                    "income_statement": {str(k): str(v) for k, v in period_data.items()}
                },
            )
        )
    out.sort(key=lambda p: p.period_end_date)
    return out


async def fetch_us_annual_fundamentals(
    *, symbol: str, collected_at: dt.datetime
) -> list[FinancialFundamentalsUpsert]:
    """Fetch yfinance annual income statement for ``symbol`` and parse to upsert rows.

    Returns [] (not an error) when yfinance has no data — fail-closed. The DB write
    + operator/CLI orchestration is a follow-up; this returns the parsed rows.
    """
    from app.mcp_server.tooling.fundamentals_sources_yfinance import (
        _fetch_financials_yfinance,
    )

    try:
        payload = await _fetch_financials_yfinance(symbol, "income", "annual")
    except Exception as exc:  # noqa: BLE001 — yfinance optional; fail-closed empty
        logger.warning("US fundamentals fetch failed symbol=%s: %s", symbol, exc)
        return []
    return parse_us_annual_income_periods(
        symbol=symbol, data=payload.get("data") or {}, collected_at=collected_at
    )


# --- ROB-441 PR2: build orchestration (resolve → fetch → optional commit) -----

_UsFetcher = Callable[..., Awaitable[list[FinancialFundamentalsUpsert]]]


@dataclass(frozen=True)
class UsFundamentalsBuildResult:
    symbols_resolved: int
    snapshots_built: int
    committed: bool
    samples: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


async def resolve_us_symbols(
    *, override: Sequence[str], limit: int, all_symbols: bool
) -> list[str]:
    """US common-stock universe (is_common_stock + is_active), or the override list."""
    if override:
        return [s.strip().upper() for s in override if s.strip()]
    from app.core.db import AsyncSessionLocal
    from app.models.us_symbol_universe import USSymbolUniverse

    async with AsyncSessionLocal() as session:
        stmt = (
            sa.select(USSymbolUniverse.symbol)
            .where(
                USSymbolUniverse.is_active.is_(True),
                USSymbolUniverse.is_common_stock.is_(True),
            )
            .order_by(USSymbolUniverse.symbol)
        )
        if not all_symbols:
            stmt = stmt.limit(limit)
        return [r[0] for r in (await session.execute(stmt)).all()]


def _sample(p: FinancialFundamentalsUpsert) -> dict[str, Any]:
    return {
        "symbol": p.symbol,
        "fiscal_period": p.fiscal_period,
        "period_end_date": p.period_end_date.isoformat(),
        "filing_date": p.filing_date.isoformat() if p.filing_date else None,
        "revenue": str(p.revenue) if p.revenue is not None else None,
        "net_income": str(p.net_income) if p.net_income is not None else None,
    }


async def build_us_fundamentals_for_symbols(
    symbols: Sequence[str],
    *,
    commit: bool = False,
    concurrency: int = 4,
    collected_at: dt.datetime | None = None,
    fetcher: _UsFetcher | None = None,
) -> UsFundamentalsBuildResult:
    """Fetch + parse US annual fundamentals for ``symbols``; write only if commit.

    dry-run by default (commit=False): fetches/parses + reports counts, no DB write.
    commit=True upserts via the repository (operator-approved). Fail-closed: a symbol
    whose fetch fails or yields no rows is warned and skipped (never fabricated).
    """
    collected_at = collected_at or dt.datetime.now(dt.UTC)
    fetch = fetcher or fetch_us_annual_fundamentals
    sem = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []

    async def _one(sym: str) -> list[FinancialFundamentalsUpsert]:
        async with sem:
            try:
                rows = await fetch(symbol=sym, collected_at=collected_at)
            except Exception as exc:  # noqa: BLE001 — fail-closed per symbol
                warnings.append(f"{sym}: fetch failed ({exc})")
                return []
            if not rows:
                warnings.append(f"{sym}: no US fundamentals rows")
            return rows

    gathered = await asyncio.gather(*[_one(s) for s in symbols])
    payloads: list[FinancialFundamentalsUpsert] = [p for rows in gathered for p in rows]

    committed = False
    if commit and payloads:
        from app.core.db import AsyncSessionLocal
        from app.services.financial_fundamentals_snapshots.repository import (
            FinancialFundamentalsSnapshotsRepository,
        )

        async with AsyncSessionLocal() as session:
            await FinancialFundamentalsSnapshotsRepository(session).upsert(payloads)
        committed = True

    return UsFundamentalsBuildResult(
        symbols_resolved=len(symbols),
        snapshots_built=len(payloads),
        committed=committed,
        samples=tuple(_sample(p) for p in payloads[:5]),
        warnings=tuple(warnings),
    )
