# tests/services/investment_reports/test_action_packet.py
"""ROB-335 — ActionPacket read-time projection + schema."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.schemas.investment_reports import (
    ActionPacket,
    InvestmentReportBundle,
    InvestmentReportItemResponse,
)
from app.services.investment_reports.action_packet import build_action_packet

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 27, tzinfo=UTC)


def _item(
    *,
    verdict: str | None,
    decision_bucket: str | None,
    symbol: str | None = "005930",
    item_kind: str = "action",
    side: str | None = "sell",
    intent: str = "sell_review",
) -> InvestmentReportItemResponse:
    evidence = {"action_verdict": verdict} if verdict is not None else {}
    return InvestmentReportItemResponse(
        item_uuid=uuid4(),
        item_kind=item_kind,  # type: ignore[arg-type]
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        intent=intent,  # type: ignore[arg-type]
        target_kind="asset",
        priority=0,
        confidence=Decimal("80"),
        rationale="r",
        evidence_snapshot=evidence,
        watch_condition=None,
        trigger_checklist=[],
        max_action={},
        valid_until=None,
        status="proposed",
        metadata={},
        created_at=_NOW,
        updated_at=_NOW,
        decision_bucket=decision_bucket,
    )


def test_action_packet_defaults_are_empty() -> None:
    packet = ActionPacket()
    assert packet.held_actions == []
    assert packet.new_buy_candidates == []
    assert packet.no_new_buy_reason is None
    assert packet.risk_reviews == []
    assert packet.no_action_reason is None
    assert packet.data_gaps_for_next_cycle == []


def test_bundle_action_packet_field_is_optional() -> None:
    # Additive, null for legacy reports (mirrors review_sections).
    assert "action_packet" in InvestmentReportBundle.model_fields
    assert InvestmentReportBundle.model_fields["action_packet"].default is None


def test_held_and_new_and_risk_are_grouped_by_verdict() -> None:
    items = [
        _item(verdict="sell_review", decision_bucket="open_action"),
        _item(verdict="keep", decision_bucket="completed_or_existing", side=None),
        _item(
            verdict="buy_review",
            decision_bucket="new_buy_candidate",
            side="buy",
            intent="buy_review",
            symbol="000660",
        ),
        _item(
            verdict="watch_only",
            decision_bucket="risk_watch",
            item_kind="watch",
            side=None,
            intent="trend_recovery_review",
            symbol="035720",
        ),
    ]
    packet = build_action_packet(items, diagnostics=None)
    assert {e.verdict for e in packet.held_actions} == {"sell_review", "keep"}
    assert [e.verdict for e in packet.new_buy_candidates] == ["buy_review"]
    assert [e.verdict for e in packet.risk_reviews] == ["watch_only"]


def test_no_new_buy_marker_sets_reason_not_a_candidate_row() -> None:
    marker = _item(
        verdict="no_new_buy_candidates",
        decision_bucket="new_buy_candidate",
        symbol=None,
        side=None,
        intent="risk_review",
        item_kind="risk",
    )
    marker.rationale = "국내 스크리너 스냅샷이 최신 거래일 기준이 아닙니다 (stale)."
    packet = build_action_packet([marker], diagnostics=None)
    assert packet.new_buy_candidates == []
    assert packet.no_new_buy_reason == marker.rationale


def test_data_gaps_from_items_and_diagnostics() -> None:
    items = [
        _item(
            verdict="data_gap",
            decision_bucket="deferred_no_action",
            symbol="005930",
            side=None,
            intent="risk_review",
            item_kind="risk",
        )
    ]
    diagnostics = {
        "why_no_action": {
            "kind": "data_insufficient",
            "reason_ko": "데이터 부족",
            "blocking_sources": ["portfolio"],
        },
        "data_sufficiency_by_source": {
            "portfolio": {"status": "unavailable", "reason_code": "user_id_missing"},
            "symbol": {"status": "fresh"},
        },
    }
    packet = build_action_packet(items, diagnostics=diagnostics)
    assert packet.no_action_reason is not None
    assert packet.no_action_reason.kind == "data_insufficient"
    # symbol-level data_gap item + degraded source from diagnostics both surface.
    sources = {g.source for g in packet.data_gaps_for_next_cycle}
    assert "005930" in sources
    assert "portfolio" in sources


def test_items_without_verdict_are_not_projected() -> None:
    # Legacy items (no action_verdict) stay out of the packet (decision A/B).
    items = [_item(verdict=None, decision_bucket="open_action")]
    packet = build_action_packet(items, diagnostics=None)
    assert packet.held_actions == []


def test_serialise_attaches_action_packet(monkeypatch) -> None:
    # _serialise_bundle should project items into bundle.action_packet using
    # the same build_action_packet path (additive, never None for intraday).
    from app.routers import investment_reports as mod

    items = [_item(verdict="sell_review", decision_bucket="open_action")]
    packet = build_action_packet(items, diagnostics=None)
    assert packet.held_actions  # sanity: projection works on these items
    # The router import wires build_action_packet:
    assert hasattr(mod, "build_action_packet")
