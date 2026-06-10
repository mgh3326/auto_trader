"""ROB-265 Plan 2 — Pydantic schema validator tests.

Schema-level rejection of invariants that the DB also enforces via CHECK.
We want callers to get a clean ValidationError, not an IntegrityError.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import (
    ActivateWatchRequest,
    AddReportItemsRequest,
    IngestReportItem,
    IngestReportRequest,
    MaxActionPayload,
    RecordDecisionRequest,
    ReportSnapshotBundleResponse,
    ReportSnapshotDetailResponse,
    UpdateDraftReportRequest,
    WatchConditionClause,
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


def test_clause_between_requires_low_high_and_orders():
    c = WatchConditionClause(metric="price", op="between", low="100", high="200")
    assert c.low == Decimal("100") and c.high == Decimal("200")
    with pytest.raises(ValidationError):
        WatchConditionClause(metric="price", op="between", low="200", high="100")
    with pytest.raises(ValidationError):
        WatchConditionClause(metric="price", op="above")  # missing threshold


def test_legacy_flat_payload_normalizes_to_single_condition():
    p = WatchConditionPayload(metric="price", operator="below", threshold="55000")
    assert len(p.conditions) == 1
    assert p.conditions[0].metric == "price"
    assert p.conditions[0].op == "below"
    assert p.conditions[0].threshold == Decimal("55000")
    assert p.combine == "and"
    assert p.threshold_key == "55000"  # legacy dedup key preserved


def test_conditions_payload_multi_metric_and():
    p = WatchConditionPayload(
        conditions=[
            {"metric": "price", "op": "between", "low": "50000", "high": "55000"},
            {"metric": "rsi", "op": "below", "threshold": "35"},
        ]
    )
    assert len(p.conditions) == 2
    assert p.combine == "and"


def test_payload_requires_conditions_or_flat():
    with pytest.raises(ValidationError):
        WatchConditionPayload(target_kind="asset")  # neither flat nor conditions


def test_max_action_xor_quantity_notional():
    MaxActionPayload(side="buy", quantity="10", account_mode="kis_mock")
    MaxActionPayload(side="sell", notional="1000000", account_mode="kis_mock")
    with pytest.raises(ValidationError):
        MaxActionPayload(side="buy", account_mode="kis_mock")  # neither
    with pytest.raises(ValidationError):
        MaxActionPayload(
            side="buy", quantity="10", notional="100", account_mode="kis_mock"
        )  # both


def test_max_action_allows_extra_legacy_keys():
    m = MaxActionPayload(
        side="buy", quantity="10", account_mode="kis_mock", notional_usd="500"
    )
    assert m.model_dump()["notional_usd"] == "500"


def test_ingest_item_validates_max_action_when_present():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="create",
            intent="buy_review",
            rationale="r",
            symbol="005930",
            watch_condition={"metric": "price", "operator": "below", "threshold": "5"},
            valid_until="2026-12-31T00:00:00Z",
            max_action={"side": "buy"},  # invalid: no quantity/notional
        )


def test_auto_execute_mock_action_mode_flag_and_literal():
    from app.core.config import settings
    from app.schemas.investment_reports import WatchConditionPayload

    assert settings.WATCH_AUTO_EXECUTE_MOCK_ENABLED is False
    # auto_execute_mock is now a valid action_mode literal value
    p = WatchConditionPayload(
        metric="price", operator="below", threshold="5", action_mode="auto_execute_mock"
    )
    assert p.action_mode == "auto_execute_mock"


def test_add_report_items_request_requires_non_empty_items() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AddReportItemsRequest(report_uuid=uuid.uuid4(), items=[])
    assert "items" in str(exc_info.value)


def test_add_report_items_request_accepts_ingest_items() -> None:
    req = AddReportItemsRequest(
        report_uuid=uuid.uuid4(),
        items=[IngestReportItem(**_base_item_kwargs(client_item_key="increment-1"))],
        actor="operator",
    )
    assert req.items[0].client_item_key == "increment-1"
    assert req.actor == "operator"


def test_update_draft_report_request_requires_at_least_one_update_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UpdateDraftReportRequest(report_uuid=uuid.uuid4(), actor="operator")
    assert "at least one draft report field" in str(exc_info.value)


def test_update_draft_report_request_accepts_summary_and_snapshots() -> None:
    req = UpdateDraftReportRequest(
        report_uuid=uuid.uuid4(),
        summary="fresh intraday summary",
        market_snapshot={"kospi": {"last": 2860.12}},
        portfolio_snapshot={"cash": 12345},
        metadata={"source": "intraday_update"},
        reason="market moved",
    )
    assert req.summary == "fresh intraday summary"
    assert req.market_snapshot == {"kospi": {"last": 2860.12}}
    assert req.reason == "market moved"
