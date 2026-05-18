"""ROB-265 Plan 2 — Pydantic schema validator tests.

Schema-level rejection of invariants that the DB also enforces via CHECK.
We want callers to get a clean ValidationError, not an IntegrityError.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.investment_reports import (
    ActivateWatchRequest,
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    WatchConditionPayload,
)


def _future(days: int = 7) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


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


def test_action_item_round_trip() -> None:
    item = IngestReportItem(
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        target_kind="asset",
        rationale="r",
    )
    assert item.item_kind == "action"
    assert item.watch_condition is None


def test_watch_item_requires_watch_condition() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            item_kind="watch",
            symbol="005930",
            intent="trend_recovery_review",
            rationale="r",
            valid_until=_future(),
        )
    assert "watch_condition" in str(exc_info.value)


def test_watch_item_requires_valid_until() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestReportItem(
            item_kind="watch",
            symbol="005930",
            intent="trend_recovery_review",
            rationale="r",
            watch_condition=WatchConditionPayload(
                metric="rsi", operator="below", threshold=30
            ),
        )
    assert "valid_until" in str(exc_info.value)


def test_watch_item_full_inserts() -> None:
    item = IngestReportItem(
        item_kind="watch",
        symbol="005930",
        intent="trend_recovery_review",
        rationale="r",
        watch_condition=WatchConditionPayload(
            metric="rsi", operator="below", threshold=30
        ),
        valid_until=_future(),
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


def test_activate_watch_request_minimal() -> None:
    req = ActivateWatchRequest(item_uuid=uuid.uuid4(), actor="operator-test")
    assert req.idempotency_key is None
