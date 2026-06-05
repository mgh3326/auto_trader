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


# --- ROB-441 PR4: quarterly periods (QoQ → growth_expectation_toss) -----------

# 10-Q SEC deadline is ~40-45 days after quarter-end (large filers 40d).
_QUARTERLY_FILING_LAG_DAYS = 45


def _quarter_label(period_end: dt.date) -> str:
    """Calendar-quarter label 'YYYYQN' (derive._quarter_idx-compatible). Calendar
    quarters keep consecutive quarter-ends one index apart, so the QoQ adjacency
    check holds regardless of a company's fiscal-year alignment."""
    return f"{period_end.year}Q{(period_end.month - 1) // 3 + 1}"


def parse_us_quarterly_income_periods(
    *, symbol: str, data: dict[str, Any], collected_at: dt.datetime
) -> list[FinancialFundamentalsUpsert]:
    """yfinance quarterly income ``data`` → quarterly upsert rows (sorted by date).

    yfinance quarterly columns are already single-quarter (discrete), unlike DART's
    cumulative YTD filings — so discrete_* equal the reported values directly.
    fail-closed (skip a period with no revenue AND no net_income); filing_date =
    quarter_end + 45d (PIT, no look-ahead)."""
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
            continue  # fail-closed: nothing usable (QoQ needs net_income)
        gross_profit = _first_present(period_data, _GROSS_PROFIT_LABELS)
        cost_of_sales = _first_present(period_data, _COST_OF_SALES_LABELS)
        filing_date = period_end + dt.timedelta(days=_QUARTERLY_FILING_LAG_DAYS)
        out.append(
            FinancialFundamentalsUpsert(
                market="us",
                symbol=symbol.upper(),
                fiscal_period=_quarter_label(period_end),
                period_type="quarterly",
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
                discrete_revenue=revenue,  # yfinance quarterly is already discrete
                discrete_net_income=net_income,
                data_state="fresh",
                raw_payload={
                    "income_statement": {str(k): str(v) for k, v in period_data.items()}
                },
            )
        )
    out.sort(key=lambda p: p.period_end_date)
    return out


async def fetch_us_quarterly_fundamentals(
    *, symbol: str, collected_at: dt.datetime
) -> list[FinancialFundamentalsUpsert]:
    """Fetch yfinance quarterly income statement for ``symbol`` → quarterly rows.

    Returns [] (fail-closed) when yfinance has no quarterly data or errors."""
    from app.mcp_server.tooling.fundamentals_sources_yfinance import (
        _fetch_financials_yfinance,
    )

    try:
        payload = await _fetch_financials_yfinance(symbol, "income", "quarterly")
    except Exception as exc:  # noqa: BLE001 — yfinance optional; fail-closed empty
        logger.warning(
            "US quarterly fundamentals fetch failed symbol=%s: %s", symbol, exc
        )
        return []
    return parse_us_quarterly_income_periods(
        symbol=symbol, data=payload.get("data") or {}, collected_at=collected_at
    )


# --- ROB-441 PR5: dividends (→ steady_dividend / future_dividend_king) ---------

# yfinance cashflow "dividends paid" labels (a cash OUTFLOW → stored negative).
_CASHFLOW_DIVIDEND_LABELS = (
    "Cash Dividends Paid",
    "Common Stock Dividends Paid",
    "Common Stock Dividend Paid",
    "Cash Dividend Paid",
    "Dividends Paid",
)


def _dps_by_year_from_payload(data: dict[str, Any]) -> dict[int, Decimal]:
    """Dividends payload ``{year: total_dps}`` → ``{int year: Decimal dps}``."""
    out: dict[int, Decimal] = {}
    for year_str, dps in (data or {}).items():
        dec = _to_decimal(dps)
        if dec is None:
            continue
        try:
            out[int(str(year_str)[:4])] = dec
        except (ValueError, TypeError):
            continue
    return out


def parse_us_cashflow_dividends_paid(data: dict[str, Any]) -> dict[int, Decimal]:
    """yfinance cashflow ``data`` → ``{year: abs(dividends paid)}`` (total cash out)."""
    out: dict[int, Decimal] = {}
    for key, period_data in (data or {}).items():
        if not isinstance(period_data, dict):
            continue
        period_end = _parse_period_end(str(key))
        if period_end is None:
            continue
        raw = _first_present(period_data, _CASHFLOW_DIVIDEND_LABELS)
        if raw is None:
            continue
        out[period_end.year] = abs(raw)  # cashflow stores the outflow as negative
    return out


