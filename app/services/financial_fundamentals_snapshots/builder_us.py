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

import datetime as dt
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

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
