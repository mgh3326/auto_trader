from __future__ import annotations

import datetime as dt

import pytest

from app.mcp_server.tooling import orders_toss_variants as otv
from app.services.brokers.toss.errors import TossApiResponseError, TossErrorEnvelope
from app.services.nxt_preflight import NxtTradability


@pytest.fixture
def _toss_enabled(monkeypatch):
    monkeypatch.setattr(
        otv, "validate_toss_api_config", lambda: [], raising=True
    )


@pytest.mark.asyncio
async def test_preview_warns_on_nxt_non_eligible(monkeypatch, _toss_enabled):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)

    async def fake_session(_moment):
        return "nxt_premarket"

    async def fake_trad(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=False, nxt_trading_suspended=None, asof=None
            )
        }

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)

    # Neutralize network-touching preview helpers.
    async def _no_price_ctx(client, symbol):
        return None, None, None

    monkeypatch.setattr(otv, "_preview_price_context", _no_price_ctx)

    class _Guard:
        ok = True
        warnings: list = []
        error_message = None

    async def _no_warnings(client, symbol, *, market, side):
        return _Guard()

    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)

    res = await otv.toss_preview_order(
        symbol="005930", side="buy", order_type="market", quantity=1
    )
    assert res["success"] is True
    assert "nxt_session_not_tradable" in res["order_warnings"]
    assert res["nxt_preflight"]["block"] is True
    assert "retry_at_regular" in res["nxt_preflight"]["alternatives"]


@pytest.mark.asyncio
async def test_preflight_context_none_when_off(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "off", raising=False)
    assert await otv._nxt_preflight_context("005930", "kr") is None


@pytest.mark.asyncio
async def test_preflight_context_none_for_us(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)
    assert await otv._nxt_preflight_context("AAPL", "us") is None


@pytest.mark.asyncio
async def test_preflight_fail_open_when_session_none(monkeypatch):
    monkeypatch.setattr(otv.settings, "toss_nxt_preflight_mode", "warn", raising=False)

    async def fake_session(_moment):
        return None

    async def fake_trad(symbols, db=None):
        return {symbols[0]: NxtTradability(False, None, None)}

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)
    verdict, _ = await otv._nxt_preflight_context("005930", "kr")
    assert verdict.block is False
    assert verdict.advisory is True


@pytest.mark.asyncio
async def test_suggest_order_account_kr_carries_nxt_advisory(monkeypatch):
    """suggest_order_account_impl (KR) exposes nxt public fields + an advisory
    nxt_preflight/session block; US path stays clean (covers the File Structure
    'suggest_order_account advisory' coverage claim)."""
    from app.mcp_server.tooling import account_routing_tools as art

    # Neutralize the pricing/capital/holdings dependencies.
    async def _fake_resolve_price(symbol, market, price):
        return 70000.0, "test"

    async def _fake_capital(*, include_manual=False):
        return {}

    async def _fake_holdings(*, market, include_current_price, minimum_value):
        return []

    async def _fake_user_setting(_key):
        return {}

    monkeypatch.setattr(art, "_resolve_price", _fake_resolve_price)
    monkeypatch.setattr(art, "get_available_capital_impl", _fake_capital)
    monkeypatch.setattr(art, "_get_holdings_impl", _fake_holdings)
    monkeypatch.setattr(art, "get_user_setting", _fake_user_setting)
    monkeypatch.setattr(
        art, "suggest_account_from_snapshot", lambda _inp: {"account_mode": "toss_live"}
    )

    async def _fake_session(_moment):
        return "nxt_premarket"

    async def _fake_trad(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=False, nxt_trading_suspended=None, asof=None
            )
        }

    monkeypatch.setattr(art, "get_kr_toss_session_from_toss", _fake_session)
    monkeypatch.setattr(art, "get_kr_nxt_tradability", _fake_trad)

    result = await art.suggest_order_account_impl(
        symbol="005930", market="kr", side="buy", quantity=1
    )
    assert result["nxt_tradable"] is False
    assert result["nxt_tradable_source"] == "kr_symbol_universe"
    assert result["nxt_preflight"]["block"] is True
    assert result["nxt_preflight"]["session"] == "nxt_premarket"

    # US path: no nxt fields, no preflight.
    us_result = await art.suggest_order_account_impl(
        symbol="AAPL", market="us", side="buy", quantity=1, usd_krw=1350.0
    )
    assert "nxt_tradable" not in us_result
    assert "nxt_preflight" not in us_result


@pytest.mark.asyncio
async def test_place_blocks_in_required_mode(monkeypatch, _toss_enabled):
    monkeypatch.setattr(
        otv.settings, "toss_nxt_preflight_mode", "required", raising=False
    )
    monkeypatch.setattr(
        otv.settings, "toss_live_order_mutations_enabled", True, raising=False
    )

    async def fake_session(_moment):
        return "nxt_after"

    async def fake_trad(symbols, db=None):
        return {symbols[0]: NxtTradability(False, None, None)}

    monkeypatch.setattr(otv, "get_kr_toss_session_from_toss", fake_session)
    monkeypatch.setattr(otv, "get_kr_nxt_tradability", fake_trad)

    placed = {"called": False}

    class _Client:
        async def place_order(self, payload):
            placed["called"] = True
            raise AssertionError("place_order must not be reached")

    # sell-loss/opposite guards are buy-side-skippable; drive a buy market order.
    async def _no_warnings(client, symbol, *, market, side):
        class _G:
            ok = True
            warnings: list = []
            error_message = None

        return _G()

    async def _no_opp(client, symbol, side, base):
        return None

    monkeypatch.setattr(otv, "check_warnings_guard", _no_warnings)
    monkeypatch.setattr(otv, "_opposite_pending_error", _no_opp)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        yield _Client()

    monkeypatch.setattr(otv, "_client_context", _ctx)

    res = await otv.toss_place_order(
        symbol="005930",
        side="buy",
        order_type="market",
        quantity=1,
        dry_run=False,
        confirm=True,
    )
    assert res["success"] is False
    assert res["error_code"] == "nxt_session_not_tradable"
    assert res["session"] == "nxt_after"
    assert "route_via_kis" in res["alternatives"]
    assert placed["called"] is False


@pytest.mark.unit
def test_error_response_maps_market_not_supported():
    envelope = TossErrorEnvelope(
        request_id="rq1",
        code="market-not-supported-for-stock",
        message="not supported",
        data=None,
    )
    exc = TossApiResponseError(envelope, status_code=422)
    out = otv._toss_error_response(exc, {"source": "toss"})
    assert out["error_code"] == "nxt_session_not_tradable"
    assert "route_via_kis" in out["alternatives"]
    assert "hint" in out
