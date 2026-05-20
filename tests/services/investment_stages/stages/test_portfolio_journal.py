import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.portfolio_journal import (
    PortfolioJournalStage,
)


def _snap(kind, payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(), snapshot_kind=kind, payload_json=payload
    )


@pytest.mark.asyncio
async def test_portfolio_journal_unavailable_without_portfolio():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={}
    )
    with pytest.raises(UnavailableStageError):
        await PortfolioJournalStage().run(ctx)


@pytest.mark.asyncio
async def test_portfolio_journal_emits_neutral_with_buying_power():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "portfolio": [
                _snap("portfolio", {"buying_power_krw": 200000, "nav_krw": 1000000})
            ],
            "journal": [
                _snap("journal", {"entries": [{"symbol": "035420", "thesis": "tech"}]})
            ],
        },
        bundle_metadata={},
    )
    payload = await PortfolioJournalStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert "035420" in (payload.summary or "")
    assert len(payload.cited_snapshots) >= 1
