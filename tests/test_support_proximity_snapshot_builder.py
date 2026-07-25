from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.invest_screener_snapshots import support_proximity_builder as builder
from app.services.market_valuation_snapshots.normalized_market_cap import (
    NormalizedMarketCap,
)


def _candidate() -> builder.SupportProximityCandidate:
    return builder.SupportProximityCandidate(
        symbol="005930",
        market_cap=NormalizedMarketCap(
            value=Decimal("60300000000000"),
            snapshot_date=dt.date(2026, 7, 20),
            source="naver_finance",
        ),
        proxy_distance_pct=1.0,
    )


@pytest.mark.asyncio
async def test_builder_cuts_future_bar_and_freezes_one_frame(monkeypatch):
    """The support engine receives exactly the as-of frame used for latest_close."""

    frame = pd.DataFrame(
        {
            "date": list(pd.date_range("2026-05-01", periods=59, freq="D"))
            + [pd.Timestamp("2026-07-20"), pd.Timestamp("2026-07-21")],
            "open": [49000.0] * 61,
            "high": [51000.0] * 61,
            "low": [48000.0] * 61,
            "close": [49000.0] * 59 + [50000.0, 80000.0],
            "volume": [1_000_000] * 61,
        }
    )
    fetch = AsyncMock(return_value=frame)
    monkeypatch.setattr(builder, "_fetch_ohlcv_for_indicators", fetch)

    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module

    async def _support(symbol, market=None, preloaded_df=None):
        assert symbol == "005930"
        assert market == "kr"
        assert preloaded_df is not None
        assert preloaded_df["date"].iloc[-1].date() == dt.date(2026, 7, 20)
        assert float(preloaded_df["close"].iloc[-1]) == 50000.0
        return {
            "current_price": 50000.0,
            "supports": [
                {
                    "price": 49000.0,
                    "sources": ["bb_lower", "fib_0.618"],
                    "strength": "strong",
                    # Deliberately wrong: the builder recomputes against its
                    # frozen price instead of trusting a provider distance.
                    "distance_pct": -99.0,
                }
            ],
            "resistances": [],
        }

    monkeypatch.setattr(sr_module, "get_support_resistance_impl", _support)

    payload = await builder.build_support_proximity_snapshot_for_candidate(
        _candidate(),
        now=dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC),
        clock=lambda: dt.datetime(2026, 7, 20, 12, 1, tzinfo=dt.UTC),
    )

    assert payload is not None
    assert payload.snapshot_date == dt.date(2026, 7, 20)
    assert payload.latest_close == Decimal("50000.0")
    assert payload.support_price == Decimal("49000.0")
    assert payload.dist_to_support_pct == Decimal("2.0000")
    assert payload.market_cap == Decimal("60300000000000")
    assert payload.market_cap_source == "naver_finance"
    assert payload.daily_turnover == Decimal("50000000000.0")
    assert payload.support_computed_at == dt.datetime(2026, 7, 20, 12, 1, tzinfo=dt.UTC)
    fetch.assert_awaited_once_with("005930", "equity_kr", count=60)


@pytest.mark.asyncio
async def test_builder_persists_honest_no_support_as_null(monkeypatch):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-06-01", periods=40, freq="D"),
            "high": [51000.0] * 40,
            "low": [48000.0] * 40,
            "close": [50000.0] * 40,
            "volume": [1_000_000] * 40,
        }
    )
    monkeypatch.setattr(
        builder, "_fetch_ohlcv_for_indicators", AsyncMock(return_value=frame)
    )

    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module

    monkeypatch.setattr(
        sr_module,
        "get_support_resistance_impl",
        AsyncMock(return_value={"current_price": 50000.0, "supports": []}),
    )

    payload = await builder.build_support_proximity_snapshot_for_candidate(
        _candidate(),
        now=dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC),
        clock=lambda: dt.datetime(2026, 7, 20, 12, 1, tzinfo=dt.UTC),
    )

    assert payload is not None
    assert payload.support_price is None
    assert payload.dist_to_support_pct is None
    assert payload.support_computed_at == dt.datetime(2026, 7, 20, 12, 1, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_builder_skips_support_engine_failure_instead_of_marking_no_match(
    monkeypatch,
):
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-06-01", periods=40, freq="D"),
            "high": [51000.0] * 40,
            "low": [48000.0] * 40,
            "close": [50000.0] * 40,
            "volume": [1_000_000] * 40,
        }
    )
    monkeypatch.setattr(
        builder, "_fetch_ohlcv_for_indicators", AsyncMock(return_value=frame)
    )

    import app.mcp_server.tooling.fundamentals._support_resistance as sr_module

    monkeypatch.setattr(
        sr_module,
        "get_support_resistance_impl",
        AsyncMock(return_value={"error": True, "message": "calculation failed"}),
    )

    payload = await builder.build_support_proximity_snapshot_for_candidate(
        _candidate(),
        now=dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC),
    )

    assert payload is None


def test_candidate_pool_has_hard_upper_bound():
    assert builder.DEFAULT_CANDIDATE_POOL_LIMIT <= builder.MAX_CANDIDATE_POOL_LIMIT
    assert builder.MAX_CANDIDATE_POOL_LIMIT == 100
