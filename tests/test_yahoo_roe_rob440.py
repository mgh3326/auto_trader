"""ROB-440 (Phase 1): yahoo client supplies ROE (percent) so US high_yield_value
(already US-active, ROB-427 PR3) stops being empty due to roe=null.

yfinance returnOnEquity is a fraction (0.35); the screener (roe >= 15) + KR Naver
ROE are percent. Convert ×100; null stays fail-closed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.brokers.yahoo.client import _roe_to_percent


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        (0.35, 35.0),
        (0.155, 15.5),
        (0.0, 0.0),
        (-0.1, -10.0),
        (None, None),
        (True, None),  # bool is not a real ROE
        ("n/a", None),
    ],
)
def test_roe_to_percent(raw, expected) -> None:
    assert _roe_to_percent(raw) == expected


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.brokers.yahoo.client.yf.Ticker")
async def test_fetch_fundamental_info_includes_roe_percent(
    mock_ticker_class, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
        lambda: object(),
    )
    mock_ticker = MagicMock()
    mock_ticker.info = {
        "trailingPE": 8.0,
        "priceToBook": 0.9,
        "trailingEps": 1000.0,
        "bookValue": 9000.0,
        "trailingAnnualDividendYield": 0.04,
        "returnOnEquity": 0.22,
    }
    mock_ticker_class.return_value = mock_ticker

    from app.services.brokers.yahoo.client import fetch_fundamental_info

    result = await fetch_fundamental_info("AAPL")
    assert result["ROE"] == 22.0  # 0.22 fraction → 22.0 percent
    assert result["PER"] == 8.0


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.brokers.yahoo.client.yf.Ticker")
async def test_fetch_fundamental_info_roe_none_when_missing(
    mock_ticker_class, monkeypatch
) -> None:
    monkeypatch.setattr(
        "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
        lambda: object(),
    )
    mock_ticker = MagicMock()
    mock_ticker.info = {"trailingPE": 8.0}  # no returnOnEquity → fail-closed null
    mock_ticker_class.return_value = mock_ticker

    from app.services.brokers.yahoo.client import fetch_fundamental_info

    result = await fetch_fundamental_info("AAPL")
    assert result["ROE"] is None


# --- ROB-440 PR3: 52-week-high date (for US undervalued_breakout date-recency) ---


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_52w_high_date_picks_max_high() -> None:
    import datetime as dt

    import pandas as pd

    from app.services.brokers.yahoo import client as yclient

    df = pd.DataFrame(
        {
            "date": [dt.date(2026, 5, 1), dt.date(2026, 5, 20), dt.date(2026, 6, 1)],
            "high": [90.0, 110.0, 100.0],  # max high on 2026-05-20
            "low": [80.0, 100.0, 95.0],
            "close": [88.0, 108.0, 99.0],
        }
    )

    async def _fake_ohlcv(ticker, days=100, period="day", end_date=None):  # noqa: ANN001
        return df

    with patch.object(yclient, "fetch_ohlcv", _fake_ohlcv):
        result = await yclient.fetch_52w_high_date("AAPL")
    assert result == dt.date(2026, 5, 20)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_52w_high_date_fail_closed() -> None:
    import pandas as pd

    from app.services.brokers.yahoo import client as yclient

    async def _empty(ticker, days=100, period="day", end_date=None):  # noqa: ANN001
        return pd.DataFrame()

    async def _boom(ticker, days=100, period="day", end_date=None):  # noqa: ANN001
        raise RuntimeError("yfinance down")

    with patch.object(yclient, "fetch_ohlcv", _empty):
        assert await yclient.fetch_52w_high_date("AAPL") is None
    with patch.object(yclient, "fetch_ohlcv", _boom):
        assert await yclient.fetch_52w_high_date("AAPL") is None
