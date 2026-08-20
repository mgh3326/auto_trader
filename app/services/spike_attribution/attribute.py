"""ROB-1303 attribution assembly (pure).

Takes a detected spike plus whatever the readers found, and rules on it. The
only two outcomes are *these documents are eligible causes* and *nothing in the
materials explains this move*. There is no third bucket, and the second one is
never dressed up as "기타" or "시장 전반" — the value of the record is that it
admits ignorance in the same words every time.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.spike_attribution.contract import (
    ELIGIBILITY_AFTER_MOVE,
    ELIGIBILITY_BEFORE_WINDOW,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_TIMESTAMP_UNKNOWN,
    EvidenceItem,
    SpikeAttribution,
    SpikeEvent,
    SpikeMaterials,
)
from app.services.spike_attribution.spec import (
    ATTRIBUTED_TYPES,
    EXPERIMENT_ID,
    PRE_REGISTRATION,
    spec_sha256,
)

_ATTRIBUTION = PRE_REGISTRATION["attribution"]
_TYPE_PRIORITY: tuple[str, ...] = tuple(_ATTRIBUTION["type_priority"])

UNATTRIBUTED = "unattributed"

# The exact wording used whenever nothing explains the move. Fixed on purpose:
# a stable phrase is greppable and cannot drift into a euphemism.
UNATTRIBUTED_PHRASE = (
    "재료로 설명되지 않음 — 증거 창 안에 적격 문서가 없다 (no eligible evidence "
    "inside the pre-move window)"
)


class AttributionError(ValueError):
    """Raised when the record could not be built deterministically."""


def rule_eligibility(
    *,
    published_at: dt.datetime | None,
    window_start_exclusive: dt.datetime,
    window_end_inclusive: dt.datetime,
) -> str:
    """Classify one document against the pre-move window.

    A document with no usable timestamp is never eligible: we cannot show it
    preceded the move, and assuming it did is exactly the invention this record
    exists to prevent.
    """

    if published_at is None:
        return ELIGIBILITY_TIMESTAMP_UNKNOWN
    if published_at.tzinfo is None:
        raise AttributionError("published_at must be timezone-aware")
    if published_at <= window_start_exclusive:
        return ELIGIBILITY_BEFORE_WINDOW
    if published_at > window_end_inclusive:
        return ELIGIBILITY_AFTER_MOVE
    return ELIGIBILITY_ELIGIBLE


def _type_rank(attribution_type: str) -> int:
    try:
        return _TYPE_PRIORITY.index(attribution_type)
    except ValueError:
        return len(_TYPE_PRIORITY)


def _sort_key(item: EvidenceItem) -> tuple[int, float, str]:
    # Type priority first, then most recent inside the window. A missing
    # timestamp cannot reach this path (it is never eligible), so the fallback
    # only guards against a caller passing a rejected item in by mistake.
    ts = item.published_at.timestamp() if item.published_at else float("-inf")
    return (_type_rank(item.attribution_type), -ts, item.title)


def correlation_id(event: SpikeEvent) -> str:
    return (
        f"{EXPERIMENT_ID}:{event.market}:{event.symbol}:"
        f"{event.session_date.isoformat()}"
    )


def _unattributed_reason(
    event: SpikeEvent, rejected: tuple[EvidenceItem, ...], materials: SpikeMaterials
) -> str:
    counts: dict[str, int] = {}
    for item in rejected:
        counts[item.eligibility] = counts.get(item.eligibility, 0) + 1
    unavailable = [row.material for row in materials.availability if not row.available]
    # "the source was readable and held nothing for this symbol" is a different
    # statement from "the source could not be read", and a reader who cannot
    # tell them apart cannot tell a coverage gap from a genuine blank.
    empty = [
        row.material
        for row in materials.availability
        if row.available and row.detail.get("rows") == 0
    ]
    parts = [UNATTRIBUTED_PHRASE]
    if counts:
        detail = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        parts.append(f"창 밖/판정불가 문서 {detail}")
    if empty:
        parts.append(f"조회됐으나 행 0 (커버리지 공백 가능) {', '.join(sorted(empty))}")
    if unavailable:
        parts.append(f"조회 불가 재료 {', '.join(sorted(unavailable))}")
    parts.append(
        f"window=({event.window_start_exclusive.isoformat()}, "
        f"{event.window_end_inclusive.isoformat()}]"
    )
    return " | ".join(parts)


def build_attribution(
    *, event: SpikeEvent, materials: SpikeMaterials
) -> SpikeAttribution:
    """Build the record for one spike event."""

    eligible: list[EvidenceItem] = []
    rejected: list[EvidenceItem] = []
    for item in materials.evidence:
        if item.attribution_type not in ATTRIBUTED_TYPES:
            raise AttributionError(
                f"evidence carries a non-cause type: {item.attribution_type!r}"
            )
        (eligible if item.is_eligible else rejected).append(item)

    eligible.sort(key=_sort_key)
    rejected.sort(key=_sort_key)

    # Every distinct type that has at least one eligible document survives.
    # Collapsing to the top-ranked one would be a single-cause declaration.
    types: list[str] = []
    for item in eligible:
        if item.attribution_type not in types:
            types.append(item.attribution_type)

    unattributed = not eligible
    return SpikeAttribution(
        event=event,
        attribution_types=tuple(types),
        candidates=tuple(eligible),
        rejected=tuple(rejected),
        availability=materials.availability,
        unattributed=unattributed,
        unattributed_reason=(
            _unattributed_reason(event, tuple(rejected), materials)
            if unattributed
            else None
        ),
        spec_sha256=spec_sha256(),
        correlation_id=correlation_id(event),
    )


def scored_class(attribution: SpikeAttribution) -> str:
    """The single follow-through class this event is scored under.

    Multiple candidate types are all kept on the record; the scoring class is
    the highest-priority one purely so per-type follow-through has a stable
    denominator. This is a bookkeeping choice, not a claim that the other
    candidates were ruled out.
    """

    if attribution.unattributed:
        return UNATTRIBUTED
    return attribution.attribution_types[0]


def record_summary(attribution: SpikeAttribution) -> dict[str, Any]:
    """Compact, JSON-safe view for reports and CLI output."""

    return {
        "symbol": attribution.event.symbol,
        "session_date": attribution.event.session_date.isoformat(),
        "direction": attribution.event.direction,
        "close_to_close_pct": str(attribution.event.close_to_close_pct),
        "intraday_extreme_pct": str(attribution.event.intraday_extreme_pct),
        "triggered_bases": list(attribution.event.triggered_bases),
        "attribution_types": list(attribution.attribution_types),
        "scored_class": scored_class(attribution),
        "candidate_count": len(attribution.candidates),
        "unattributed": attribution.unattributed,
        "unattributed_reason": attribution.unattributed_reason,
    }


__all__ = [
    "UNATTRIBUTED",
    "UNATTRIBUTED_PHRASE",
    "AttributionError",
    "build_attribution",
    "correlation_id",
    "record_summary",
    "rule_eligibility",
    "scored_class",
]
