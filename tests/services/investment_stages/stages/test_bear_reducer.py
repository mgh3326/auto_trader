import uuid
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.ai_providers.base import AiProviderResult
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.bear_reducer import BearReducerStage


@pytest.mark.asyncio
async def test_bear_reducer_degrades_when_budget_exhausted():
    """C3 (ROB-279): BudgetExceeded must produce a deterministic BEAR fallback,
    not propagate the exception, and not call the LLM provider."""
    provider = AsyncMock()
    budget = StageLLMBudget(max_calls=0)
    stage = BearReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={
            "market": StageArtifactPayload(
                stage_type="market",
                verdict=StageVerdict.BEAR,
                confidence=80,
                sell_evidence=["macro headwinds", "declining revenue"],
            )
        },
    )

    payload = await stage.run(ctx)

    provider.ask.assert_not_called()
    assert payload.verdict == StageVerdict.BEAR
    assert payload.confidence <= 40
    assert payload.model_name is None
    assert "macro headwinds" in (payload.summary or "")


@pytest.mark.asyncio
async def test_bear_reducer_degrades_to_neutral_when_budget_exhausted_and_no_evidence():
    provider = AsyncMock()
    budget = StageLLMBudget(max_calls=0)
    stage = BearReducerStage(provider, budget)

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
async def test_bear_reducer_synthesizes_prior_artifacts():
    provider = AsyncMock()
    provider.ask.return_value = AiProviderResult(
        answer='{"verdict": "bear", "confidence": 90, "summary": "Extreme volatility detected"}',
        provider="gemini",
        model="gemini-2.5-flash",
        usage=None,
        elapsed_ms=100,
    )
    budget = StageLLMBudget(max_calls=4)
    stage = BearReducerStage(provider, budget)

    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={},
        bundle_metadata={},
        prior_artifacts={
            "market": StageArtifactPayload(
                stage_type="market", verdict=StageVerdict.BEAR, confidence=80
            )
        },
    )

    payload = await stage.run(ctx)
    assert payload.verdict == StageVerdict.BEAR
    assert payload.confidence == 90
    assert budget.remaining == 3
