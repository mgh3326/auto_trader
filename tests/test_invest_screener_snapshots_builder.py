import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services.invest_screener_snapshots.builder import (
    build_snapshot_for_symbol,
    derive_metrics,
)
from app.services.invest_view_model.screener_service import (
    calculate_consecutive_up_days,
)


def test_derive_metrics_full_window():
    closes = [Decimal(c) for c in [100, 101, 102, 103, 104, 105, 106, 107, 108, 110]]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == 9
    assert metrics.latest_close == Decimal("110")
    assert metrics.prev_close == Decimal("108")
    assert metrics.change_amount == Decimal("2")
    assert round(metrics.change_rate, 4) == Decimal("1.8519")
    # week_change uses closes[-6] = 105 → (110-105)/105*100
    assert round(metrics.week_change_rate, 4) == Decimal("4.7619")


def test_derive_metrics_streak_matches_view_model():
    closes = [Decimal(c) for c in [99, 100, 99, 100, 101, 102]]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == calculate_consecutive_up_days(
        [float(c) for c in closes]
    )


def test_derive_metrics_short_window_returns_partial():
    closes = [Decimal("100"), Decimal("101")]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days == 1
    assert metrics.week_change_rate is None  # < 6 elements


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_kr(monkeypatch):
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-29", periods=10),
            "close": [100, 101, 102, 103, 104, 105, 106, 107, 108, 110],
            "volume": [1_000_000] * 10,
        }
    )
    fetcher = AsyncMock(return_value=df)
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        fetcher,
    )

    payload = await build_snapshot_for_symbol(
        market="kr", symbol="005930", today=dt.date(2026, 5, 9)
    )
    assert payload is not None
    assert payload.market == "kr"
    assert payload.symbol == "005930"
    assert payload.snapshot_date == dt.date(2026, 5, 8)  # latest row in df
    assert payload.consecutive_up_days == 9
    assert payload.daily_volume == 1_000_000
    assert payload.source == "kis"
    fetcher.assert_awaited_once_with("005930", "equity_kr", count=10)


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_returns_none_on_empty_df(monkeypatch):
    fetcher = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        fetcher,
    )
    assert (
        await build_snapshot_for_symbol(market="us", symbol="AAPL", today=dt.date.today())
        is None
    )