def enrich_annual_with_dividends(
    periods: list[FinancialFundamentalsUpsert],
    *,
    dps_by_year: dict[int, Decimal],
    dividends_paid_by_year: dict[int, Decimal],
) -> list[FinancialFundamentalsUpsert]:
    """Set dividend_per_share (per-share, for streak direction) + payout_ratio
    (percent = total dividends paid / net_income × 100, total-based to avoid split
    skew) on each ANNUAL period. Quarterly rows pass through. Fail-closed: a missing
    figure leaves that field None (the derive streak/payout metric → excluded)."""
    out: list[FinancialFundamentalsUpsert] = []
    for p in periods:
        if p.period_type != "annual":
            out.append(p)
            continue
        year = p.period_end_date.year
        dps = dps_by_year.get(year)
        payout: Decimal | None = None
        div_paid = dividends_paid_by_year.get(year)
        if div_paid is not None and p.net_income is not None and p.net_income > 0:
            payout = (div_paid / p.net_income) * Decimal(100)
        if dps is None and payout is None:
            out.append(p)  # nothing to enrich
            continue
        out.append(
            p.model_copy(update={"dividend_per_share": dps, "payout_ratio": payout})
        )
    return out


async def fetch_us_dividend_data(
    *, symbol: str
) -> tuple[dict[int, Decimal], dict[int, Decimal]]:
    """(dps_by_year, dividends_paid_by_year) for ``symbol`` — both fail-closed empty."""
    from app.mcp_server.tooling.fundamentals_sources_yfinance import (
        _fetch_dividends_yfinance,
        _fetch_financials_yfinance,
    )

    dps_by_year: dict[int, Decimal] = {}
    dividends_paid_by_year: dict[int, Decimal] = {}
    try:
        dv = await _fetch_dividends_yfinance(symbol)
        dps_by_year = _dps_by_year_from_payload(dv.get("data") or {})
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning("US dividends fetch failed symbol=%s: %s", symbol, exc)
    try:
        cf = await _fetch_financials_yfinance(symbol, "cashflow", "annual")
        dividends_paid_by_year = parse_us_cashflow_dividends_paid(cf.get("data") or {})
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning("US cashflow fetch failed symbol=%s: %s", symbol, exc)
    return dps_by_year, dividends_paid_by_year


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
    include_quarterly: bool = False,
    quarterly_fetcher: _UsFetcher | None = None,
    include_dividends: bool = False,
    dividend_fetcher: Callable[..., Awaitable[tuple[dict, dict]]] | None = None,
) -> UsFundamentalsBuildResult:
    """Fetch + parse US annual (and optionally quarterly) fundamentals; write if commit.

    dry-run by default (commit=False): fetches/parses + reports counts, no DB write.
    commit=True upserts via the repository (operator-approved). include_quarterly also
    builds quarterly periods (ROB-441 PR4, QoQ → growth_expectation_toss);
    include_dividends enriches annual periods with dividend_per_share + payout_ratio
    (ROB-441 PR5, → steady_dividend / future_dividend_king).
    Fail-closed: a symbol whose fetch fails or yields no rows is warned and skipped.
    """
    collected_at = collected_at or dt.datetime.now(dt.UTC)
    annual_fetch = fetcher or fetch_us_annual_fundamentals
    quarterly_fetch = quarterly_fetcher or fetch_us_quarterly_fundamentals
    dividend_fetch = dividend_fetcher or fetch_us_dividend_data
    sem = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []

    async def _one(sym: str) -> list[FinancialFundamentalsUpsert]:
        async with sem:
            rows: list[FinancialFundamentalsUpsert] = []
            annual_rows: list[FinancialFundamentalsUpsert] = []
            try:
                annual_rows = list(
                    await annual_fetch(symbol=sym, collected_at=collected_at)
                )
            except Exception as exc:  # noqa: BLE001 — fail-closed per symbol
                warnings.append(f"{sym}: annual fetch failed ({exc})")
            if include_dividends and annual_rows:
                try:
                    dps_by_year, div_paid_by_year = await dividend_fetch(symbol=sym)
                    annual_rows = enrich_annual_with_dividends(
                        annual_rows,
                        dps_by_year=dps_by_year,
                        dividends_paid_by_year=div_paid_by_year,
                    )
                except Exception as exc:  # noqa: BLE001 — fail-closed per symbol
                    warnings.append(f"{sym}: dividend fetch failed ({exc})")
            rows.extend(annual_rows)
            if include_quarterly:
                try:
                    rows.extend(
                        await quarterly_fetch(symbol=sym, collected_at=collected_at)
                    )
                except Exception as exc:  # noqa: BLE001 — fail-closed per symbol
                    warnings.append(f"{sym}: quarterly fetch failed ({exc})")
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
