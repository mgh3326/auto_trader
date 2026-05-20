import uuid
from types import SimpleNamespace

import pytest

from app.schemas.investment_stages import StageVerdict
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)
from app.services.investment_stages.stages.watch_context import WatchContextStage


def _snap(payload):
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="watch_context",
        payload_json=payload,
    )


@pytest.mark.asyncio
async def test_watch_context_unavailable():
    with pytest.raises(UnavailableStageError):
        await WatchContextStage().run(
            StageContext(bundle_uuid=uuid.uuid4(), snapshots_by_kind={}, bundle_metadata={})
        )


@pytest.mark.asyncio
async def test_watch_context_lists_active_alerts():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={
            "watch_context": [
                _snap(
                    {
                        "active_alerts": [
                            {"symbol": "035420", "condition": "price < 200000"},
                            {"symbol": "015760", "condition": "price > 25000"},
                        ]
                    }
                )
            ]
        },
        bundle_metadata={},
    )
    payload = await WatchContextStage().run(ctx)
    assert payload.verdict == StageVerdict.NEUTRAL
    assert any("035420" in kp for kp in payload.key_points)
