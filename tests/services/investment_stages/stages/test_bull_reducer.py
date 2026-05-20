import uuid
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.ai_providers.base import AiProviderResult
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.bull_reducer import BullReducerStage


@pytest.mark.asyncio
async def test_bull_reducer_degrades_when_budget_exhausted():
    """C3 (ROB-279): BudgetExceeded must produce a deterministic BULL fallback,
    not propagate the exception, and not call the LLM provider."""
    provider = AsyncMock()
    # Budget already exhausted (0 remaining calls)
    budget = StageLLMBudget(max_calls=0)
    stage = BullReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={
            "market": StageArtifactPayload(
                stage_type="market",
                verdict=StageVerdict.BULL,
                confidence=70,
                buy_evidence=["strong earnings", "positive guidance"],
            )
        },
    )

    payload = await stage.run(ctx)

    # LLM must NOT be called on budget exhaustion
    provider.ask.assert_not_called()
    # Verdict should be BULL because buy_evidence was present
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence <= 40
    assert payload.model_name is None
    assert payload.prompt_version is None
    # Summary should contain at least one buy evidence fragment
    assert "strong earnings" in (payload.summary or "")


@pytest.mark.asyncio
async def test_bull_reducer_degrades_to_neutral_when_budget_exhausted_and_no_evidence():
    """C3: with no buy_evidence and exhausted budget → NEUTRAL at low confidence."""
    provider = AsyncMock()
    budget = StageLLMBudget(max_calls=0)
    stage = BullReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={},
    )

    payload = await stage.run(ctx)
    provider.ask.assert_not_called()
    assert payload.verdict == StageVerdict.NEUTRAL
    assert payload.confidence <= 20


@pytest.mark.asyncio
async def test_bull_reducer_falls_back_when_llm_returns_non_object_json():
    provider = AsyncMock()
    provider.ask.return_value = AiProviderResult(
        answer="[]",
        provider="gemini",
        model="gemini-2.5-flash",
        usage=None,
        elapsed_ms=123,
    )
    budget = StageLLMBudget(max_calls=4)
    stage = BullReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={},
    )

    payload = await stage.run(ctx)

    provider.ask.assert_called_once()
    assert payload.verdict == StageVerdict.NEUTRAL
    assert payload.confidence == 20
    assert payload.missing_data == ["bull_reducer"]
    assert payload.model_name is None
    assert payload.prompt_version is None


@pytest.mark.asyncio
async def test_bull_reducer_synthesizes_prior_artifacts():
    provider = AsyncMock()
    provider.ask.return_value = AiProviderResult(
        answer='{"verdict": "bull", "confidence": 85, "summary": "Highly positive news and market momentum"}',
        provider="gemini",
        model="gemini-2.5-flash",
        usage=None,
        elapsed_ms=123,
    )
    budget = StageLLMBudget(max_calls=4)
    stage = BullReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={
            "market": StageArtifactPayload(
                stage_type="market", verdict=StageVerdict.BULL, confidence=60
            )
        },
    )

    payload = await stage.run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.confidence == 85
    assert budget.remaining == 3
    provider.ask.assert_called_once()
