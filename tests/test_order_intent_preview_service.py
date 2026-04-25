from unittest.mock import AsyncMock

import pytest

from app.schemas.order_intent_preview import OrderIntentPreviewRequest
from app.services.order_intent_preview_service import OrderIntentPreviewService


def _payload_with_items(items: list[dict]) -> dict:
    return {
        "success": True,
        "decision_run": {
            "id": "decision-test-run",
            "generated_at": "2026-04-20T10:00:00+00:00",
            "mode": "analysis_only",
            "persisted": True,
            "source": "portfolio_decision_service_v1",
        },
        "filters": {"market": "ALL", "account_keys": [], "q": None},
        "summary": {
            "symbols": 0,
            "decision_items": 0,
            "actionable_items": 0,
            "manual_review_items": 0,
            "auto_candidate_items": 0,
            "missing_context_items": 0,
            "by_action": {},
            "by_market": {},
        },
        "facets": {"accounts": []},
        "symbol_groups": [
            {
                "market_type": "CRYPTO",
                "symbol": "KRW-BTC",
                "name": "Bitcoin",
                "detail_url": "/portfolio/positions/CRYPTO/KRW-BTC",
                "position": {"components": []},
                "journal": None,
                "support_resistance": {"status": "unavailable"},
                "items": items,
                "warnings": [],
            }
        ],
        "warnings": [],
    }


def _item(**overrides) -> dict:
    item = {
        "id": "item-1",
        "action": "buy_candidate",
        "label": "Buy candidate",
        "priority": "medium",
        "current_price": 150_000_000.0,
        "action_price": 140_000_000.0,
        "action_price_source": "support",
        "delta_from_current_pct": -6.66,
        "anchor": None,
        "rationale": [],
        "execution_boundary": {
            "mode": "analysis_only",
            "auto_executable": False,
            "manual_only": True,
        },
        "badges": [],
        "warnings": [],
    }
    item.update(overrides)
    return item


def _service(payload: dict) -> OrderIntentPreviewService:
    decision_service = AsyncMock()
    decision_service.get_decision_run = AsyncMock(return_value=payload)
    return OrderIntentPreviewService(decision_service=decision_service)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buy_candidate_with_action_price_yields_watch_ready_below_trigger() -> None:
    service = _service(_payload_with_items([_item()]))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    assert response.mode == "preview_only"
    assert response.decision_run_id == "decision-test-run"
    assert len(response.intents) == 1
    intent = response.intents[0]
    assert intent.side == "buy"
    assert intent.intent_type == "buy_candidate"
    assert intent.status == "watch_ready"
    assert intent.symbol == "KRW-BTC"
    assert intent.market == "CRYPTO"
    assert intent.trigger is not None
    assert intent.trigger.metric == "price"
    assert intent.trigger.operator == "below"
    assert intent.trigger.threshold == 140_000_000.0
