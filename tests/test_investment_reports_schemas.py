"""ROB-265 Plan 2 — Pydantic schema validator tests.

Schema-level rejection of invariants that the DB also enforces via CHECK.
We want callers to get a clean ValidationError, not an IntegrityError.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    ReportSnapshotBundleResponse,
    ReportSnapshotDetailResponse,
    WatchConditionPayload,
)
from tests._investment_reports_helpers import future_datetime


def _base_report_kwargs(**overrides) -> dict:
    kwargs: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "테스트",
        "summary": "요약",
        "kst_date": "2026-05-18",
    }
    kwargs.update(overrides)
    return kwargs


def _base_item_kwargs(**overrides) -> dict:
    kwargs: dict = {
        "client_item_key": "action-1",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "rationale": "r",
    }
    kwargs.update(overrides)
    return kwargs


def test_action_item_round_trip() -> None:
    item = IngestReportItem(**_base_item_kwargs())
    assert item.item_kind == "action"
    assert item.client_item_key == "action-1"
    assert item.watch_condition is None


def test_item_requires_client_item_key() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            item_kind="action",
            symbol="005930",
            side="buy",
            intent="buy_review",
            rationale="r",
        )
    assert "client_item_key" in str(exc_info.value)


def test_item_rejects_empty_client_item_key() -> None:
    with pytest.raises(ValidationError):
        IngestReportItem(
            **_base_item_kwargs(client_item_key=""),
        )


def test_watch_item_requires_watch_condition() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            **_base_item_kwargs(
                client_item_key="watch-1",
                item_kind="watch",
                intent="trend_recovery_review",
                side=None,
                valid_until=future_datetime(),
            )
        )
    assert "watch_condition" in str(exc_info.value)


def test_watch_item_requires_valid_until() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            **_base_item_kwargs(
                client_item_key="watch-1",
                item_kind="watch",
                intent="trend_recovery_review",
                side=None,
                watch_condition=WatchConditionPayload(
                    metric="rsi", operator="below", threshold=30
                ),
            )
        )
    assert "valid_until" in str(exc_info.value)


def test_watch_item_full_inserts() -> None:
    item = IngestReportItem(
        **_base_item_kwargs(
            client_item_key="watch-1",
            item_kind="watch",
            intent="trend_recovery_review",
            side=None,
            watch_condition=WatchConditionPayload(
                metric="rsi", operator="below", threshold=30
            ),
            valid_until=future_datetime(),
        )
    )
    assert item.watch_condition is not None
    assert item.watch_condition.metric == "rsi"


def test_kis_live_with_mock_preview_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportRequest(
            **_base_report_kwargs(
                account_scope="kis_live", execution_mode="mock_preview"
            )
        )
    assert "advisory_only" in str(exc_info.value)


def test_nxt_session_with_mock_preview_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportRequest(
            **_base_report_kwargs(market_session="nxt", execution_mode="mock_preview")
        )
    assert "advisory_only" in str(exc_info.value)


def test_kis_live_with_advisory_only_allowed() -> None:
    req = IngestReportRequest(
        **_base_report_kwargs(account_scope="kis_live", execution_mode="advisory_only")
    )
    assert req.account_scope == "kis_live"


def test_decision_request_minimal() -> None:
    req = RecordDecisionRequest(
        item_uuid=uuid.uuid4(),
        decision="approve",
        actor="operator-test",
    )
    assert req.decision == "approve"
    assert req.idempotency_key is None


def test_partial_approve_requires_non_empty_snapshot() -> None:
    """partial_approve without scoped payload is indistinguishable from full approve."""
    with pytest.raises(ValidationError) as exc_info:
        RecordDecisionRequest(
            item_uuid=uuid.uuid4(),
            decision="partial_approve",
            actor="operator-test",
        )
    assert "approved_payload_snapshot" in str(exc_info.value)


def test_partial_approve_rejects_empty_snapshot_dict() -> None:
    with pytest.raises(ValidationError) as exc_info:
        RecordDecisionRequest(
            item_uuid=uuid.uuid4(),
            decision="partial_approve",
            actor="operator-test",
            approved_payload_snapshot={},
        )
    assert "approved_payload_snapshot" in str(exc_info.value)


def test_partial_approve_with_snapshot_allowed() -> None:
    req = RecordDecisionRequest(
        item_uuid=uuid.uuid4(),
        decision="partial_approve",
        actor="operator-test",
        approved_payload_snapshot={"max_notional_krw": 100000},
    )
    assert req.decision == "partial_approve"


def test_activate_watch_request_minimal() -> None:
    req = ActivateWatchRequest(item_uuid=uuid.uuid4(), actor="operator-test")
    assert req.idempotency_key is None


def test_report_snapshot_bundle_response_legacy_no_snapshot_shape() -> None:
    """ROB-275 — legacy/no-snapshot reports return an empty bundle response."""
    response = ReportSnapshotBundleResponse(legacy_no_snapshot=True)
    assert response.bundle is None
    assert response.items == []
    assert response.unavailable_sources is None
    assert response.source_conflicts is None


def test_report_snapshot_detail_response_includes_full_payload() -> None:
    """ROB-275 — detail response carries role + payload."""
    response = ReportSnapshotDetailResponse(
        snapshot_uuid=uuid.uuid4(),
        role="required",
        snapshot_kind="portfolio",
        source_kind="manual",
        market="kr",
        symbol=None,
        account_scope="kis_live",
        source_table=None,
        source_id=None,
        source_uri=None,
        freshness_status="fresh",
        as_of=dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC),
        valid_until=None,
        source_timestamps_json={},
        coverage_json={},
        errors_json={},
        payload_json={"cash_krw": 1_000_000},
    )
    assert response.role == "required"
    assert response.payload_json == {"cash_krw": 1_000_000}


def test_item_accepts_decision_bucket_and_citations():
    su = uuid.uuid4()
    du = uuid.uuid4()
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        operation="review",
        apply_policy="requires_user_approval",
        symbol="AAA",
        side="buy",
        decision_bucket="new_buy_candidate",
        cited_symbol_report_uuid=su,
        cited_dimension_report_uuids=[du],
        cited_snapshot_uuids=[su, du],
    )
    assert item.decision_bucket == "new_buy_candidate"
    assert item.cited_symbol_report_uuid == su
    assert item.cited_dimension_report_uuids == [du]
    assert item.cited_snapshot_uuids == [su, du]


def test_item_rejects_unknown_decision_bucket():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            intent="buy_review",
            rationale="r",
            operation="review",
            apply_policy="requires_user_approval",
            decision_bucket="macro_call",
        )


def test_item_decision_bucket_optional():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        operation="review",
        apply_policy="requires_user_approval",
    )
    assert item.decision_bucket is None
    assert item.cited_dimension_report_uuids == []
    assert item.cited_snapshot_uuids == []
