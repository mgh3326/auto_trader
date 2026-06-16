from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.services.market_valuation_snapshots import us_provider as provider_mod
from app.services.market_valuation_snapshots.builder import (
    build_valuation_snapshots_bulk_for_us,
    build_valuation_snapshots_for_market,
)
from app.services.market_valuation_snapshots.us_provider import (
    _FULL_UNIVERSE_FETCH_CAP,
    TvScreenerUsValuationProvider,
)


class _FakeStockField:
    ACTIVE_SYMBOL = "active_symbol"
    SYMBOL = "symbol"
    MARKET_CAPITALIZATION = "market_capitalization"
    MARKET_CAP_BASIC = "market_cap_basic"
    PRICE_TO_EARNINGS_RATIO_TTM = "price_to_earnings_ratio_ttm"
    PRICE_TO_EARNINGS_TTM = "price_to_earnings_ttm"
    PRICE_TO_BOOK_FQ = "price_to_book_fq"
    PRICE_TO_BOOK_MRQ = "price_to_book_mrq"
    PRICE_BOOK_CURRENT = "price_book_current"
    DIVIDENDS_YIELD = "dividend_yield"
    DIVIDEND_YIELD_FORWARD = "dividend_yield_forward"
    RETURN_ON_EQUITY_TTM = "return_on_equity_ttm"
    WEEK_HIGH_52 = "52_week_high"
    WEEK_LOW_52 = "52_week_low"
    PRICE_52_WEEK_HIGH_DATE = "price_52_week_high_date"


class _FakeMarket:
    AMERICA = "america"


class _FakeTvscreener:
    StockField = _FakeStockField
    Market = _FakeMarket


def _fake_df(n: int) -> pd.DataFrame:
    # Mimic the DataFrame structure returned by TvScreenerService after column normalization
    return pd.DataFrame(
        {
            "symbol": [f"NASDAQ:SYM{i}" for i in range(n)],
            "market_capitalization": [1000000.0 + i for i in range(n)],
            "price_to_earnings_ratio_ttm": [15.5 + i for i in range(n)],
            "price_to_book_mrq": [1.5 + (i * 0.1) for i in range(n)],
            "dividend_yield": [2.5 for i in range(n)],
            "return_on_equity_ttm": [10.0 + i for i in range(n)],
            "52_week_high": [50.0 + i for i in range(n)],
            "52_week_low": [20.0 + i for i in range(n)],
            "price_52_week_high_date": [1778765400 for i in range(n)],
        }
    )


class _CapturingService:
    last_limit: int | None = None

    def __init__(self, *, full_rows: int) -> None:
        self._full_rows = full_rows

    async def query_stock_screener(
        self, *, columns: Any, markets: Any, limit: int | None = None
    ) -> pd.DataFrame:
        type(self).last_limit = limit
        n = min(limit, self._full_rows) if limit else 150
        return _fake_df(n)


@pytest.fixture
def _patch_tvscreener(monkeypatch):
    monkeypatch.setattr(provider_mod, "_import_tvscreener", lambda: _FakeTvscreener)


@pytest.mark.asyncio
async def test_fetch_rows_full_universe_cap(monkeypatch, _patch_tvscreener):
    full_rows = 5000
    service = _CapturingService(full_rows=full_rows)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    rows = await TvScreenerUsValuationProvider().fetch_rows(limit=None)

    assert _CapturingService.last_limit == _FULL_UNIVERSE_FETCH_CAP
    assert _FULL_UNIVERSE_FETCH_CAP == 12000
    assert len(rows) == full_rows

    # Verify key mapping
    first_row = rows[0]
    assert first_row["symbol"] == "NASDAQ:SYM0"
    assert first_row["market_cap_basic"] == 1000000.0
    assert first_row["price_earnings_ttm"] == 15.5
    assert first_row["price_book_ratio"] == 1.5
    assert first_row["dividends_yield"] == 2.5
    assert first_row["return_on_equity"] == 10.0
    assert first_row["price_52_week_high"] == 50.0
    assert first_row["price_52_week_low"] == 20.0
    # ROB-590: provider emits an ISO date STRING (JSON-safe boundary), not a
    # dt.date object — so raw_payload stays Postgres-jsonb serializable.
    assert first_row["price_52_week_high_date"] == "2026-05-14"


@pytest.mark.asyncio
async def test_fetch_rows_diagnostic_limit(monkeypatch, _patch_tvscreener):
    service = _CapturingService(full_rows=5000)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    rows = await TvScreenerUsValuationProvider().fetch_rows(limit=10)

    assert _CapturingService.last_limit == 10
    assert len(rows) == 10


