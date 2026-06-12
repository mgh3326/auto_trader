"""ROB-420 — sell-failure message disambiguates KIS-subaccount scoping."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import _no_holdings_sell_message


def test_equity_kr_message_names_kis_subaccount_and_routable_hint():
    msg = _no_holdings_sell_message("005930", "equity_kr", is_mock=False)
    assert "kis_live" in msg
    assert "reference-only" in msg
    assert "order_routable" in msg


def test_equity_us_mock_message_uses_kis_mock_channel():
    msg = _no_holdings_sell_message("AAPL", "equity_us", is_mock=True)
    assert "kis_mock" in msg
    assert "toss/samsung" in msg


def test_crypto_message_is_upbit_not_kis():
    msg = _no_holdings_sell_message("KRW-BTC", "crypto", is_mock=False)
    assert "Upbit" in msg
    assert "kis_live" not in msg
    assert "kis_mock" not in msg


@pytest.mark.asyncio
async def test_preview_sell_uses_routability_message(monkeypatch):
    async def fake_holdings(*_a, **_k):
        return None

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", fake_holdings)
    result = await order_validation._preview_sell(
        symbol="AAPL",
        order_type="limit",
        quantity=1.0,
        price=100.0,
        current_price=100.0,
        market_type="equity_us",
        is_mock=False,
    )
    assert "reference-only" in result["error"]
    assert "kis_live" in result["error"]


@pytest.mark.asyncio
async def test_validate_sell_side_uses_routability_message(monkeypatch):
    async def fake_holdings(*_a, **_k):
        return None

    captured: dict[str, str] = {}

    def order_error(msg: str) -> dict[str, str]:
        captured["msg"] = msg
        return {"error": msg}

    monkeypatch.setattr(order_validation, "_get_holdings_for_order", fake_holdings)
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="AAPL",
        normalized_symbol="AAPL",
        market_type="equity_us",
        quantity=1.0,
        order_type="limit",
        price=100.0,
        current_price=100.0,
        order_error_fn=order_error,
        is_mock=True,
    )
    assert err is not None
    assert "kis_mock" in captured["msg"]
    assert "reference-only" in captured["msg"]


def test_no_holdings_sell_message_mentions_toss_api_when_enabled(monkeypatch):
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", True)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert "KIS subaccount" in msg
    assert "Toss API" in msg
    assert "reference-only" in msg


def test_no_holdings_sell_message_preserves_reference_only_when_disabled(monkeypatch):
    from app.mcp_server.tooling import order_validation

    monkeypatch.setattr(order_validation.settings, "toss_api_enabled", False)

    msg = order_validation._no_holdings_sell_message("005930", "equity_kr", False)

    assert "toss/samsung" in msg
    assert "reference-only" in msg
