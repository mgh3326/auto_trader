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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hold_items_are_excluded() -> None:
    items = [
        _item(id="hold-1", action="hold", action_price=None),
        _item(id="buy-1", action="buy_candidate"),
    ]
    service = _service(_payload_with_items(items))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    assert [i.decision_item_id for i in response.intents] == ["buy-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_manual_review_action_is_marked_manual_review_required() -> None:
    items = [_item(id="mr-1", action="manual_review", action_price=None)]
    service = _service(_payload_with_items(items))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    assert len(response.intents) == 1
    assert response.intents[0].status == "manual_review_required"
    assert response.intents[0].trigger is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trim_candidate_above_threshold_is_execution_candidate() -> None:
    items = [
        _item(
            id="trim-1",
            action="trim_candidate",
            current_price=120.0,
            action_price=110.0,
        )
    ]
    service = _service(_payload_with_items(items))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    intent = response.intents[0]
    assert intent.side == "sell"
    assert intent.intent_type == "trim_candidate"
    assert intent.status == "execution_candidate"
    assert intent.trigger is not None
    assert intent.trigger.operator == "above"
    assert intent.trigger.threshold == 110.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_watch_below_threshold_is_watch_ready() -> None:
    items = [
        _item(
            id="sw-1",
            action="sell_watch",
            current_price=90.0,
            action_price=110.0,
        )
    ]
    service = _service(_payload_with_items(items))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    intent = response.intents[0]
    assert intent.side == "sell"
    assert intent.status == "watch_ready"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trim_candidate_without_action_price_is_manual_review_required() -> None:
    items = [_item(id="trim-2", action="trim_candidate", action_price=None)]
    service = _service(_payload_with_items(items))

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=OrderIntentPreviewRequest(),
    )

    intent = response.intents[0]
    assert intent.status == "manual_review_required"
    assert intent.trigger is None


from app.schemas.order_intent_preview import IntentSelectionInput


@pytest.mark.unit
@pytest.mark.asyncio
async def test_selection_override_threshold_replaces_action_price() -> None:
    items = [_item(id="buy-1", action="buy_candidate", action_price=140.0)]
    service = _service(_payload_with_items(items))

    request = OrderIntentPreviewRequest(
        selections=[
            IntentSelectionInput(decision_item_id="buy-1", override_threshold=125.0)
        ]
    )

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=request,
    )

    intent = response.intents[0]
    assert intent.trigger is not None
    assert intent.trigger.threshold == 125.0
    assert intent.trigger.source == "override"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_override_threshold_supplies_trigger_when_action_price_missing() -> None:
    items = [_item(id="trim-3", action="trim_candidate", action_price=None,
                   current_price=200.0)]
    service = _service(_payload_with_items(items))

    request = OrderIntentPreviewRequest(
        selections=[
            IntentSelectionInput(decision_item_id="trim-3", override_threshold=180.0)
        ]
    )

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=request,
    )

    intent = response.intents[0]
    assert intent.trigger is not None
    assert intent.trigger.threshold == 180.0
    assert intent.status == "execution_candidate"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_selection_enabled_false_excludes_item() -> None:
    items = [
        _item(id="buy-1", action="buy_candidate"),
        _item(id="buy-2", action="buy_candidate"),
    ]
    service = _service(_payload_with_items(items))

    request = OrderIntentPreviewRequest(
        selections=[
            IntentSelectionInput(decision_item_id="buy-1", enabled=False),
        ]
    )

    response = await service.build_preview(
        user_id=7,
        run_id="decision-test-run",
        request=request,
    )

    assert [i.decision_item_id for i in response.intents] == ["buy-2"]