@pytest.mark.asyncio
async def test_build_valuation_snapshots_bulk_for_us(monkeypatch, _patch_tvscreener):
    service = _CapturingService(full_rows=5)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    snapshot_date = dt.date(2026, 6, 16)
    result = await build_valuation_snapshots_bulk_for_us(
        snapshot_date=snapshot_date, limit=5
    )

    assert len(result.payloads) == 5
    p = result.payloads[0]
    assert p.market == "us"
    assert p.symbol == "SYM0"
    assert p.snapshot_date == snapshot_date
    assert p.source == "tvscreener"
    assert p.per == Decimal("15.5")
    assert p.pbr == Decimal("1.5")
    assert p.roe == Decimal("10.0")
    # ROB-590: tvscreener dividend_yield is a PERCENT (2.5 = 2.5%); the column
    # stores a RATIO, so it must be divided by 100.
    assert p.dividend_yield == Decimal("0.025")
    assert p.market_cap == Decimal("1000000")
    assert p.high_52w == Decimal("50.0")
    assert p.low_52w == Decimal("20.0")
    assert p.high_52w_date == dt.date(2026, 5, 14)


@pytest.mark.asyncio
async def test_build_valuation_snapshots_for_market_routing(
    monkeypatch, _patch_tvscreener
):
    service = _CapturingService(full_rows=10)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    snapshot_date = dt.date(2026, 6, 16)

    # With use_bulk=True, no symbols filtering (returns all bulk rows)
    result = await build_valuation_snapshots_for_market(
        market="us",
        symbols=[],
        snapshot_date=snapshot_date,
        use_bulk=True,
    )
    assert len(result.payloads) == 10
    assert all(p.source == "tvscreener" for p in result.payloads)

    # With use_bulk=True, and specific symbols filtering
    result_filtered = await build_valuation_snapshots_for_market(
        market="us",
        symbols=["SYM1", "SYM3", "SYM999"],
        snapshot_date=snapshot_date,
        use_bulk=True,
    )
    assert len(result_filtered.payloads) == 2
    symbols_returned = {p.symbol for p in result_filtered.payloads}
    assert symbols_returned == {"SYM1", "SYM3"}


def _fake_df_with_gaps() -> pd.DataFrame:
    # Non-dividend / unprofitable stocks: tvscreener returns NaN for those metrics
    # (e.g. live NVDA/GOOG dividend=NaN). raw_payload must stay strict-JSON because
    # Postgres JSONB rejects NaN tokens.
    return pd.DataFrame(
        {
            "symbol": ["NASDAQ:NODIV"],
            "market_capitalization": [4.3e12],
            "price_to_earnings_ratio_ttm": [float("nan")],
            "price_to_book_mrq": [1.3],
            "dividend_yield": [float("nan")],
            "return_on_equity_ttm": [float("nan")],
            "52_week_high": [55.97],
            "52_week_low": [48.88],
            "price_52_week_high_date": [1778765400],
        }
    )


class _FixedDfService:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def query_stock_screener(
        self, *, columns: Any, markets: Any, limit: int | None = None
    ) -> pd.DataFrame:
        return self._df


@pytest.mark.asyncio
async def test_bulk_raw_payload_is_strict_json_safe(monkeypatch, _patch_tvscreener):
    # ROB-590 bug B: NaN metrics + a date object in raw_payload broke the JSONB
    # commit (default engine serializer is json.dumps; Postgres rejects NaN). The
    # bulk path must scrub non-finite floats and serialize the date as a string.
    service = _FixedDfService(_fake_df_with_gaps())
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    result = await build_valuation_snapshots_bulk_for_us(
        snapshot_date=dt.date(2026, 6, 16), limit=5
    )

    assert len(result.payloads) == 1
    p = result.payloads[0]
    # NaN metrics -> typed columns None (never NaN Decimals)
    assert p.per is None
    assert p.dividend_yield is None
    assert p.roe is None
    # strict-JSON invariant == what Postgres JSONB accepts: no NaN tokens, no
    # non-native types (the date must already be a string).
    json.dumps(p.raw_payload, allow_nan=False)
    assert p.raw_payload["dividends_yield"] is None
    assert p.raw_payload["price_52_week_high_date"] == "2026-05-14"
    # typed date column still resolves to a real date
    assert p.high_52w_date == dt.date(2026, 5, 14)


@pytest.mark.asyncio
async def test_bulk_dividend_yield_percent_converted_to_ratio(
    monkeypatch, _patch_tvscreener
):
    # ROB-590 bug A: tvscreener dividend_yield is PERCENT (e.g. 0.36 = 0.36%); the
    # market_valuation_snapshots column stores a RATIO (parity with ROB-444 / Finnhub).
    df = _fake_df_with_gaps()
    df["dividend_yield"] = [0.36]
    service = _FixedDfService(df)
    monkeypatch.setattr(provider_mod, "TvScreenerService", lambda **kw: service)

    result = await build_valuation_snapshots_bulk_for_us(
        snapshot_date=dt.date(2026, 6, 16), limit=5
    )

    assert result.payloads[0].dividend_yield == Decimal("0.0036")
