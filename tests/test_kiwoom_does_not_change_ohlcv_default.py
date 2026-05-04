# tests/test_kiwoom_does_not_change_ohlcv_default.py
"""Regression guard: ROB-97 must not change the default KR OHLCV source."""

from __future__ import annotations

from pathlib import Path

MARKET_DATA_QUOTES = (
    Path(__file__).parent.parent
    / "app"
    / "mcp_server"
    / "tooling"
    / "market_data_quotes.py"
)


def test_market_data_quotes_does_not_import_kiwoom():
    text = MARKET_DATA_QUOTES.read_text(encoding="utf-8")
    assert "kiwoom" not in text.lower(), (
        "ROB-97 explicitly defers Kiwoom OHLCV; market_data_quotes.py must "
        "remain on the KIS path."
    )


def test_kiwoom_market_data_skeleton_raises_on_use():
    import asyncio

    from app.services.brokers.kiwoom.domestic_market_data import (
        KiwoomDomesticMarketDataClient,
    )

    client = KiwoomDomesticMarketDataClient()

    async def _call():
        await client.fetch_daily_candles()

    import pytest

    with pytest.raises(NotImplementedError):
        asyncio.run(_call())
