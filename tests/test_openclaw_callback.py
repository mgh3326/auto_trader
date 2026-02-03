"""Tests for OpenClaw callback router."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.openclaw_callback import OpenClawCallbackRequest, openclaw_callback


@pytest.mark.asyncio
async def test_openclaw_callback_persists_result_with_prompt_fallback_and_model_prefix() -> (
    None
):
    class DummyStock:
        id = 42

    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def refresh_side_effect(instance):
        instance.id = 123

    db.refresh = AsyncMock(side_effect=refresh_side_effect)

    payload = OpenClawCallbackRequest(
        request_id="r1",
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        decision="buy",
        confidence=90,
        reasons=["a", "b"],
        price_analysis={
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        detailed_text=None,
        model_name="openai/gpt-x",
        prompt=None,
    )

    mock_create_stock = AsyncMock(return_value=DummyStock())
    with patch(
        "app.routers.openclaw_callback.create_stock_if_not_exists",
        new=mock_create_stock,
    ):
        res = await openclaw_callback(payload, db=db)

    assert res == {"status": "ok", "request_id": "r1", "analysis_result_id": 123}

    mock_create_stock.assert_awaited_once_with(
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    db.refresh.assert_awaited_once()

    record = db.add.call_args.args[0]
    assert record.stock_info_id == 42
    assert record.model_name == "openclaw-gpt"
    assert record.prompt == "[openclaw request_id=r1] AAPL (Apple Inc.)"
    assert record.decision == "buy"
    assert record.confidence == 90
    assert record.reasons == ["a", "b"]

    assert record.appropriate_buy_min == 100
    assert record.appropriate_buy_max == 110
    assert record.appropriate_sell_min == 120
    assert record.appropriate_sell_max == 130
    assert record.buy_hope_min == 95
    assert record.buy_hope_max == 98
    assert record.sell_target_min == 150
    assert record.sell_target_max == 160

    assert record.detailed_text == "[openclaw upstream_model=openai/gpt-x]"


@pytest.mark.asyncio
async def test_openclaw_callback_uses_payload_prompt_when_provided() -> None:
    class DummyStock:
        id = 7

    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def refresh_side_effect(instance):
        instance.id = 999

    db.refresh = AsyncMock(side_effect=refresh_side_effect)

    payload = OpenClawCallbackRequest(
        request_id="r2",
        symbol="BTC",
        name="Bitcoin",
        instrument_type="crypto",
        decision="hold",
        confidence=55,
        reasons=None,
        price_analysis={
            "appropriate_buy_range": {"min": 1, "max": 2},
            "appropriate_sell_range": {"min": 3, "max": 4},
            "buy_hope_range": {"min": 5, "max": 6},
            "sell_target_range": {"min": 7, "max": 8},
        },
        detailed_text="md",
        model_name=None,
        prompt="original prompt",
    )

    mock_create_stock = AsyncMock(return_value=DummyStock())
    with patch(
        "app.routers.openclaw_callback.create_stock_if_not_exists",
        new=mock_create_stock,
    ):
        res = await openclaw_callback(payload, db=db)

    assert res["analysis_result_id"] == 999
    record = db.add.call_args.args[0]
    assert record.prompt == "original prompt"
    assert record.detailed_text == "md"
