"""ROB-1303 attribution record dataclasses (pure, stdlib only).

The record is the deliverable: a spike event, every evidence item that was
considered (eligible *and* rejected, with the reason), and the resulting
attribution candidates. ``unattributed`` is a first-class verdict, not a
leftover bucket — it means the materials did not explain the move, and the
record says so in those words.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# Why an evidence item is / is not allowed to explain the session move.
ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_AFTER_MOVE = "after_move"
ELIGIBILITY_BEFORE_WINDOW = "before_window"
ELIGIBILITY_TIMESTAMP_UNKNOWN = "timestamp_unknown"
# The external ROB-491 judge ruled this article unrelated to the symbol. That
# judgment is authoritative and this code honours it — but the row stays on the
# record with this reason rather than disappearing.
ELIGIBILITY_JUDGED_NOT_RELEVANT = "judged_not_relevant"

# Why a material could not be consulted at all (never confused with "no cause").
UNAVAILABLE_T_PLUS_1 = "unavailable_t_plus_1"
UNAVAILABLE_NO_COVERAGE = "unavailable_no_coverage"


@dataclass(frozen=True)
class DailyBar:
    """One daily OHLCV row. ``session_date`` is the local trading date."""

    symbol: str
    session_date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class SpikeEvent:
    """A detected session move that cleared the pre-registered threshold."""

    market: str
    symbol: str
    session_date: dt.date
    direction: str  # "up" | "down"
    prev_close: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    close_to_close_pct: Decimal
    intraday_extreme_pct: Decimal
    triggered_bases: tuple[str, ...]
    window_start_exclusive: dt.datetime
    window_end_inclusive: dt.datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "direction": self.direction,
            "prev_close": str(self.prev_close),
            "close": str(self.close),
            "high": str(self.high),
            "low": str(self.low),
            "close_to_close_pct": str(self.close_to_close_pct),
            "intraday_extreme_pct": str(self.intraday_extreme_pct),
            "triggered_bases": list(self.triggered_bases),
            "evidence_window": {
                "start_exclusive": self.window_start_exclusive.isoformat(),
                "end_inclusive": self.window_end_inclusive.isoformat(),
            },
        }


@dataclass(frozen=True)
class EvidenceItem:
    """One candidate cause document, with its link and its eligibility ruling."""

    attribution_type: str
    source: str
    title: str
    url: str | None
    published_at: dt.datetime | None
    published_at_precision: str  # "exact" | "date_only" | "unknown"
    published_at_source: str  # which column/key the timestamp came from
    eligibility: str
    judgment: str  # "judged_relevant" | "unjudged" | "not_applicable"
    judgment_detail: dict[str, Any] = field(default_factory=dict)
    ref: dict[str, Any] = field(default_factory=dict)

    @property
    def is_eligible(self) -> bool:
        return self.eligibility == ELIGIBILITY_ELIGIBLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "attribution_type": self.attribution_type,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "published_at": (
                self.published_at.isoformat() if self.published_at else None
            ),
            "published_at_precision": self.published_at_precision,
            "published_at_source": self.published_at_source,
            "eligibility": self.eligibility,
            "judgment": self.judgment,
            "judgment_detail": dict(self.judgment_detail),
            "ref": dict(self.ref),
        }


@dataclass(frozen=True)
class MaterialAvailability:
    """Per-material availability, so 'no data' never reads as 'no cause'."""

    material: str
    available: bool
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "available": self.available,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class SpikeMaterials:
    """Everything the readers found for one spike event, already timestamped."""

    evidence: tuple[EvidenceItem, ...]
    availability: tuple[MaterialAvailability, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence": [item.as_dict() for item in self.evidence],
            "availability": [row.as_dict() for row in self.availability],
        }


@dataclass(frozen=True)
class SpikeAttribution:
    """The record. ``attribution_types`` is empty exactly when unattributed."""

    event: SpikeEvent
    attribution_types: tuple[str, ...]
    candidates: tuple[EvidenceItem, ...]
    rejected: tuple[EvidenceItem, ...]
    availability: tuple[MaterialAvailability, ...]
    unattributed: bool
    unattributed_reason: str | None
    spec_sha256: str
    correlation_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.as_dict(),
            "attribution_types": list(self.attribution_types),
            "unattributed": self.unattributed,
            "unattributed_reason": self.unattributed_reason,
            "candidates": [item.as_dict() for item in self.candidates],
            "rejected_evidence": [item.as_dict() for item in self.rejected],
            "material_availability": [row.as_dict() for row in self.availability],
            "spec_sha256": self.spec_sha256,
            "correlation_id": self.correlation_id,
            "promote": False,
            "live_gate_impact": False,
        }


__all__ = [
    "ELIGIBILITY_AFTER_MOVE",
    "ELIGIBILITY_BEFORE_WINDOW",
    "ELIGIBILITY_ELIGIBLE",
    "ELIGIBILITY_JUDGED_NOT_RELEVANT",
    "ELIGIBILITY_TIMESTAMP_UNKNOWN",
    "UNAVAILABLE_NO_COVERAGE",
    "UNAVAILABLE_T_PLUS_1",
    "DailyBar",
    "EvidenceItem",
    "MaterialAvailability",
    "SpikeAttribution",
    "SpikeEvent",
    "SpikeMaterials",
]
