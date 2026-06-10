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


def test_derive_metrics_short_window_streak_is_none():
    # ROB-430 PR-①: < 6 sessions → consecutive_up_days is None (insufficient), NOT a
    # truncated lower bound that would silently fail the consecutive_gainers >= 5 filter.
    closes = [Decimal("100"), Decimal("101")]
    metrics = derive_metrics(closes)
    assert metrics.consecutive_up_days is None
    assert metrics.week_change_rate is None  # < 6 elements
    # change_rate is still computed from the latest pair (needs only 2 closes).
    assert metrics.change_rate is not None


def test_derive_metrics_streak_reliable_at_six_sessions():
    # ROB-430 PR-①: 5 all-up closes can't confirm a >= 5 streak (max 4) → None;
    # 6 all-up closes yield a reliable streak of 5 that passes the >= 5 filter.
    assert (
        derive_metrics([Decimal(c) for c in [10, 11, 12, 13, 14]]).consecutive_up_days
        is None
    )
    assert (
        derive_metrics(
            [Decimal(c) for c in [10, 11, 12, 13, 14, 15]]
        ).consecutive_up_days
        == 5
    )


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
    fetcher.assert_awaited_once_with("005930", "equity_kr", count=30)


@pytest.mark.asyncio
async def test_build_snapshot_drops_forming_intraday_bar(monkeypatch):
    """ROB-430 트랙B f/u: a forming (intraday) current-session bar is excluded so
    consecutive_up_days is computed on COMPLETED closes only (Toss "종가 기준").

    7 rising completed closes through 2026-06-03, then a DOWN forming 2026-06-04 bar.
    Intraday (before 16:20 KST) → drop 06-04 → streak intact (6). Post-close → keep
    the completed 06-04 down close → streak breaks (0).
    """
    df = pd.DataFrame(
        {
            "date": [
                dt.date(2026, 5, 26),
                dt.date(2026, 5, 27),
                dt.date(2026, 5, 28),
                dt.date(2026, 5, 29),
                dt.date(2026, 6, 1),
                dt.date(2026, 6, 2),
                dt.date(2026, 6, 3),
                dt.date(2026, 6, 4),
            ],
            "close": [100, 101, 102, 103, 104, 105, 106, 90],
            "volume": [1_000_000] * 8,
        }
    )
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    # 06-04 10:00 KST (01:00 UTC) — before 16:20 KST → forming 06-04 bar dropped.
    intraday = await build_snapshot_for_symbol(
        market="kr",
        symbol="005930",
        today=dt.date(2026, 6, 4),
        now=dt.datetime(2026, 6, 4, 1, 0, tzinfo=dt.UTC),
    )
    assert intraday is not None
    assert intraday.snapshot_date == dt.date(2026, 6, 3)
    assert intraday.consecutive_up_days == 6  # streak intact through the last close

    # 06-04 17:00 KST (08:00 UTC) — after 16:20 KST → completed 06-04 down close kept.
    post_close = await build_snapshot_for_symbol(
        market="kr",
        symbol="005930",
        today=dt.date(2026, 6, 4),
        now=dt.datetime(2026, 6, 4, 8, 0, tzinfo=dt.UTC),
    )
    assert post_close is not None
    assert post_close.snapshot_date == dt.date(2026, 6, 4)
    assert post_close.consecutive_up_days == 0  # the down close ends the run


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_accepts_python_date_cells(monkeypatch):
    df = pd.DataFrame(
        {
            "date": [dt.date(2026, 5, day) for day in range(1, 11)],
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
        market="kr", symbol="005930", today=dt.date(2026, 5, 10)
    )

    assert payload is not None
    assert payload.snapshot_date == dt.date(2026, 5, 10)


@pytest.mark.asyncio
async def test_build_snapshot_for_symbol_returns_none_on_empty_df(monkeypatch):
    fetcher = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        fetcher,
    )
    assert (
        await build_snapshot_for_symbol(
            market="us", symbol="AAPL", today=dt.date.today()
        )
        is None
    )


def test_lookback_supports_rsi14():
    """ROB-512: build_rsi14_from_closes는 최소 15종가가 필요하다. _LOOKBACK이
    그 밑이면 closes_window 기반 RSI enrichment가 전 심볼에서 구조적으로 None이
    된다(rsiSucceeded=0 회귀 가드)."""
    from app.services.invest_screener_snapshots import builder

    assert builder._LOOKBACK >= 15


@pytest.mark.asyncio
async def test_build_snapshot_stores_rsi_capable_closes_window(monkeypatch):
    """ROB-512: 30세션 OHLCV가 주어지면 closes_window에 15개 이상 저장되고,
    저장된 윈도우만으로 RSI14가 계산 가능해야 한다."""
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-04-01", periods=30),
            "close": [100.0 + (i % 3) for i in range(30)],
            "volume": [1_000_000] * 30,
        }
    )
    monkeypatch.setattr(
        "app.services.invest_screener_snapshots.builder._fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df),
    )

    payload = await build_snapshot_for_symbol(
        market="kr", symbol="005930", today=dt.date(2026, 5, 9)
    )
    assert payload is not None
    assert len(payload.closes_window) >= 15

    from app.services.invest_view_model.screener_analysis_enrichment import (
        build_rsi14_from_closes,
    )

    assert build_rsi14_from_closes(payload.closes_window) is not None
