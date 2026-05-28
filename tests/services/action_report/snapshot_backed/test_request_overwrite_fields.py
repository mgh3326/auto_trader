"""ROB-352 Slice A — overwrite/reused contract fields on the generator envelopes."""

from __future__ import annotations

import uuid

from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
    ReportGenerationResponse,
)


def _req(**overrides):
    base = {
        "market": "kr",
        "account_scope": "kis_live",
        "created_by_profile": "t",
        "title": "t",
        "summary": "s",
        "kst_date": "2026-05-29",
    }
    base.update(overrides)
    return ReportGenerationRequest.model_validate(base)


def test_overwrite_defaults_to_false():
    req = _req()
    assert req.overwrite_existing is False
    assert req.overwrite_reason is None


def test_overwrite_can_be_set_with_reason():
    req = _req(overwrite_existing=True, overwrite_reason="restated US session")
    assert req.overwrite_existing is True
    assert req.overwrite_reason == "restated US session"


def test_response_reused_existing_defaults_false():
    resp = ReportGenerationResponse(
        report_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        snapshot_policy_version="p",
        snapshot_coverage_summary={},
        snapshot_freshness_summary={},
        source_conflicts={},
        unavailable_sources={},
        items_count=0,
        warnings=[],
        bundle_status="complete",
        bundle_reused=False,
        stale_gate={},
    )
    assert resp.reused_existing is False
