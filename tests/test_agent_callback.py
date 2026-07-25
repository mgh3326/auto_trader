"""Tests for the agent gateway callback router (formerly OpenClaw)."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from app.routers.agent_callback import AgentCallbackRequest, agent_callback


@pytest.mark.asyncio
async def test_agent_callback_persists_result_with_prompt_fallback_and_model_prefix() -> (
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

    payload = AgentCallbackRequest(
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
        "app.routers.agent_callback.create_stock_if_not_exists",
        new=mock_create_stock,
    ):
        res = await agent_callback(payload, db=db)

    assert res == {"status": "ok", "request_id": "r1", "analysis_result_id": 123}

    mock_create_stock.assert_awaited_once_with(
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="equity_us",
        db=ANY,
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
async def test_agent_callback_uses_payload_prompt_when_provided() -> None:
    class DummyStock:
        id = 7

    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def refresh_side_effect(instance):
        instance.id = 999

    db.refresh = AsyncMock(side_effect=refresh_side_effect)

    payload = AgentCallbackRequest(
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
        "app.routers.agent_callback.create_stock_if_not_exists",
        new=mock_create_stock,
    ):
        res = await agent_callback(payload, db=db)

    assert res["analysis_result_id"] == 999
    record = db.add.call_args.args[0]
    assert record.prompt == "original prompt"
    assert record.detailed_text == "md"


def _callback_payload() -> dict:
    return {
        "request_id": "r-alias",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "instrument_type": "equity_us",
        "decision": "buy",
        "confidence": 80,
        "reasons": ["x"],
        "price_analysis": {
            "appropriate_buy_range": {"min": 100, "max": 110},
            "appropriate_sell_range": {"min": 120, "max": 130},
            "buy_hope_range": {"min": 95, "max": 98},
            "sell_target_range": {"min": 150, "max": 160},
        },
        "detailed_text": "md",
        "model_name": None,
        "prompt": "p",
    }


def _build_app_with_callback_router(monkeypatch: pytest.MonkeyPatch):
    from fastapi import FastAPI

    from app.core.config import settings
    from app.routers import agent_callback as agent_callback_router

    monkeypatch.setattr(settings, "AGENT_GATEWAY_CALLBACK_TOKEN", "cb-token")

    app = FastAPI()
    app.include_router(agent_callback_router.router)
    return app


def test_new_agent_callback_path_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    app = _build_app_with_callback_router(monkeypatch)

    async def fake_persist(payload, db):
        return {
            "status": "ok",
            "request_id": payload.request_id,
            "analysis_result_id": 1,
        }

    with patch("app.routers.agent_callback._persist_agent_callback", new=fake_persist):
        with TestClient(app) as client:
            res = client.post(
                "/api/v1/agent/callback",
                json=_callback_payload(),
                headers={"Authorization": "Bearer cb-token"},
            )

    assert res.status_code == 200
    assert res.json()["request_id"] == "r-alias"


def test_legacy_openclaw_callback_alias_is_not_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    app = _build_app_with_callback_router(monkeypatch)

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/openclaw/callback",
            json=_callback_payload(),
            headers={"Authorization": "Bearer cb-token"},
        )

    assert res.status_code == 404
