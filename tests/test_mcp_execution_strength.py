"""ROB-462: get_execution_strength MCP tool + KIS broker fetch (KR equity)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import market_data_quotes


@pytest.mark.asyncio
async def test_inquire_execution_strength_extracts_cttr():
    from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin

    md = DomesticMarketDataMixin.__new__(DomesticMarketDataMixin)
    md._kis_url = lambda path: path
    md._request_with_token_retry = AsyncMock(
        return_value={
            "output": {
                "stck_shrn_iscd": "005930",
                "cttr": "120.3",
                "shnu_cntg_qty": "10",
                "seln_cntg_qty": "5",
                "stck_prpr": "80000",
                "acml_vol": "1000",
                "stck_cntg_hour": "100000",
            }
        }
    )

    raw = await md.inquire_execution_strength("005930")

    assert raw["symbol"] == "005930"
    assert raw["cttr"] == "120.3"
    assert raw["shnu_cntg_qty"] == "10"
    assert raw["seln_cntg_qty"] == "5"
    md._request_with_token_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_facade_delegates_execution_strength():
    from app.services.brokers.kis.client import KISClient

    client = KISClient.__new__(KISClient)
    client._market_data = AsyncMock()
    client._market_data.inquire_execution_strength = AsyncMock(
        return_value={"cttr": "120.3"}
    )

    raw = await client.inquire_execution_strength("005930", market="J")

    assert raw == {"cttr": "120.3"}
    client._market_data.inquire_execution_strength.assert_awaited_once_with(
        "005930", "J"
    )


@pytest.mark.asyncio
async def test_get_execution_strength_kr_returns_strength(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {
                "symbol": code,
                "cttr": "135.5",
                "shnu_cntg_qty": "1200",
                "seln_cntg_qty": "800",
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["symbol"] == "005930"
    assert result["execution_strength_pct"] == pytest.approx(135.5)
    assert result["trend"] == "buy_dominant"
    assert result["buy_volume"] == pytest.approx(1200.0)
    assert result["sell_volume"] == pytest.approx(800.0)
    assert result["data_state"] == "fresh"
    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert result["as_of"]


@pytest.mark.asyncio
async def test_get_execution_strength_tags_premarket_data_state(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"cttr": "88.0"}

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "premarket_unavailable",
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["data_state"] == "premarket_unavailable"
    assert result["trend"] == "sell_dominant"


@pytest.mark.asyncio
async def test_get_execution_strength_rejects_non_kr():
    result = await market_data_quotes._get_execution_strength_impl("AAPL", "us")
    assert result.get("error") or result.get("success") is False


def test_execution_strength_tool_registered():
    assert "get_execution_strength" in market_data_quotes.MARKET_DATA_TOOL_NAMES
