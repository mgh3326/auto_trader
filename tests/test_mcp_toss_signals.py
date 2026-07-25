from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals import _toss_signals as mod

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(mod.settings, "toss_consumer_signals_enabled", False)

    out_balance = await mod.handle_get_toss_buy_balance("005930")
    assert out_balance["status"] == "disabled"
    assert out_balance["source"] == "toss_consumer"
    assert "disabled" in out_balance["note"]

    out_signal = await mod.handle_get_toss_ai_signal("005930")
    assert out_signal["status"] == "disabled"
    assert out_signal["source"] == "toss_consumer"
    assert "disabled" in out_signal["note"]


@pytest.mark.asyncio
async def test_enabled_and_ok_response(monkeypatch):
    monkeypatch.setattr(mod.settings, "toss_consumer_signals_enabled", True)

    async def mock_fetch_buy_balance(self, product_code: str):
        assert product_code == "A005930"
        return {
            "buyBalanceRate": 0.6,
            "sellBalanceRate": 0.4,
            "foreignerRatio": 0.5,
            "warnings": [],
        }

    async def mock_fetch_ai_signal(self, product_code: str):
        assert product_code == "A005930"
        return {
            "signalDirection": "BUY",
            "reasoning": "Technical indicator support",
            "relatedReasoning": "Consensus target raised",
            "warnings": [],
        }

    monkeypatch.setattr(
        mod.TossConsumerClient, "fetch_buy_balance", mock_fetch_buy_balance
    )
    monkeypatch.setattr(mod.TossConsumerClient, "fetch_ai_signal", mock_fetch_ai_signal)

    out_balance = await mod.handle_get_toss_buy_balance("005930")
    assert out_balance["status"] == "ok"
    assert out_balance["buyBalanceRate"] == 0.6
    assert out_balance["sellBalanceRate"] == 0.4
    assert out_balance["foreignerRatio"] == 0.5
    assert not out_balance["warnings"]

    out_signal = await mod.handle_get_toss_ai_signal("005930")
    assert out_signal["status"] == "ok"
    assert out_signal["signalDirection"] == "BUY"
    assert out_signal["reasoning"] == "Technical indicator support"
    assert out_signal["relatedReasoning"] == "Consensus target raised"
    assert not out_signal["warnings"]


@pytest.mark.asyncio
async def test_enabled_but_http_error(monkeypatch):
    monkeypatch.setattr(mod.settings, "toss_consumer_signals_enabled", True)

    async def mock_fetch_error(self, product_code: str):
        raise RuntimeError("WTS API is down")

    monkeypatch.setattr(mod.TossConsumerClient, "fetch_buy_balance", mock_fetch_error)
    monkeypatch.setattr(mod.TossConsumerClient, "fetch_ai_signal", mock_fetch_error)

    out_balance = await mod.handle_get_toss_buy_balance("005930")
    assert "error" in out_balance
    assert "WTS API is down" in out_balance["error"]
    assert out_balance["source"] == "toss_consumer"

    out_signal = await mod.handle_get_toss_ai_signal("005930")
    assert "error" in out_signal
    assert "WTS API is down" in out_signal["error"]
    assert out_signal["source"] == "toss_consumer"


@pytest.mark.asyncio
async def test_non_kr_symbol_validation(monkeypatch):
    monkeypatch.setattr(mod.settings, "toss_consumer_signals_enabled", True)

    with pytest.raises(ValueError, match="only available for Korean stocks"):
        await mod.handle_get_toss_buy_balance("AAPL")

    with pytest.raises(ValueError, match="only available for Korean stocks"):
        await mod.handle_get_toss_ai_signal("AAPL")


def test_honest_labeling_description():
    doc_balance = mod.handle_get_toss_buy_balance.__doc__ or ""
    assert "orderbook balance rate" in doc_balance
    # "user buy ratio" should not be used as it is misleading
    assert "user buy ratio" not in doc_balance.lower()
    assert "retail polarity" not in doc_balance.lower()
