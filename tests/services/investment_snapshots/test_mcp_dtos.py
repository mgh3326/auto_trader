"""ROB-269 Phase 2 — MCP/API DTO validation."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.investment_snapshots_mcp import (
    EnsureBundleRequest,
    EnsureBundleResponse,
    ListBundlesRequest,
    ListSnapshotsRequest,
    RefreshRequest,
)


def test_ensure_bundle_request_defaults_mode_to_ensure_fresh():
    req = EnsureBundleRequest(
        purpose="kr_action_report",
        market="kr",
        policy_version="intraday_action_report_v1",
    )
    assert req.mode == "ensure_fresh"
    assert req.requested_by == "user"
    assert req.manual_snapshots is None


def test_ensure_bundle_request_reuse_only_accepted():
    req = EnsureBundleRequest(
        purpose="kr_action_report",
        market="kr",
        policy_version="intraday_action_report_v1",
        mode="reuse_only",
    )
    assert req.mode == "reuse_only"


def test_ensure_bundle_request_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        EnsureBundleRequest(
            purpose="kr_action_report",
            market="kr",
            policy_version="intraday_action_report_v1",
            mode="hypothetical_mode",  # type: ignore[arg-type]
        )


def test_ensure_bundle_request_rejects_unknown_market():
    with pytest.raises(ValidationError):
        EnsureBundleRequest(
            purpose="kr_action_report",
            market="jp",  # type: ignore[arg-type]
            policy_version="intraday_action_report_v1",
        )


def test_ensure_bundle_request_candidate_limit_bounded():
    with pytest.raises(ValidationError):
        EnsureBundleRequest(
            purpose="kr_action_report",
            market="kr",
            policy_version="intraday_action_report_v1",
            candidate_limit=0,  # below ge=1
        )
    with pytest.raises(ValidationError):
        EnsureBundleRequest(
            purpose="kr_action_report",
            market="kr",
            policy_version="intraday_action_report_v1",
            candidate_limit=101,  # above le=100
        )


def test_ensure_bundle_response_reused_status_allowed():
    resp = EnsureBundleResponse(
        bundle_uuid=uuid.uuid4(),
        status="reused",
        created=False,
    )
    assert resp.status == "reused"
    assert resp.run_uuid is None


def test_ensure_bundle_response_complete_with_run_uuid():
    resp = EnsureBundleResponse(
        bundle_uuid=uuid.uuid4(),
        status="complete",
        created=True,
        run_uuid=uuid.uuid4(),
    )
    assert resp.status == "complete"


def test_list_snapshots_request_limit_default_and_bounds():
    req = ListSnapshotsRequest()
    assert req.limit == 20
    with pytest.raises(ValidationError):
        ListSnapshotsRequest(limit=0)
    with pytest.raises(ValidationError):
        ListSnapshotsRequest(limit=200)


def test_list_bundles_request_accepts_all_filters_optional():
    req = ListBundlesRequest()
    assert req.purpose is None
    assert req.market is None
    assert req.status is None
    assert req.limit == 20


def test_refresh_request_requires_reason():
    with pytest.raises(ValidationError):
        RefreshRequest(
            reason="",  # min_length=1
            market="kr",
        )


def test_refresh_request_defaults():
    req = RefreshRequest(reason="test smoke", market="kr")
    assert req.purpose == "manual_refresh"
    assert req.policy_version == "intraday_action_report_v1"
    assert req.requested_by == "user"
    assert req.snapshot_kinds is None


def test_refresh_request_with_kinds_filter():
    req = RefreshRequest(
        reason="forced refresh after deploy",
        market="kr",
        snapshot_kinds=["portfolio", "market"],
    )
    assert req.snapshot_kinds == ["portfolio", "market"]


def test_refresh_request_extra_field_rejected():
    with pytest.raises(ValidationError):
        RefreshRequest(
            reason="x",
            market="kr",
            unknown_field=1,  # type: ignore[call-arg]
        )


def test_ensure_bundle_request_accepts_manual_snapshots_collection():
    from app.services.investment_snapshots.collectors import SnapshotCollectResult

    snap = SnapshotCollectResult(
        snapshot_kind="portfolio",
        market="kr",
        account_scope="kis_live",
        source_kind="manual",
        payload_json={"cash_krw": 1_000_000},
        as_of=dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC),
    )
    req = EnsureBundleRequest(
        purpose="kr_action_report",
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        manual_snapshots={"portfolio": [snap]},
    )
    assert req.manual_snapshots is not None
    assert "portfolio" in req.manual_snapshots
    assert req.manual_snapshots["portfolio"][0].payload_json == {"cash_krw": 1_000_000}
