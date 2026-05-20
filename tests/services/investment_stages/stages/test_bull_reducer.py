import uuid
from unittest.mock import AsyncMock

import pytest

from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.ai_providers.base import AiProviderResult
from app.services.investment_stages.budget import StageLLMBudget
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.bull_reducer import BullReducerStage


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
