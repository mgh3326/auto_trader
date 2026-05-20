import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.ai_providers.base import AiProviderResult
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.composer import FinalComposer


@pytest.mark.asyncio
async def test_composer_drops_uncited_items():
    """C2 (ROB-279): items with empty or unknown cited_stage_types must be dropped;
    when ALL items are dropped a single no_action_note fallback is emitted."""
    provider = AsyncMock()
    provider.ask.return_value = AiProviderResult(
        answer=(
            '{"title": "T", "summary": "S", "items": ['
            '{"client_item_key": "k_empty", "item_kind": "action", "intent": "buy_review",'
            ' "symbol": "AAPL", "side": "buy", "rationale": "r", "cited_stage_types": []},'
            '{"client_item_key": "k_unknown", "item_kind": "action", "intent": "buy_review",'
            ' "symbol": "GOOG", "side": "buy", "rationale": "r2", "cited_stage_types": ["unknown_stage"]}'
            "]}"
        ),
        provider="gemini",
        model="m",
        usage=None,
        elapsed_ms=1,
    )
    budget = StageLLMBudget()
    composer = FinalComposer(provider, budget)

    market_uuid = uuid.uuid4()
    artifacts = [
        SimpleNamespace(
            stage_type="market",
            artifact_uuid=market_uuid,
            verdict="bull",
            summary="s",
            key_points=[],
            buy_evidence=[],
            sell_evidence=[],
            risk_evidence=[],
        )
    ]

    req = await composer.compose(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
        kst_date="2026-05-20",
        artifacts=artifacts,
    )

    # Both items have no valid citations — both dropped; fallback emitted
    assert len(req.items) == 1
    fallback = req.items[0]
    assert fallback.client_item_key == "auto-no-action"
    assert fallback.item_kind == "risk"
    assert fallback.operation == "review"
    assert "stage citations" in fallback.rationale


@pytest.mark.asyncio
async def test_composer_assembles_report_and_enforces_citations():
    provider = AsyncMock()
    provider.ask.return_value = AiProviderResult(
        answer='{"title": "Test Report", "summary": "Sum", "items": [{"client_item_key": "k1", "item_kind": "action", "intent": "buy_review", "symbol": "AAPL", "side": "buy", "rationale": "r", "cited_stage_types": ["market"]}]}',
        provider="gemini",
        model="m",
        usage=None,
        elapsed_ms=1,
    )
    budget = StageLLMBudget()
    composer = FinalComposer(provider, budget)

    market_uuid = uuid.uuid4()
    artifacts = [
        SimpleNamespace(stage_type="market", artifact_uuid=market_uuid, verdict="bull", summary="s", key_points=[], buy_evidence=[], sell_evidence=[], risk_evidence=[])
    ]

    req = await composer.compose(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
        kst_date="2026-05-20",
        artifacts=artifacts,
    )

    assert req.title == "Test Report"
    assert len(req.items) == 1
    assert str(market_uuid) in req.items[0].metadata["cited_stage_uuids"]
    assert budget.remaining == 3
