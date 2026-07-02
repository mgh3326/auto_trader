from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.analysis_artifact import (
    AnalysisArtifactListRequest,
    AnalysisArtifactRead,
    AnalysisArtifactSave,
)


@pytest.mark.unit
def test_save_strips_title_and_cleans_symbols() -> None:
    entry = AnalysisArtifactSave.model_validate(
        {
            "market": "kr",
            "kind": "candidate_pool",
            "title": "  KR candidates  ",
            "symbols": ["  005930  ", "", "DB"],
            "payload": {"top": ["005930"]},
            "as_of": "2026-07-02T00:00:00+00:00",
            "created_by": "claude",
            "session_label": "kr-2026-07-02",
        }
    )

    assert entry.title == "KR candidates"
    assert entry.symbols == ["005930", "DB"]
    assert entry.payload == {"top": ["005930"]}
    assert entry.as_of == datetime(2026, 7, 2, tzinfo=UTC)


@pytest.mark.unit
def test_save_rejects_unknown_kind_and_extra_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "mystery",
                "title": "x",
                "as_of": "2026-07-02T00:00:00+00:00",
            }
        )

    rendered = str(exc_info.value)
    assert "kind" in rendered

    with pytest.raises(ValidationError):
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "candidate_pool",
                "title": "x",
                "as_of": "2026-07-02T00:00:00+00:00",
                "unexpected": True,
            }
        )


@pytest.mark.unit
def test_list_request_clamps_limit_and_strips_symbol() -> None:
    request = AnalysisArtifactListRequest.model_validate(
        {
            "market": "us",
            "kind": "screening_ranking",
            "symbol": "  AAPL  ",
            "limit": 500,
        }
    )

    assert request.limit == 100
    assert request.symbol == "AAPL"
    assert request.include_stale is False


@pytest.mark.unit
def test_read_serializes_payload_from_attributes() -> None:
    class Row:
        id = 42
        artifact_uuid = UUID("55555555-5555-5555-5555-555555555555")
        market = "crypto"
        kind = "flow_assessment"
        title = "BTC flow"
        symbols = ["KRW-BTC"]
        payload = {"net_flow": 1_000_000}
        as_of = datetime(2026, 7, 2, tzinfo=UTC)
        valid_until = None
        session_label = None
        correlation_id = None
        account_scope = None
        content_hash = None
        version = 1
        readiness_label = None
        payload_size_bytes = 24
        is_stale = False
        created_by = "operator"
        created_at = datetime(2026, 7, 2, 1, 2, 3, tzinfo=UTC)

    response = AnalysisArtifactRead.model_validate(Row())

    assert response.payload == {"net_flow": 1_000_000}
    dumped = response.model_dump(mode="json")
    assert dumped["artifact_uuid"] == "55555555-5555-5555-5555-555555555555"
    assert dumped["symbols"] == ["KRW-BTC"]


@pytest.mark.unit
def test_save_accepts_valid_readiness_label_and_rejects_bad() -> None:
    entry = AnalysisArtifactSave.model_validate(
        {
            "market": "kr",
            "kind": "candidate_pool",
            "title": "with readiness",
            "as_of": "2026-07-02T00:00:00+00:00",
            "readiness_label": "ready_for_order_review",
        }
    )
    assert entry.readiness_label == "ready_for_order_review"

    # Omitted -> None (advisory, optional).
    entry_none = AnalysisArtifactSave.model_validate(
        {
            "market": "kr",
            "kind": "candidate_pool",
            "title": "no readiness",
            "as_of": "2026-07-02T00:00:00+00:00",
        }
    )
    assert entry_none.readiness_label is None

    with pytest.raises(ValidationError) as exc_info:
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "candidate_pool",
                "title": "bad readiness",
                "as_of": "2026-07-02T00:00:00+00:00",
                "readiness_label": "go_live",
            }
        )
    assert "readiness_label" in str(exc_info.value)


@pytest.mark.unit
def test_save_rejects_payload_over_100kb() -> None:
    large_payload = {"data": "x" * 105000}
    with pytest.raises(ValidationError) as exc_info:
        AnalysisArtifactSave.model_validate(
            {
                "market": "kr",
                "kind": "candidate_pool",
                "title": "too large",
                "as_of": "2026-07-02T00:00:00+00:00",
                "payload": large_payload,
            }
        )
    assert "payload size must not exceed 100KB" in str(exc_info.value)
