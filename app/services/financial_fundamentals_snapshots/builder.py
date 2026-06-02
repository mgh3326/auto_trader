from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

# XBRL account_id codes (preferred) with Korean account_nm contains-fallbacks.
_REVENUE_IDS = ("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers")
_GROSS_PROFIT_IDS = ("ifrs-full_GrossProfit",)
_COST_OF_SALES_IDS = ("ifrs-full_CostOfSales",)
_NET_INCOME_IDS = ("ifrs-full_ProfitLoss",)

_REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
_GROSS_PROFIT_NAMES = ("매출총이익",)
_COST_OF_SALES_NAMES = ("매출원가",)
_NET_INCOME_NAMES = ("당기순이익", "당기순이익(손실)")


def _dart_amount_to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "nan"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_label(value: Any) -> str:
    if value is None:
        return ""
    return "".join(str(value).split()).replace("(", "").replace(")", "")


def _pick_amount(
    df: pd.DataFrame, *, account_ids: tuple[str, ...], account_names: tuple[str, ...]
) -> Decimal | None:
    if df.empty:
        return None
    for _, row in df.iterrows():
        if str(row.get("account_id", "")).strip() in account_ids:
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    # Fallback: normalized-contains match on the Korean account name.
    targets = {_normalize_label(name) for name in account_names}
    for _, row in df.iterrows():
        label = _normalize_label(row.get("account_nm"))
        if any(target in label for target in targets):
            return _dart_amount_to_decimal(row.get("thstrm_amount"))
    return None


def parse_income_statement_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract revenue / net_income / gross_profit / cost_of_sales from a finstate_all frame."""
    return {
        "revenue": _pick_amount(df, account_ids=_REVENUE_IDS, account_names=_REVENUE_NAMES),
        "gross_profit": _pick_amount(df, account_ids=_GROSS_PROFIT_IDS, account_names=_GROSS_PROFIT_NAMES),
        "cost_of_sales": _pick_amount(df, account_ids=_COST_OF_SALES_IDS, account_names=_COST_OF_SALES_NAMES),
        "net_income": _pick_amount(df, account_ids=_NET_INCOME_IDS, account_names=_NET_INCOME_NAMES),
    }


def _pick_dividend_row(df: pd.DataFrame, *, contains: str) -> Decimal | None:
    target = _normalize_label(contains)
    for _, row in df.iterrows():
        if target in _normalize_label(row.get("se")):
            return _dart_amount_to_decimal(row.get("thstrm"))
    return None


def parse_dividend_frame(df: pd.DataFrame) -> dict[str, Decimal | None]:
    """Extract payout_ratio (현금배당성향%) and dividend_per_share (주당 현금배당금) from alotMatter."""
    if df.empty:
        return {"payout_ratio": None, "dividend_per_share": None}
    return {
        "payout_ratio": _pick_dividend_row(df, contains="현금배당성향"),
        "dividend_per_share": _pick_dividend_row(df, contains="주당현금배당금"),
    }


def _parse_dart_date(value: Any) -> dt.date | None:
    text = str(value).strip().replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_filing_dates_frame(df: pd.DataFrame) -> dict[str, dt.date]:
    """Map rcept_no -> filing date (rcept_dt) from a disclosure-list frame."""
    mapping: dict[str, dt.date] = {}
    if df.empty:
        return mapping
    for _, row in df.iterrows():
        rcept_no = str(row.get("rcept_no", "")).strip()
        filing_date = _parse_dart_date(row.get("rcept_dt"))
        if rcept_no and filing_date is not None:
            mapping[rcept_no] = filing_date
    return mapping


def single_quarter_discrete(
    *, cumulative: Decimal | None, prior_cumulative: Decimal | None
) -> Decimal | None:
    """Standalone single-quarter value from KR YTD-cumulative interim amounts.

    Q1 cumulative == standalone (prior_cumulative is None). Later quarters subtract the
    prior cumulative. A missing current cumulative cannot be differenced.
    """
    if cumulative is None:
        return None
    if prior_cumulative is None:
        return cumulative
    return cumulative - prior_cumulative
