"""ROB-429 A1 — provider full-universe fetch semantics.

The bug: ``fetch_rows(limit=None)`` (full mode) collapsed to 200 because
``query_limit = limit or 200`` + ``return rows[:query_limit]``. tvscreener's
``query_stock_screener(limit=None)`` also does NOT fetch everything — when
``limit`` is falsy it skips ``set_range`` and TradingView returns its small
default range (~150). So the full-universe path MUST pass a large explicit bound.

These tests mock the tvscreener import + service so they never hit the network.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.services.invest_kr_fundamentals_snapshots import provider as provider_mod
from app.services.invest_kr_fundamentals_snapshots.provider import (
    _FULL_UNIVERSE_FETCH_CAP,
    TvScreenerKrFundamentalsProvider,
)


class _FakeStockField:
    # The provider resolves columns via _get_tvscreener_attr(stock_field, *names);
    # any non-None attribute works because the fake service ignores the columns.
    ACTIVE_SYMBOL = "active_symbol"
    SYMBOL = "symbol"
    NAME = "name"
    DESCRIPTION = "description"
    PRICE = "price"
    CLOSE = "close"
    CHANGE_PERCENT = "change_percent"
    VOLUME = "volume"
    MARKET_CAPITALIZATION = "market_capitalization"
    MARKET_CAP_BASIC = "market_cap_basic"
    PRICE_TO_EARNINGS_RATIO_TTM = "price_to_earnings_ratio_ttm"
    PRICE_TO_EARNINGS_TTM = "price_to_earnings_ttm"
    PRICE_TO_BOOK_FQ = "price_to_book_fq"
    PRICE_TO_BOOK_MRQ = "price_to_book_mrq"
    PRICE_BOOK_CURRENT = "price_book_current"
    DIVIDEND_YIELD_FORWARD = "dividend_yield_forward"
    DIVIDENDS_YIELD_CURRENT = "dividends_yield_current"
    DIVIDEND_YIELD_RECENT = "dividend_yield_recent"
    DIVIDEND_YIELD_CURRENT = "dividend_yield_current"
    RETURN_ON_EQUITY_TTM = "return_on_equity_ttm"
    RETURN_ON_EQUITY_FY = "return_on_equity_fy"
    DIVIDEND_PAYOUT_RATIO_TTM = "dividend_payout_ratio_ttm"
    DIVIDEND_PAYOUT_RATIO_PERCENT_TTM = "dividend_payout_ratio_percent_ttm"
    DIVIDEND_PAYOUT_RATIO_FY = "dividend_payout_ratio_fy"
    GROSS_MARGIN_TTM = "gross_margin_ttm"
    GROSS_MARGIN_PERCENT_TTM = "gross_margin_percent_ttm"
    REVENUE_ANNUAL_YOY_GROWTH = "revenue_annual_yoy_growth"
    EPS_DILUTED_ANNUAL_YOY_GROWTH = "eps_diluted_annual_yoy_growth"
    EPS_DILUTED_QUARTERLY_QOQ_GROWTH = "eps_diluted_quarterly_qoq_growth"
    NET_INCOME_ANNUAL_YOY_GROWTH = "net_income_annual_yoy_growth"
    NET_INCOME_CAGR_5Y = "net_income_cagr_5y"
    CONTINUOUS_DIVIDEND_PAYOUT = "continuous_dividend_payout"
    CONTINUOUS_DIVIDEND_GROWTH = "continuous_dividend_growth"
    WEEK_HIGH_52 = "52_week_high"
    RELATIVE_STRENGTH_INDEX_14 = "relative_strength_index_14"
    SECTOR = "sector"
    INDUSTRY = "industry"


class _FakeMarket:
    KOREA = "korea"


class _FakeTvscreener:
    StockField = _FakeStockField
    Market = _FakeMarket


def _fake_df(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"KRX:{600000 + i:06d}" for i in range(n)],
            "price": [1000.0 + i for i in range(n)],
        }
    )


class _CapturingService:
    """Captures the `limit` query_stock_screener was called with and returns a
    DataFrame sized to that limit (mimicking a server that honors set_range)."""

    last_limit: int | None = None

    def __init__(self, *, full_rows: int) -> None:
        self._full_rows = full_rows

    async def query_stock_screener(
        self, *, columns: Any, markets: Any, limit: int | None = None
    ) -> pd.DataFrame:
        type(self).last_limit = limit
        # Server returns min(limit, full_rows) rows when a bound is given; when no
        # bound is given it would return the small default — but the provider must
        # always pass an explicit large bound in full mode, so we size to limit.
        n = min(limit, self._full_rows) if limit else 150
        return _fake_df(n)


@pytest.fixture
def _patch_tvscreener(monkeypatch):
    monkeypatch.setattr(provider_mod, "_import_tvscreener", lambda: _FakeTvscreener)


@pytest.mark.asyncio
async def test_fetch_rows_full_universe_passes_large_cap_not_200(
    monkeypatch, _patch_tvscreener
):
    # full universe ~4250 rows available; provider must request the large cap and
    # return ALL of them (not collapse to 200).
    full_rows = 4250
    service = _CapturingService(full_rows=full_rows)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    rows = await TvScreenerKrFundamentalsProvider().fetch_rows(limit=None)

    assert _CapturingService.last_limit == _FULL_UNIVERSE_FETCH_CAP
    assert _FULL_UNIVERSE_FETCH_CAP == 10_000
    assert _CapturingService.last_limit != 200
    # All full-universe rows returned (no 200-row slice).
    assert len(rows) == full_rows


@pytest.mark.asyncio
async def test_fetch_rows_diagnostic_limit_is_bounded(monkeypatch, _patch_tvscreener):
    service = _CapturingService(full_rows=4250)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    rows = await TvScreenerKrFundamentalsProvider().fetch_rows(limit=50)

    assert _CapturingService.last_limit == 50
    assert len(rows) == 50


@pytest.mark.asyncio
async def test_fetch_rows_zero_or_negative_limit_treated_as_full(
    monkeypatch, _patch_tvscreener
):
    # A non-positive limit must not silently shrink to a tiny range; treat as full.
    service = _CapturingService(full_rows=300)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    rows = await TvScreenerKrFundamentalsProvider().fetch_rows(limit=0)

    assert _CapturingService.last_limit == _FULL_UNIVERSE_FETCH_CAP
    assert len(rows) == 300
