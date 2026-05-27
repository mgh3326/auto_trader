# tests/services/action_report/snapshot_backed/test_intraday_floor.py
"""ROB-335 — intraday non-empty floor guard."""

from __future__ import annotations

import pytest

from app.schemas.investment_reports import IngestReportItem
from app.services.action_report.snapshot_backed.intraday_floor import (
    ensure_action_floor,
    is_intraday_action,
)

pytestmark = pytest.mark.unit


def test_is_intraday_action_matches_policy_version() -> None:
    assert is_intraday_action("intraday_action_report_v1") is True
    assert is_intraday_action("snapshot_backed_advisory_v1") is False
    assert is_intraday_action(None) is False


def test_floor_synthesizes_one_item_when_empty() -> None:
    why = {"kind": "data_insufficient", "reason_ko": "데이터 부족 — portfolio 확인 불가",
           "blocking_sources": ["portfolio"]}
    out = ensure_action_floor([], why_no_action=why)
    assert len(out) == 1
    item = out[0]
    assert item.evidence_snapshot["action_verdict"] == "data_gap"
    assert item.decision_bucket == "deferred_no_action"
    assert item.rationale == why["reason_ko"]


def test_floor_real_no_action_uses_no_action_verdict() -> None:
    why = {"kind": "real_no_action", "reason_ko": "데이터 충분 — 현 시점 신규 액션 없음(관망)",
           "blocking_sources": []}
    out = ensure_action_floor([], why_no_action=why)
    assert out[0].evidence_snapshot["action_verdict"] == "keep"
    assert out[0].decision_bucket == "completed_or_existing"


def test_floor_is_noop_when_items_present() -> None:
    existing = [
        IngestReportItem(
            client_item_key="x", item_kind="action", symbol="005930", side="sell",
            intent="sell_review", rationale="r", operation="review",
            apply_policy="requires_user_approval",
            evidence_snapshot={"action_verdict": "sell_review"},
            decision_bucket="open_action",
        )
    ]
    out = ensure_action_floor(existing, why_no_action=None)
    assert out == existing
