import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import StageContext
from app.services.investment_stages.stages.news import NewsStage


def _snap(articles):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="news",
        payload_json={"articles": articles},
    )


@pytest.mark.asyncio
async def test_news_stage_neutral_on_empty_articles():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"news": [_snap([])]},
        bundle_metadata={},
    )
    payload = await NewsStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL


@pytest.mark.asyncio
async def test_news_stage_bull_when_positive_dominates():
    articles = [{"title": "good", "sentiment": "positive"}] * 5 + [
        {"title": "bad", "sentiment": "negative"}
    ]
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"news": [_snap(articles)]},
        bundle_metadata={},
    )
    payload = await NewsStage().run(ctx)
    assert payload.verdict == StageVerdict.BULL
    assert payload.cited_snapshots
