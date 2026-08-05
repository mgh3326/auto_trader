"""Shared gate-result vocabulary for the KR-B1 P0-3 selector proof path.

Every gate answers exactly one question: *can this be proven with evidence that
already existed at ``decision_at``?* A gate is ``proven`` only when the answer is
yes. Everything else is ``unprovable`` with a named reason, and one unprovable
gate fails the whole selector closed.

This module is pure: no clock, database, network, broker, order, scheduler, or
file surface is reachable from here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

GateStatus = Literal["proven", "unprovable"]

KST = dt.timezone(dt.timedelta(hours=9))
HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class GateResult:
    """One gate verdict plus the evidence that produced it."""

    status: GateStatus
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_proven(self) -> bool:
        return self.status == "proven"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence": normalize_evidence(self.evidence),
        }


def proven(reason: str, **evidence: Any) -> GateResult:
    return GateResult(status="proven", reason=reason, evidence=dict(evidence))


def unprovable(reason: str, **evidence: Any) -> GateResult:
    return GateResult(status="unprovable", reason=reason, evidence=dict(evidence))


def normalize_evidence(value: Any) -> Any:
    """Return a JSON-compatible projection with ISO-8601 timestamps."""
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize_evidence(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set | frozenset):
        items = sorted(value, key=repr) if isinstance(value, set | frozenset) else value
        return [normalize_evidence(item) for item in items]
    return value


def is_aware(value: object) -> bool:
    """True only for timezone-aware datetimes.

    A naive datetime cannot be compared against ``decision_at`` without guessing
    an offset, and guessing is what would let post-decision evidence slip in.
    """
    return (
        isinstance(value, dt.datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def is_sha256_hex(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value).issubset(HEX_DIGITS)


def to_kst(value: dt.datetime) -> dt.datetime:
    return value.astimezone(KST)


def kst_datetime(session: dt.date, session_time: dt.time) -> dt.datetime:
    return dt.datetime.combine(session, session_time, tzinfo=KST)


def parse_nonnegative_int_string(value: object) -> int | None:
    """Parse an exact non-negative integer string; never coerce through float."""
    if type(value) is not str:
        return None
    text = value.strip()
    if not text or not text.isdigit():
        return None
    return int(text)


def examples(values: list[str], limit: int = 20) -> list[str]:
    return values[:limit]


__all__ = [
    "HEX_DIGITS",
    "KST",
    "GateResult",
    "GateStatus",
    "examples",
    "is_aware",
    "is_sha256_hex",
    "kst_datetime",
    "normalize_evidence",
    "parse_nonnegative_int_string",
    "proven",
    "to_kst",
    "unprovable",
]
