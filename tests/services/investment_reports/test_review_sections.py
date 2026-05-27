"""ROB-322 — deterministic five-section review projection.

These tests pin the *view-layer* mapping: the five `/invest/reports`
review sections are derived from the already-locked ROB-301
``decision_bucket`` vocabulary + ROB-318 report diagnostics. No new
``decision_bucket`` enum value / DB CHECK / persisted classification is
introduced — the assembler is a pure read-time projection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.schemas.investment_reports import InvestmentReportItemResponse
from app.services.investment_reports.review_sections import build_review_sections

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 5, 27, tzinfo=UTC)

# Fixed display order required by ROB-322 §3.
_EXPECTED_ORDER = [
    "new_buy_candidate",
    "held_strategy_review",
    "watch_only",
    "excluded_or_unavailable",
]


def _item(
    decision_bucket: str | None = None,
    *,
    item_kind: str = "action",
    symbol: str = "005930",
    intent: str = "buy_review",
) -> InvestmentReportItemResponse:
    return InvestmentReportItemResponse(
        item_uuid=uuid4(),
        item_kind=item_kind,  # type: ignore[arg-type]
        symbol=symbol,
        side="buy",
        intent=intent,  # type: ignore[arg-type]
        target_kind="asset",
        priority=0,
        confidence=Decimal("80"),
        rationale="rationale",
        evidence_snapshot={},
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


def _section(result, key: str) -> list[InvestmentReportItemResponse]:
    for section in result.sections:
        if section.key == key:
            return section.items
    raise AssertionError(
        f"section {key!r} missing from {[s.key for s in result.sections]}"
    )


def test_sections_are_present_in_fixed_order_even_when_empty() -> None:
    result = build_review_sections([], diagnostics=None)
    assert [s.key for s in result.sections] == _EXPECTED_ORDER
    assert all(s.items == [] for s in result.sections)


def test_new_buy_candidate_bucket_maps_to_new_buy_section() -> None:
    item = _item("new_buy_candidate")
    result = build_review_sections([item], diagnostics=None)
    assert [i.item_uuid for i in _section(result, "new_buy_candidate")] == [
        item.item_uuid
    ]


def test_open_action_and_completed_or_existing_map_to_held_strategy_review() -> None:
    open_action = _item("open_action", intent="sell_review")
    completed = _item("completed_or_existing", intent="rebalance_review")
    result = build_review_sections([open_action, completed], diagnostics=None)
    held = {i.item_uuid for i in _section(result, "held_strategy_review")}
    assert held == {open_action.item_uuid, completed.item_uuid}


def test_risk_watch_maps_to_watch_only() -> None:
    item = _item("risk_watch", item_kind="watch", intent="risk_review")
    result = build_review_sections([item], diagnostics=None)
    assert [i.item_uuid for i in _section(result, "watch_only")] == [item.item_uuid]


def test_deferred_no_action_maps_to_excluded_or_unavailable() -> None:
    item = _item("deferred_no_action")
    result = build_review_sections([item], diagnostics=None)
    assert [i.item_uuid for i in _section(result, "excluded_or_unavailable")] == [
        item.item_uuid
    ]


def test_legacy_items_without_decision_bucket_are_not_projected() -> None:
    # Backward-compat: pre-ROB-308 reports carry decision_bucket=None.
    # They must NOT be force-classified into any review section.
    legacy = _item(None)
    result = build_review_sections([legacy], diagnostics=None)
    assert all(
        legacy.item_uuid not in {i.item_uuid for i in s.items} for s in result.sections
    )


def test_no_action_summary_is_derived_from_why_no_action_diagnostics() -> None:
    diagnostics = {
        "why_no_action": {
            "kind": "stale_gated",
            "blocking_sources": ["market"],
            "reason_ko": "스냅샷 stale — market 신선도 부족으로 매수/매도 권고 보류",
        }
    }
    result = build_review_sections([], diagnostics=diagnostics)
    summary = result.no_action_summary
    assert summary is not None
    assert summary.kind == "stale_gated"
    assert summary.blocking_sources == ["market"]
    assert summary.reason_ko and "stale" in summary.reason_ko


def test_no_action_summary_counts_excluded_items() -> None:
    items = [_item("deferred_no_action"), _item("deferred_no_action")]
    result = build_review_sections(items, diagnostics=None)
    assert result.no_action_summary is not None
    assert result.no_action_summary.excluded_count == 2


def test_no_action_summary_none_when_no_diagnostics_and_nothing_excluded() -> None:
    result = build_review_sections([_item("new_buy_candidate")], diagnostics=None)
    assert result.no_action_summary is None
