import uuid

import pytest

from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


class _FakeBundleReadService:
    async def get_bundle(self, *, bundle_uuid):
        raise NotImplementedError


def test_stage_context_holds_bundle_and_snapshots():
    ctx = StageContext(
        bundle_uuid=uuid.uuid4(),
        snapshots_by_kind={"market": []},
        bundle_metadata={"freshness_overall": "fresh"},
    )
    assert ctx.snapshots_for("market") == []
    assert ctx.snapshots_for("unknown") == []


def test_unavailable_stage_error_carries_reason():
    with pytest.raises(UnavailableStageError) as exc:
        raise UnavailableStageError("portfolio snapshot missing")
    assert "portfolio" in str(exc.value)
