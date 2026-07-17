"""ROB-334 — pure KIS-mock fill-evidence classifier.

Maps KIS daily order-execution rows (``inquire_daily_order_domestic`` raw
``output1``) for a given order number into a ``FillEvidence`` verdict plus a
fail-closed category. stdlib-only: no broker / DB / network import, so the gate
logic is unit-tested in isolation and cannot fabricate a fill.

The daily-execution field names differ across KIS surfaces, so each value is
resolved against ordered candidate keys, case-insensitively. The read-only
smoke (scripts/kis_mock_fill_evidence_smoke.py) confirms the real keys.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any


class FillVerdict(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    PENDING = "pending"
    EXPIRED = "expired"
    NONE = "none"
    UNSUPPORTED = "unsupported"


class EvidenceCategory(StrEnum):
    """Issue ROB-334 fail-closed failure categories."""

    CODE = "code"
    ENV_CONFIG = "env/config"
    DATA_PRECONDITION = "data-precondition"
    UNSUPPORTED_MOCK_API = "unsupported mock API"
    OPERATOR_APPROVAL_NEEDED = "operator approval needed"


@dataclass(frozen=True)
class FillEvidence:
    verdict: FillVerdict
    filled_qty: Decimal | None
    avg_price: Decimal | None
    category: EvidenceCategory | None  # populated only for fail-closed verdicts
    reason_code: str
    detail: str


_ORDER_NO_KEYS = ("odno", "ord_no")
_ORD_QTY_KEYS = ("ord_qty",)
_FILLED_QTY_KEYS = ("tot_ccld_qty", "ccld_qty")
_AVG_PRICE_KEYS = ("avg_prvs", "ccld_unpr", "ccld_avg_unpr")
_FILLED_AMT_KEYS = ("tot_ccld_amt", "ccld_amt")


def _lower_keys(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop byte-for-byte duplicate broker rows after key normalization.

    KIS daily-order inquiry can return the same order row more than once across
    pages / query shapes.  The filled-quantity fields are then cumulative for
    the order, not independent fills, so counting exact duplicates would
    overstate fills and could over-book live journals.
    """

    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(sorted((str(k), str(v)) for k, v in row.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _get_field(lowered: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for c in candidates:
        if c in lowered and lowered[c] not in (None, ""):
            return lowered[c]
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sum_decimals(values: Iterable[Any]) -> Decimal | None:
    total = Decimal("0")
    saw_any = False
    for v in values:
        d = _to_decimal(v)
        if d is None:
            return None
        total += d
        saw_any = True
    return total if saw_any else None


def _order_no_matches(target: str, candidate: Any) -> bool:
    cand = str(candidate).strip()
    if not cand:
        return False
    return cand == target or cand.lstrip("0") == target.lstrip("0")


def classify_fill_evidence(
    *, order_no: str | None, rows: list[dict[str, Any]]
) -> FillEvidence:
    """Classify fill evidence for ``order_no`` from daily-execution rows."""
    target = (order_no or "").strip()
    if not target:
        return FillEvidence(
            FillVerdict.NONE,
            None,
            None,
            EvidenceCategory.DATA_PRECONDITION,
            "order_no_missing",
            "no order number to match",
        )

    matched = _dedupe_rows(
        _lower_keys(r)
        for r in rows
        if _order_no_matches(target, _get_field(_lower_keys(r), _ORDER_NO_KEYS))
    )
    if not matched:
        return FillEvidence(
            FillVerdict.NONE,
            None,
            None,
            EvidenceCategory.DATA_PRECONDITION,
            "no_matching_order",
            f"no daily-execution row for odno={target}",
        )

    ord_qty = _to_decimal(_get_field(matched[0], _ORD_QTY_KEYS))
    filled_qty = _sum_decimals(_get_field(m, _FILLED_QTY_KEYS) for m in matched)
    if ord_qty is None or filled_qty is None:
        return FillEvidence(
            FillVerdict.NONE,
            None,
            None,
            EvidenceCategory.CODE,
            "unparseable_qty",
            "could not parse ord_qty / filled_qty",
        )

    if filled_qty <= 0:
        return FillEvidence(
            FillVerdict.PENDING,
            Decimal("0"),
            None,
            None,
            "pending",
            f"order {target} accepted, no fill yet",
        )

    avg_price = _resolve_avg_price(matched, filled_qty)
    if avg_price is None or avg_price <= 0:
        return FillEvidence(
            FillVerdict.NONE,
            filled_qty,
            None,
            EvidenceCategory.CODE,
            "missing_fill_price",
            "filled qty present but no usable fill price",
        )

    if ord_qty > 0 and filled_qty >= ord_qty:
        return FillEvidence(
            FillVerdict.FILLED,
            filled_qty,
            avg_price,
            None,
            "filled",
            f"order {target} filled {filled_qty}@{avg_price}",
        )
    return FillEvidence(
        FillVerdict.PARTIAL,
        filled_qty,
        avg_price,
        None,
        "partial_fill",
        f"order {target} partial {filled_qty}/{ord_qty}",
    )


def _resolve_avg_price(
    matched: list[dict[str, Any]], filled_qty: Decimal
) -> Decimal | None:
    for m in matched:
        p = _to_decimal(_get_field(m, _AVG_PRICE_KEYS))
        if p is not None and p > 0:
            return p
    amt = _sum_decimals(_get_field(m, _FILLED_AMT_KEYS) for m in matched)
    if amt is not None and amt > 0 and filled_qty > 0:
        return amt / filled_qty
    return None
