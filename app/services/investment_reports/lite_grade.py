"""ROB-472 — deterministic lite quality grade for Claude advisory reports.

The snapshot-backed generator grades reports from snapshot bundle coverage
(build_report_quality_summary). The lite create path has no snapshot bundle, so
this derives an HONEST grade from the per-item structured evidence shipped in
ROB-459 P1. By construction it NEVER returns high_confidence — a lite report
lacks the snapshot coverage that grade is defined around — so an evidence-thin
report can never masquerade as snapshot-backed high confidence.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.schemas.investment_reports import IngestReportItem

_LITE_BASIS = "item_evidence_lite"
_ACTIONABLE_KINDS = frozenset({"action", "watch"})
_FRESHNESS_VALUES = ("fresh", "soft_stale", "stale", "unknown")


def build_lite_report_quality_summary(
    items: list[IngestReportItem],
) -> dict[str, Any]:
    """Derive a lite report_quality_summary from per-item evidence/freshness.

    Grade is capped at ``informational_only`` (never ``high_confidence``):
    - ``no_action``: no actionable (action|watch) items, OR no item carries any
      structured evidence — genuinely insufficient to advise.
    - ``informational_only``: otherwise — an evidence-backed lite advisory.

    ``freshness_breakdown`` counts both item-level ``freshness`` and each
    evidence row's ``freshness`` (None values are not counted).
    """
    total_item_count = len(items)
    actionable_item_count = sum(1 for it in items if it.item_kind in _ACTIONABLE_KINDS)
    evidence_item_count = sum(1 for it in items if it.evidence)

    sources: set[str] = set()
    freshness_counter: Counter[str] = Counter()
    for it in items:
        if it.freshness is not None:
            freshness_counter[it.freshness] += 1
        for ev in it.evidence:
            sources.add(ev.source)
            if ev.freshness is not None:
                freshness_counter[ev.freshness] += 1

    if actionable_item_count == 0 or evidence_item_count == 0:
        grade = "no_action"
        reason = (
            "no actionable (action|watch) items"
            if actionable_item_count == 0
            else "no structured evidence on any item"
        )
    else:
        grade = "informational_only"
        reason = "evidence-backed lite advisory (no snapshot coverage)"

    return {
        "grade": grade,
        "basis": _LITE_BASIS,
        "reason": reason,
        "total_item_count": total_item_count,
        "actionable_item_count": actionable_item_count,
        "evidence_item_count": evidence_item_count,
        "evidence_source_count": len(sources),
        "freshness_breakdown": {
            k: freshness_counter.get(k, 0) for k in _FRESHNESS_VALUES
        },
    }
