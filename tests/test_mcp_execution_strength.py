"""ROB-462/ROB-485: get_execution_strength MCP tool + KIS FHKST01010300 fetch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import market_data_quotes

# 2026-06-10 09:42 KST 라이브 프로브 실측 row (012450). FHKST01010300 row 키
# 전수: cntg_vol, prdy_ctrt, prdy_vrss, prdy_vrss_sign, stck_cntg_hour,
# stck_prpr, tday_rltv — per-side 매수/매도 체결량 필드는 없다.
_CCNL_ROW_OLDER = {
    "stck_cntg_hour": "093713",
    "stck_prpr": "1031000",
    "prdy_vrss": "15000",
    "prdy_vrss_sign": "2",
    "cntg_vol": "2",
    "tday_rltv": "80.89",
    "prdy_ctrt": "1.48",
}
_CCNL_ROW_LATEST = {
    "stck_cntg_hour": "094227",
    "stck_prpr": "1031000",
    "prdy_vrss": "15000",
    "prdy_vrss_sign": "2",
    "cntg_vol": "1",
    "tday_rltv": "81.82",
    "prdy_ctrt": "1.48",
}


def _make_market_data_mixin(response):
    from app.services.brokers.kis.domestic_market_data import (
        DomesticMarketDataMixin,
    )

    md = DomesticMarketDataMixin.__new__(DomesticMarketDataMixin)
    md._kis_url = lambda path: path
    md._request_with_token_retry = AsyncMock(return_value=response)
    return md


@pytest.mark.asyncio
async def test_inquire_execution_strength_uses_ccnl_tr_and_selects_latest_row():
    from app.services.brokers.kis import constants

    # 의도적으로 오래된 row 를 먼저 둬서 index 0 비신뢰를 증명한다.
    md = _make_market_data_mixin({"output": [_CCNL_ROW_OLDER, _CCNL_ROW_LATEST]})

    raw = await md.inquire_execution_strength("012450")

    assert raw["symbol"] == "012450"
    assert raw["tday_rltv"] == "81.82"
    assert raw["stck_cntg_hour"] == "094227"
    assert raw["stck_prpr"] == "1031000"
    assert raw["acml_vol"] is None
    md._request_with_token_retry.assert_awaited_once_with(
        tr_id=constants.DOMESTIC_CCNL_TR,
        url=constants.DOMESTIC_CCNL_URL,
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "012450"},
        api_name="inquire_execution_strength",
    )


@pytest.mark.asyncio
async def test_inquire_execution_strength_empty_list_returns_all_none():
    # 개장 직전/거래정지 등 빈 tick 리스트 → all-None graceful, 절대 raise 금지.
    md = _make_market_data_mixin({"output": []})

    raw = await md.inquire_execution_strength("012450")

    assert raw == {
        "symbol": "012450",
        "tday_rltv": None,
        "stck_cntg_hour": None,
        "stck_prpr": None,
        "acml_vol": None,
    }


@pytest.mark.asyncio
async def test_inquire_execution_strength_dict_output_returns_all_none():
    # 옛 FHKST01010100 식 단일 dict output 형태는 더 이상 가정하지 않는다.
    md = _make_market_data_mixin({"output": {"cttr": "120.3", "stck_prpr": "80000"}})

    raw = await md.inquire_execution_strength("005930")

    assert raw["tday_rltv"] is None
    assert raw["stck_cntg_hour"] is None


@pytest.mark.asyncio
async def test_inquire_execution_strength_row_missing_tday_rltv():
    row = {"stck_cntg_hour": "094227", "stck_prpr": "1031000", "cntg_vol": "1"}
    md = _make_market_data_mixin({"output": [row]})

    raw = await md.inquire_execution_strength("012450")

    assert raw["tday_rltv"] is None
    assert raw["stck_cntg_hour"] == "094227"


@pytest.mark.asyncio
async def test_kis_facade_delegates_execution_strength():
    from app.services.brokers.kis.client import KISClient

    client = KISClient.__new__(KISClient)
    client._market_data = AsyncMock()
    client._market_data.inquire_execution_strength = AsyncMock(
        return_value={"tday_rltv": "81.82"}
    )

    raw = await client.inquire_execution_strength("005930", market="J")

    assert raw == {"tday_rltv": "81.82"}
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
                "stck_cntg_hour": "100000",
                "stck_prpr": "80000",
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["symbol"] == "005930"
    assert result["execution_strength_pct"] == pytest.approx(135.5)
    assert result["trend"] == "buy_dominant"
    # KIS REST 미제공 (WebSocket H0STCNT0 전용) — 항상 None, 0 날조 금지.
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
async def test_get_execution_strength_rejects_non_kr():
    result = await market_data_quotes._get_execution_strength_impl("AAPL", "us")
    assert result.get("error") or result.get("success") is False


@pytest.mark.asyncio
async def test_get_execution_strength_surfaces_tick_time(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {
                "symbol": code,
                "tday_rltv": "81.82",
                "stck_cntg_hour": "094227",
                "stck_prpr": "1031000",
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("012450", "kr")

    assert result["tick_time"] == "094227"
    assert result["execution_strength_pct"] == pytest.approx(81.82)
    assert result["trend"] == "sell_dominant"
    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_get_execution_strength_field_unavailable_during_fresh(monkeypatch):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            # 빈 tick 리스트 → broker all-None graceful 형태.
            return {
                "symbol": code,
                "tday_rltv": None,
                "stck_cntg_hour": None,
                "stck_prpr": None,
                "acml_vol": None,
            }

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    result = await market_data_quotes._get_execution_strength_impl("005930", "kr")

    assert result["execution_strength_pct"] is None
    assert result["trend"] is None
    # 장중인데 전부 null 을 "fresh" 로 위장하지 않는다 (ROB-485 정직 신호).
    assert result["data_state"] == "field_unavailable"


@pytest.mark.asyncio
async def test_get_execution_strength_null_outside_session_keeps_session_state(
    monkeypatch,
):
    class _MockKIS:
        async def inquire_execution_strength(self, code, market="J"):
            return {"tday_rltv": None, "stck_cntg_hour": None}

    monkeypatch.setattr(market_data_quotes, "KISClient", _MockKIS)
    for state in ("premarket_unavailable", "market_closed"):
        monkeypatch.setattr(
            market_data_quotes,
            "kr_market_data_state",
            lambda *a, _state=state, **k: _state,
        )
        result = await market_data_quotes._get_execution_strength_impl("005930", "kr")
        # field_unavailable 은 fresh 일 때만 — 세션 외 상태는 그대로 보존.
        assert result["data_state"] == state


def test_execution_strength_tool_registered():
    assert "get_execution_strength" in market_data_quotes.MARKET_DATA_TOOL_NAMES
