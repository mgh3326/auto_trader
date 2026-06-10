"""ROB-485: get_execution_strength MCP tool + KIS broker fetch (KR equity)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import market_data_quotes


@pytest.mark.asyncio
async def test_inquire_execution_strength_uses_inquire_ccnl_tday_rltv():
    from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin

    md = DomesticMarketDataMixin.__new__(DomesticMarketDataMixin)
    md._kis_url = lambda path: path
    md._request_with_token_retry = AsyncMock(
        return_value={
            "output": [
                {
                    "stck_cntg_hour": "100001",
                    "stck_prpr": "80010",
                    "cntg_vol": "3",
                    "tday_rltv": "121.4",
                    "prdy_ctrt": "1.2",
                },
                {
                    "stck_cntg_hour": "100000",
                    "stck_prpr": "80000",
                    "cntg_vol": "5",
                    "tday_rltv": "120.3",
                    "prdy_ctrt": "1.1",
                },
            ]
        }
    )

    raw = await md.inquire_execution_strength("005930")

    assert raw["symbol"] == "005930"
    assert raw["tday_rltv"] == "121.4"
    assert raw["last_price"] == "80010"
    assert raw["cntg_vol"] == "3"
    assert raw["time"] == "100001"
    md._request_with_token_retry.assert_awaited_once_with(
        tr_id="FHKST01010300",
        url="/uapi/domestic-stock/v1/quotations/inquire-ccnl",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": "005930",
        },
        api_name="inquire_execution_strength",
    )


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
                "tday_rltv": "135.5",
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["symbol"] == "005930"
    assert result["execution_strength_pct"] == pytest.approx(135.5)
    assert result["trend"] == "buy_dominant"
    assert result["buy_volume"] is None
    assert result["sell_volume"] is None
    assert result["data_state"] == "fresh"
    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert result["as_of"]


@pytest.mark.asyncio
async def test_get_execution_strength_tags_premarket_data_state(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"tday_rltv": "88.0"}

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
async def test_get_execution_strength_null_during_session_tags_field_unavailable(
    monkeypatch,
):
    """ROB-485: a null strength while the market is open means the KIS field
    mapping broke — surface field_unavailable instead of a healthy 'fresh'."""

    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"symbol": code, "tday_rltv": None}

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["execution_strength_pct"] is None
    assert result["data_state"] == "field_unavailable"


@pytest.mark.asyncio
async def test_get_execution_strength_null_when_closed_keeps_session_state(
    monkeypatch,
):
    """Null strength outside trading hours is expected — keep the session tag."""

    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"symbol": code, "tday_rltv": None}

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "market_closed"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["execution_strength_pct"] is None
    assert result["data_state"] == "market_closed"


@pytest.mark.asyncio
async def test_get_execution_strength_rejects_non_kr():
    result = await market_data_quotes._get_execution_strength_impl("AAPL", "us")
    assert result.get("error") or result.get("success") is False


def test_execution_strength_tool_registered():
    assert "get_execution_strength" in market_data_quotes.MARKET_DATA_TOOL_NAMES
