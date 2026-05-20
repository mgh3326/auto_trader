"""ROB-274 — operation-aware watch validator tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import (
    IngestReportItem,
    TargetRefPayload,
    WatchConditionPayload,
)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _watch_condition() -> WatchConditionPayload:
    return WatchConditionPayload(
        metric="price", operator="above", threshold=Decimal("100")
    )


def _target_ref() -> TargetRefPayload:
    return TargetRefPayload(
        type="investment_watch_alert",
        id=str(uuid4()),
        status="active",
    )


def test_watch_create_requires_watch_condition_and_valid_until():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="create",
            intent="buy_review",
            rationale="r",
            # missing watch_condition and valid_until
        )


def test_watch_modify_requires_target_ref_and_current_state():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="modify",
            intent="trend_recovery_review",
            rationale="r",
            watch_condition=_watch_condition(),
            valid_until=_now_utc(),
            # missing target_ref + current_state
        )


def test_watch_cancel_does_not_require_watch_condition():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="cancel",
        intent="risk_review",
        rationale="r",
        target_ref=_target_ref(),
        current_state={"metric": "price", "operator": "above", "threshold": "100"},
        apply_policy="requires_user_approval",
    )
    assert item.watch_condition is None
    assert item.valid_until is None


def test_watch_keep_requires_target_ref_and_current_state_only():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="keep",
        intent="risk_review",
        rationale="r",
        target_ref=_target_ref(),
        current_state={"metric": "price"},
    )
    assert item.operation == "keep"


def test_watch_review_accepts_ambiguous_target_list():
    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="review",
        intent="risk_review",
        rationale="r",
        target_ref=TargetRefPayload(
            type="ambiguous", id=None, candidates=[{"id": "a"}, {"id": "b"}]
        ),
        current_state={},
    )
    assert item.target_ref.type == "ambiguous"


def test_legacy_item_without_operation_keeps_old_invariants():
    # Legacy (operation=None) must still reject watch items without
    # watch_condition or valid_until so existing callers don't regress.
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            intent="buy_review",
            rationale="r",
        )


def test_apply_policy_is_locked_to_single_value():
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="watch",
            operation="cancel",
            intent="risk_review",
            rationale="r",
            target_ref=_target_ref(),
            current_state={},
            apply_policy="notify_only",  # not in this PR
        )


def test_action_cancel_requires_target_ref_when_present():
    # action/cancel proposals target an existing broker order.
    with pytest.raises(ValidationError):
        IngestReportItem(
            client_item_key="k1",
            item_kind="action",
            operation="cancel",
            intent="sell_review",
            rationale="r",
            # missing target_ref
        )
