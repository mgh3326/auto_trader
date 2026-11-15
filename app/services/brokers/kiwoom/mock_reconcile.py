"""hk 4722 (3797 T-P3) — Kiwoom mock order terminal classification.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §3.1–§3.2 and §6.1.

Pure module: broker payloads + a ledger snapshot in, a verdict out. No I/O, no
database, no client, no clock. The kernel (``app/services/kiwoom_mock_reconcile``)
fetches the broker payloads and applies the verdicts through the ledger service.

Invariants enforced here (design §6.1):

* **I4 filled only by broker delta** — ``filled``/``partial`` need a broker row
  with a positive cumulative filled quantity *and* a parseable price.
* **I5 absence is not evidence** — an unreadable, truncated or ambiguous listing
  and a missing exact ``ord_no`` row all produce ``unknown``, never a terminal
  verdict.
* **No inferred DAY expiry (operator Q4)** — ``expired``/``rejected`` need a
  positive broker marker on the exact row; a session simply ending is not
  expiry evidence. A broker row that is terminal-with-no-fill but carries no
  recognised marker is ``unknown`` rather than a guess.
* **Cancelled needs both halves (§3.2)** — our own acknowledged cancel request
  *and* a positive broker cancel confirmation on the original order.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.services.brokers.kiwoom.normalization import (
    KiwoomMockEvidenceError,
    normalize_order_detail,
    normalize_orders,
)

__all__ = (
    "BrokerEvidence",
    "BrokerRow",
    "LedgerOrder",
    "Verdict",
    "classify_verdict",
    "evidence_from_payloads",
    "return_code_ok",
)

#: Verdicts of the design §3.2 table, plus ``pending`` for a broker row that is
#: still working (PENDING stays open — it is not a terminal verdict).
VerdictName = Literal[
    "filled",
    "partial",
    "cancelled",
    "expired",
    "rejected",
    "accepted",
    "pending",
    "unknown",
]

SOURCE_ORDER_DETAIL: Final = "kt00007"
SOURCE_ORDER_STATUS: Final = "kt00009"

# Closed marker vocabulary. Only these broker-reported texts are positive
# terminal evidence; anything else is unclassified (``unknown``), never guessed.
_MARKERS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("cancel", ("취소", "cancel")),
    ("reject", ("거부", "reject")),
    ("expiry", ("만료", "expire")),
)


@dataclass(frozen=True, slots=True)
class LedgerOrder:
    """The reconcile-relevant columns of one Kiwoom mock order ledger row."""

    client_order_id: str
    status: str
    broker_order_id: str | None
    quantity: int
    symbol: str
    #: The ledger's already-recorded cumulative fill. A no-fill terminal verdict
    #: contradicts a positive value, so those rows go to manual review instead.
    recorded_filled_quantity: int = 0


@dataclass(frozen=True, slots=True)
class BrokerRow:
    """One broker order row flattened from kt00007 / kt00009."""

    order_id: str
    symbol: str
    source: str
    ordered_quantity: int | None
    filled_quantity: int | None
    remaining_quantity: int | None
    average_price: int | None
    marker: str | None  # "cancel" | "reject" | "expiry" | None


@dataclass(frozen=True, slots=True)
class BrokerEvidence:
    """A complete-or-not broker listing. ``complete`` is the absence guard."""

    complete: bool
    reason: str | None
    rows: tuple[BrokerRow, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    """The classification of one ledger row against broker evidence."""

    verdict: VerdictName
    reason: str
    #: ``True`` when a human must look: absence, ambiguity, or unparseable input.
    requires_manual_review: bool
    filled_quantity: int | None = None
    average_price: int | None = None
    #: Broker order id proven by the listing (only for ``uncertain → accepted``).
    broker_order_id: str | None = None


def return_code_ok(payload: Mapping[str, Any]) -> bool:
    """``return_code`` must be the broker success code.

    Mirrors ``app.mcp_server.tooling.orders_kiwoom_shared.derive_broker_success``
    without the upward import: bool (an int subclass) fails closed, and a
    missing / non-numeric code is not success.
    """
    value = payload.get("return_code")
    if type(value) is int:
        return value == 0
    return isinstance(value, str) and value == "0"


def _listing_truncated(payload: Mapping[str, Any]) -> bool:
    """A ``cont_yn`` other than an explicit "no continuation" is truncation.

    kt00009/kt00007 page with ``cont_yn``/``next_key`` headers. An unfinished
    page means the listing is partial, and a partial listing can never prove
    absence (§3.1).
    """
    continuation = payload.get("continuation")
    if not isinstance(continuation, Mapping):
        # No continuation envelope at all is not proof of a finished listing.
        return True
    cont_yn = str(continuation.get("cont_yn") or "").strip().upper()
    return cont_yn not in {"", "N"}


def _marker(text: Any) -> str | None:
    """The single positive marker in a broker change-type text, or ``None``.

    Zero matches and multi-way matches are both ``None``: an ambiguous text is
    not evidence (§3.2 positive evidence only).
    """
    lowered = str(text or "").strip().lower()
    if not lowered:
        return None
    matched = [name for name, tokens in _MARKERS if any(t in lowered for t in tokens)]
    return matched[0] if len(matched) == 1 else None


def _rows_from_detail(payload: Mapping[str, Any]) -> tuple[BrokerRow, ...]:
    """kt00007 ``acnt_ord_cntr_prps_dtl`` rows (raise = unparseable evidence)."""
    return tuple(
        BrokerRow(
            order_id=str(row["order_id"]),
            symbol=str(row["symbol"]),
            source=SOURCE_ORDER_DETAIL,
            ordered_quantity=int(row["ordered_quantity"]),
            filled_quantity=int(row["filled_quantity"]),
            remaining_quantity=int(row["remaining_quantity"]),
            average_price=int(row["average_price"]),
            marker=_marker(row.get("change_type")),
        )
        for row in normalize_order_detail(dict(payload))
    )


def _rows_from_status(payload: Mapping[str, Any]) -> tuple[BrokerRow, ...]:
    """kt00009 ``acnt_ord_cntr_prst_array`` rows.

    The shared normalizer keeps no ordered/filled quantity for kt00009, so this
    source contributes cancel confirmation only — fill evidence comes from
    kt00007 (§3.2).
    """
    rows: list[BrokerRow] = []
    for row in normalize_orders(dict(payload)):
        rows.append(
            BrokerRow(
                order_id=str(row["order_id"]),
                symbol=str(row["symbol"]),
                source=SOURCE_ORDER_STATUS,
                ordered_quantity=None,
                filled_quantity=None,
                remaining_quantity=None,
                average_price=None,
                marker="cancel" if row.get("status") == "cancelled" else None,
            )
        )
    return tuple(rows)


def evidence_from_payloads(
    *,
    order_status: Mapping[str, Any] | None,
    order_detail: Mapping[str, Any] | None,
    consult_order_status: bool = True,
) -> BrokerEvidence:
    """Build broker evidence from the two reads of design §3.1.

    ``None`` stands for a read that failed. Any failed, errored, truncated or
    unparseable read makes the whole evidence incomplete: absence is only
    meaningful when the consulted listings are complete.

    ``consult_order_status=False`` is for a past trading day, which kt00009 does
    not list: that source is then *not consulted* (no rows, no completeness
    requirement) rather than represented by an empty listing.
    """
    payloads = [(order_status, "order_status")] if consult_order_status else []
    payloads.append((order_detail, "order_detail"))
    for payload, written in payloads:
        if payload is None:
            return BrokerEvidence(
                complete=False, reason=f"{written}_unreadable", rows=()
            )
        if not return_code_ok(payload):
            return BrokerEvidence(
                complete=False, reason=f"{written}_broker_error", rows=()
            )
        if _listing_truncated(payload):
            return BrokerEvidence(
                complete=False,
                reason=f"{written}_listing_truncated",
                rows=(),
            )
    try:
        rows = _rows_from_detail(order_detail)
        if consult_order_status:
            rows = rows + _rows_from_status(order_status)
    except (KiwoomMockEvidenceError, KeyError, TypeError, ValueError):
        return BrokerEvidence(complete=False, reason="broker_rows_unparseable", rows=())
    return BrokerEvidence(complete=True, reason=None, rows=rows)


def _unknown(reason: str) -> Verdict:
    return Verdict(verdict="unknown", reason=reason, requires_manual_review=True)


def _exact_rows(
    ledger: LedgerOrder, evidence: BrokerEvidence
) -> tuple[BrokerRow | None, BrokerRow | None, str | None]:
    """Return ``(detail_row, status_row, ambiguity_reason)`` for the exact ord_no."""
    order_id = ledger.broker_order_id
    if order_id is None:
        return None, None, "no_broker_order_id"
    matched = [row for row in evidence.rows if row.order_id == order_id]
    detail = [row for row in matched if row.source == SOURCE_ORDER_DETAIL]
    status = [row for row in matched if row.source == SOURCE_ORDER_STATUS]
    if len(detail) > 1 or len(status) > 1:
        return None, None, "ambiguous_rows"
    if any(row.symbol != ledger.symbol for row in matched):
        # The order number is reused or misread: do not attribute another
        # symbol's order to this ledger row.
        return None, None, "attribute_mismatch"
    return (detail[0] if detail else None, status[0] if status else None, None)


def _classify_uncertain(
    *,
    ledger: LedgerOrder,
    detail_row: BrokerRow | None,
    status_row: BrokerRow | None,
) -> Verdict:
    """NH T9b: uncertainty is resolved only to ``accepted``, and only when the
    matched broker row shows the order did not end against that assumption.

    Promotable: a working row (no terminal marker, fill 0 with remaining > 0) or
    a row with a positive fill (a fill is proof the order was accepted; the next
    run classifies it filled/partial and T-P4 books it).

    Not promotable: a terminal marker (cancel/reject/expiry) — writing
    ``accepted`` would fabricate an acceptance and permanently strand the row,
    because a later ``cancelled`` verdict needs our own cancel request, which a
    broker-side cancel never has — and a no-fill, no-remaining row, whose ending
    reason is unknown.
    """
    if detail_row is None and status_row is None:
        return _unknown("no_exact_row")
    markers = {
        row.marker
        for row in (detail_row, status_row)
        if row is not None and row.marker is not None
    }
    terminal_no_fill = (
        detail_row is not None
        and detail_row.filled_quantity == 0
        and detail_row.remaining_quantity == 0
    )
    if markers or terminal_no_fill:
        return _unknown("uncertain_broker_terminal_evidence")
    return Verdict(
        verdict="accepted",
        reason="positive_listing_match",
        requires_manual_review=False,
        broker_order_id=ledger.broker_order_id,
    )


def classify_verdict(
    *,
    ledger: LedgerOrder,
    evidence: BrokerEvidence,
    own_cancel_acknowledged: bool,
) -> Verdict:
    """Classify one ledger row (design §3.2).

    ``own_cancel_acknowledged`` is ``True`` when the ledger holds our own
    accepted cancel order for this exact original order — the first half of the
    ``cancelled`` requirement.
    """
    if not evidence.complete:
        return _unknown(evidence.reason or "broker_listing_incomplete")

    detail_row, status_row, ambiguity = _exact_rows(ledger, evidence)
    if ambiguity is not None:
        return _unknown(ambiguity)

    if ledger.status == "uncertain":
        return _classify_uncertain(
            ledger=ledger, detail_row=detail_row, status_row=status_row
        )

    if detail_row is None:
        # kt00009 alone carries no quantities, so it cannot prove fills. It can
        # still complete the cancel pair when our own request is acknowledged.
        if (
            status_row is not None
            and status_row.marker == "cancel"
            and own_cancel_acknowledged
        ):
            if ledger.recorded_filled_quantity > 0:
                return _unknown("recorded_fill_contradicts_broker")
            return Verdict(
                verdict="cancelled",
                reason="own_cancel_acknowledged_and_broker_confirmed",
                requires_manual_review=False,
            )
        return _unknown(
            "no_detail_evidence" if status_row is not None else "no_exact_row"
        )

    filled = detail_row.filled_quantity
    ordered = detail_row.ordered_quantity or ledger.quantity
    if filled is None:
        return _unknown("filled_quantity_unreadable")

    if filled > 0:
        price = detail_row.average_price
        if price is None or price <= 0:
            return _unknown("fill_price_unparseable")
        verdict: VerdictName = "filled" if filled >= ordered else "partial"
        return Verdict(
            verdict=verdict,
            reason="broker_cumulative_fill",
            requires_manual_review=False,
            filled_quantity=filled,
            average_price=price,
        )

    remaining = detail_row.remaining_quantity
    if remaining is None:
        return _unknown("remaining_quantity_unreadable")
    if remaining > 0:
        # PENDING: accepted, still working. Not a close (§3.2).
        return Verdict(
            verdict="pending", reason="broker_pending", requires_manual_review=False
        )

    # Terminal with no fill. The broker marker, never the clock, says which.
    if ledger.recorded_filled_quantity > 0:
        # A booked fill cannot coexist with a broker no-fill close; closing would
        # strand the fill outside every T-P4 booking reader.
        return _unknown("recorded_fill_contradicts_broker")
    markers = {
        row.marker
        for row in (detail_row, status_row)
        if row is not None and row.marker is not None
    }
    if "cancel" in markers:
        if not own_cancel_acknowledged:
            # A cancel we cannot tie to our own acknowledged request is not
            # proven (design §3.2 requires both halves).
            return _unknown("cancel_without_own_acknowledged_request")
        return Verdict(
            verdict="cancelled",
            reason="own_cancel_acknowledged_and_broker_confirmed",
            requires_manual_review=False,
        )
    if "reject" in markers:
        return Verdict(
            verdict="rejected",
            reason="broker_reject_confirmed",
            requires_manual_review=False,
        )
    if "expiry" in markers:
        return Verdict(
            verdict="expired",
            reason="broker_expiry_confirmed",
            requires_manual_review=False,
        )
    return _unknown("terminal_without_reason_marker")
