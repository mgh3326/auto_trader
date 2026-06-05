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
