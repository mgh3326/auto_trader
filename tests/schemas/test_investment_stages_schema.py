import uuid

import pytest
from pydantic import ValidationError

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)


def test_stage_artifact_payload_minimal_valid():
    payload = StageArtifactPayload(
        stage_type="market",
        verdict=StageVerdict.NEUTRAL,
        confidence=42,
    )
    assert payload.confidence == 42
    assert payload.cited_snapshots == []


def test_stage_artifact_payload_rejects_confidence_out_of_range():
    with pytest.raises(ValidationError):
        StageArtifactPayload(
            stage_type="market",
            verdict=StageVerdict.BULL,
            confidence=120,
        )


def test_stage_citation_requires_snapshot_uuid():
    citation = StageCitation(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind="news",
        payload_path="$.articles[0].title",
    )
    assert citation.payload_path.startswith("$")
