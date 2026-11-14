"""Shared evidence-boundary vocabulary for the reconcile kernels (hk 4606).

Both reconcile kernels — the generic live kernel used by KIS US and the
Toss kernel — evaluate broker evidence through one boundary before any
terminal transition or fill booking, in every reconcile scope (``full`` and
``terminal_only``):

* ``usable``        — the evidence may act: a fill verdict may book/defer
                      the fill, a terminal no-fill verdict may close the row.
* ``contradictory`` — broker terminal no-fill evidence while positive fills
                      are already recorded for the same order identity; the
                      row stays open with ``reason_code=evidence_contradiction``.
* ``incomplete``    — the verdict carries missing or invalid order/fill
                      quantities or price fields; the row stays open with
                      ``reason_code=evidence_incomplete``.

``usable`` evidence keeps every existing behavior; only contradictory and
incomplete evidence change the outcome (open + manual review instead of a
close or booking).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class EvidenceBoundary(StrEnum):
    USABLE = "usable"
    CONTRADICTORY = "contradictory"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class EvidenceBoundaryVerdict:
    boundary: EvidenceBoundary
    reason_code: str | None = None
    detail: str = ""


USABLE = EvidenceBoundaryVerdict(EvidenceBoundary.USABLE)

REASON_CONTRADICTION = "evidence_contradiction"
REASON_INCOMPLETE = "evidence_incomplete"


def contradictory(detail: str) -> EvidenceBoundaryVerdict:
    return EvidenceBoundaryVerdict(
        EvidenceBoundary.CONTRADICTORY, REASON_CONTRADICTION, detail
    )


def incomplete(detail: str) -> EvidenceBoundaryVerdict:
    return EvidenceBoundaryVerdict(
        EvidenceBoundary.INCOMPLETE, REASON_INCOMPLETE, detail
    )


def qty_is_invalid(qty: Decimal | None, *, integer: bool) -> bool:
    """Missing, non-finite, non-positive or non-integer fill quantity."""
    if qty is None or not qty.is_finite() or qty <= 0:
        return True
    return integer and qty != qty.to_integral_value()


def price_is_invalid(price: Decimal | None) -> bool:
    """Missing, non-finite or non-positive average fill price."""
    return price is None or not price.is_finite() or price <= 0
