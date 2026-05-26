"""ROB-322 — deterministic five-section review projection.

Turns the flat report-item list into the KR ``/invest/reports`` actionable
review surface:

1. 신규매수 후보            (``new_buy_candidate``)
2. 보유종목 전략 변경 후보  (``held_strategy_review``)
3. watch-only              (``watch_only``)
4. 제외 / 확인 불가         (``excluded_or_unavailable``)
5. no-action summary       (``no_action_summary``)

This is a pure, read-time *view-layer mapping* over the already-locked
ROB-301 ``decision_bucket`` vocabulary + ROB-318 report diagnostics. It does
**not** introduce a new ``decision_bucket`` value, DB CHECK, or persisted
classification, and it does not invent a trading-strategy engine.

Backward-compat: items whose ``decision_bucket`` is ``None`` (pre-ROB-308
reports) are intentionally *not* projected into any section; they stay
available via the bundle's ``items`` / ``item_groups`` for safe rendering.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.schemas.investment_reports import (
    InvestmentReportItemResponse,
    NoActionSummary,
    ReportReviewSections,
    ReviewSection,
)

# Locked ROB-301 decision_bucket -> display section key. The two "held"
# buckets collapse into one strategy-review queue; legacy/unknown buckets map
# to ``None`` (not projected).
_BUCKET_TO_SECTION: dict[str, str] = {
    "new_buy_candidate": "new_buy_candidate",
    "open_action": "held_strategy_review",
    "completed_or_existing": "held_strategy_review",
    "risk_watch": "watch_only",
    "deferred_no_action": "excluded_or_unavailable",
}

# Fixed display order + Korean labels required by ROB-322 §3.
_SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("new_buy_candidate", "신규매수 후보"),
    ("held_strategy_review", "보유종목 전략 변경 후보"),
    ("watch_only", "watch-only"),
    ("excluded_or_unavailable", "제외 / 확인 불가"),
)


def build_review_sections(
    items: Sequence[InvestmentReportItemResponse],
    diagnostics: Mapping[str, Any] | None,
) -> ReportReviewSections:
    """Project report items into the five-section review surface.

    ``diagnostics`` is the report's ``snapshot_report_diagnostics`` block
    (or ``None`` on legacy reports).
    """
    buckets: dict[str, list[InvestmentReportItemResponse]] = {
        key: [] for key, _ in _SECTION_ORDER
    }
    for item in items:
        section_key = _BUCKET_TO_SECTION.get(item.decision_bucket or "")
        if section_key is not None:
            buckets[section_key].append(item)

    sections = [
        ReviewSection(key=key, label_ko=label, items=buckets[key])  # type: ignore[arg-type]
        for key, label in _SECTION_ORDER
    ]

    no_action_summary = _build_no_action_summary(
        diagnostics, excluded_count=len(buckets["excluded_or_unavailable"])
    )
    return ReportReviewSections(sections=sections, no_action_summary=no_action_summary)


def _build_no_action_summary(
    diagnostics: Mapping[str, Any] | None,
    *,
    excluded_count: int,
) -> NoActionSummary | None:
    why: Mapping[str, Any] | None = None
    if isinstance(diagnostics, Mapping):
        candidate = diagnostics.get("why_no_action")
        if isinstance(candidate, Mapping):
            why = candidate

    # Nothing to summarise: no diagnostics verdict and no excluded items.
    if why is None and excluded_count == 0:
        return None

    why = why or {}
    blocking = why.get("blocking_sources") or []
    return NoActionSummary(
        kind=why.get("kind"),
        reason_ko=why.get("reason_ko"),
        blocking_sources=[str(s) for s in blocking],
        excluded_count=excluded_count,
    )
