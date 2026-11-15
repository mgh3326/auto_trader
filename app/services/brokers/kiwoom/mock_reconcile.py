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
    "ROLE_DETAIL",
    "ROLE_STATUS",
    "SOURCE_US_OPEN_ORDERS",
    "SOURCE_US_TODAY_ORDERS",
    "STATUS_LIST_KEY",
    "US_ORDER_LIST_KEY",
    "US_PAST_DAY_UNAVAILABLE",
    "Verdict",
    "classify_verdict",
    "evidence_from_payloads",
    "return_code_ok",
    "us_evidence_from_payloads",
    "us_past_day_evidence",
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
STATUS_LIST_KEY: Final = "acnt_ord_cntr_prst_array"

#: Roles the classifier partitions on (see :class:`BrokerRow`).
ROLE_DETAIL: Final = "detail"
ROLE_STATUS: Final = "status"

# US mock order listings (hk 4754). The observed row shape is the one the US
# smoke recorded (docs/runbooks/kiwoom-mock-us-smoke.md): ``result_list`` rows
# with ``ord_no``/``ord_stat``/``cntr_qty``/``ord_remnq``/``ord_cntr_tp``.
SOURCE_US_TODAY_ORDERS: Final = "ust21510"
SOURCE_US_OPEN_ORDERS: Final = "ust21050"
US_ORDER_LIST_KEY: Final = "result_list"

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
    """One broker order row flattened from an order listing.

    ``source`` names the TR the row came from (``kt00007``/``ust21510``, …) and
    is kept for evidence provenance. ``role`` is what the classifier partitions
    on: ``detail`` is the per-order row that carries fill and marker evidence,
    ``status`` is the account listing consulted alongside it.
    """

    order_id: str
    symbol: str
    source: str
    role: str
    ordered_quantity: int | None
    filled_quantity: int | None
    remaining_quantity: int | None
    average_price: int | None
    marker: str | None  # "cancel" | "reject" | "expiry" | None
    #: Raw change-type text, kept so an unrecognised/ambiguous text is never
    #: mistaken for "no change".
    change_text: str | None = None
    #: The change text matched more than one marker class (e.g. ``취소/만료``).
    marker_ambiguous: bool = False


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


def _matched_markers(text: Any) -> list[str]:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return []
    return [name for name, tokens in _MARKERS if any(t in lowered for t in tokens)]


def _marker(text: Any) -> str | None:
    """The single positive marker in a broker change-type text, or ``None``.

    Zero matches and multi-way matches are both ``None``: an ambiguous text is
    not evidence (§3.2 positive evidence only). Callers that need to tell the
    two apart use :func:`_matched_markers`.
    """
    matched = _matched_markers(text)
    return matched[0] if len(matched) == 1 else None


def _raw_int(row: Mapping[str, Any], key: str) -> int:
    """Parse a raw kt00009 integer with the normalizer's rules."""
    raw = row.get(key)
    text = str(raw).strip().replace(",", "") if raw is not None else ""
    try:
        value = int(text)
    except ValueError as exc:
        raise KiwoomMockEvidenceError(
            f"Kiwoom row field {key} is not an integer"
        ) from exc
    if value < 0:
        raise KiwoomMockEvidenceError(f"Kiwoom row field {key} is negative")
    return value


def _rows_from_detail(payload: Mapping[str, Any]) -> tuple[BrokerRow, ...]:
    """kt00007 ``acnt_ord_cntr_prps_dtl`` rows (raise = unparseable evidence)."""
    rows: list[BrokerRow] = []
    for row in normalize_order_detail(dict(payload)):
        text = str(row.get("change_type") or "").strip()
        matched = _matched_markers(text)
        rows.append(
            BrokerRow(
                order_id=str(row["order_id"]),
                symbol=str(row["symbol"]),
                source=SOURCE_ORDER_DETAIL,
                role=ROLE_DETAIL,
                ordered_quantity=int(row["ordered_quantity"]),
                filled_quantity=int(row["filled_quantity"]),
                remaining_quantity=int(row["remaining_quantity"]),
                average_price=int(row["average_price"]),
                marker=matched[0] if len(matched) == 1 else None,
                change_text=text or None,
                marker_ambiguous=len(matched) > 1,
            )
        )
    return tuple(rows)


def _rows_from_status(payload: Mapping[str, Any]) -> tuple[BrokerRow, ...]:
    """kt00009 ``acnt_ord_cntr_prst_array`` rows.

    The shared normalizer does not expose quantities or the raw change text, so
    both are carried from the paired raw row: a fill or a terminal marker in
    either listing is broker evidence and must not be lost.
    """
    normalized = normalize_orders(dict(payload))
    raw_rows = payload.get(STATUS_LIST_KEY)
    if not isinstance(raw_rows, list) or len(raw_rows) != len(normalized):
        raise KiwoomMockEvidenceError("kt00009 rows could not be paired with raw rows")
    rows: list[BrokerRow] = []
    for raw, row in zip(raw_rows, normalized, strict=True):
        if not isinstance(raw, Mapping):
            raise KiwoomMockEvidenceError("kt00009 raw row is not a mapping")
        text = str(raw.get("mdfy_cncl_tp") or "").strip()
        matched = _matched_markers(text)
        # ``normalize_orders`` maps only cancels; reject/expiry text is kept so
        # the closed marker vocabulary applies to both listings. An ambiguous
        # text never falls back to the normalizer's coarse "cancelled" status.
        if len(matched) == 1:
            marker = matched[0]
        elif matched:
            marker = None
        else:
            marker = "cancel" if row.get("status") == "cancelled" else None
        rows.append(
            BrokerRow(
                order_id=str(row["order_id"]),
                symbol=str(row["symbol"]),
                source=SOURCE_ORDER_STATUS,
                role=ROLE_STATUS,
                ordered_quantity=_raw_int(raw, "ord_qty"),
                # Carried, not discarded: a fill in either listing must block a
                # no-fill close (the fill may still need booking).
                filled_quantity=int(row["filled_quantity"]),
                remaining_quantity=int(row["remaining_quantity"]),
                average_price=None,
                marker=marker,
                change_text=text or None,
                marker_ambiguous=len(matched) > 1,
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


# ---------------------------------------------------------------------------
# US mock listings (hk 4754) — the same evidence model, US field names.
#
# Field shape is the one recorded by the US mock smoke
# (docs/runbooks/kiwoom-mock-us-smoke.md): ``result_list`` rows carrying
# ``ord_no``, ``ord_stat``, ``cntr_qty``, ``ord_remnq`` and ``ord_cntr_tp``
# (``"12"`` marks a cancel row). ``ust21510`` lists today's orders *and fills*,
# so it is the detail role; ``ust21050`` lists open orders and is the status
# role. Neither exposes a per-row average fill price, so ``average_price`` is
# always ``None`` and a fill verdict is only priced when the caller demands it.
# ---------------------------------------------------------------------------

#: ``ord_cntr_tp`` value of a cancel confirmation row.
US_CANCEL_ROW_CODE: Final = "12"
#: No US listing covers a past ET trading day: absence there proves nothing.
US_PAST_DAY_UNAVAILABLE: Final = "us_listing_unavailable_for_past_trading_day"


def _us_optional_int(row: Mapping[str, Any], key: str) -> int | None:
    """A US listing integer, or ``None`` when absent/unreadable — never a guess."""
    raw = row.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).replace(",", "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as exc:
        raise KiwoomMockEvidenceError(
            f"Kiwoom US row field {key} is not an integer"
        ) from exc
    if value < 0:
        raise KiwoomMockEvidenceError(f"Kiwoom US row field {key} is negative")
    return value


def _us_order_id(row: Mapping[str, Any]) -> str:
    """This row's own order number (``orig_ord_no`` names a *different* order)."""
    found = {
        str(value).strip()
        for value in (row.get("ord_no"), row.get("order_no"))
        if isinstance(value, str) and str(value).strip()
    }
    if len(found) != 1:
        raise KiwoomMockEvidenceError("Kiwoom US row has no single order number")
    return next(iter(found))


def _us_rows(
    payload: Mapping[str, Any], *, source: str, role: str
) -> tuple[BrokerRow, ...]:
    raw_rows = payload.get(US_ORDER_LIST_KEY)
    if not isinstance(raw_rows, list):
        raise KiwoomMockEvidenceError("Kiwoom US response has no result_list")
    rows: list[BrokerRow] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise KiwoomMockEvidenceError("Kiwoom US row is not a mapping")
        symbol = str(raw.get("stk_cd") or "").strip().upper()
        if not symbol:
            raise KiwoomMockEvidenceError("Kiwoom US row has no symbol")
        text = str(raw.get("ord_stat") or "").strip()
        matched = _matched_markers(text)
        marker = matched[0] if len(matched) == 1 else None
        if marker is None and str(raw.get("ord_cntr_tp") or "").strip() == (
            US_CANCEL_ROW_CODE
        ):
            # A cancel *confirmation row* is positive cancel evidence for the
            # original order it names; the closed marker vocabulary still owns
            # any text it did recognise.
            marker = "cancel"
        rows.append(
            BrokerRow(
                order_id=_us_order_id(raw),
                symbol=symbol,
                source=source,
                role=role,
                ordered_quantity=_us_optional_int(raw, "ord_qty"),
                filled_quantity=_us_optional_int(raw, "cntr_qty"),
                remaining_quantity=_us_optional_int(raw, "ord_remnq"),
                average_price=None,
                marker=marker,
                change_text=text or None,
                marker_ambiguous=len(matched) > 1,
            )
        )
    return tuple(rows)


def us_evidence_from_payloads(
    *, today_orders: Mapping[str, Any] | None, open_orders: Mapping[str, Any] | None
) -> BrokerEvidence:
    """Broker evidence from the two US listings (hk 4754).

    ``None`` stands for a failed read. A failed, errored, truncated or
    unparseable read makes the whole evidence incomplete: absence is only
    meaningful when every consulted listing is complete (I5).
    """
    for payload, written in (
        (today_orders, "us_today_orders"),
        (open_orders, "us_open_orders"),
    ):
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
                complete=False, reason=f"{written}_listing_truncated", rows=()
            )
    try:
        rows = _us_rows(today_orders, source=SOURCE_US_TODAY_ORDERS, role=ROLE_DETAIL)
        rows = rows + _us_rows(
            open_orders, source=SOURCE_US_OPEN_ORDERS, role=ROLE_STATUS
        )
    except (KiwoomMockEvidenceError, KeyError, TypeError, ValueError):
        return BrokerEvidence(complete=False, reason="broker_rows_unparseable", rows=())
    return BrokerEvidence(complete=True, reason=None, rows=rows)


def us_past_day_evidence() -> BrokerEvidence:
    """No US listing covers a past ET trading day — and absence is not evidence."""
    return BrokerEvidence(complete=False, reason=US_PAST_DAY_UNAVAILABLE, rows=())


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
    detail = [row for row in matched if row.role == ROLE_DETAIL]
    status = [row for row in matched if row.role == ROLE_STATUS]
    if len(detail) > 1 or len(status) > 1:
        return None, None, "ambiguous_rows"
    if any(row.symbol != ledger.symbol for row in matched):
        # The order number is reused or misread: do not attribute another
        # symbol's order to this ledger row.
        return None, None, "attribute_mismatch"
    if any(
        row.ordered_quantity is not None and row.ordered_quantity != ledger.quantity
        for row in matched
    ):
        # Same number and symbol but a different order size is a different
        # order (number reuse); never close/promote the ledger row with it.
        return None, None, "quantity_mismatch"
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
    rows = [row for row in (detail_row, status_row) if row is not None]
    if not rows:
        return _unknown("no_exact_row")
    if any(row.marker is not None for row in rows):
        return _unknown("uncertain_broker_terminal_evidence")
    if any(row.marker_ambiguous for row in rows):
        # ``취소/만료`` and friends are not proof the order is working.
        return _unknown("uncertain_change_type_ambiguous")
    if any(row.change_text for row in rows):
        # An unclassifiable change text is likewise not working evidence.
        return _unknown("uncertain_change_type_unclear")
    if detail_row is None:
        # No quantity evidence: nothing positively proves the order is working.
        return _unknown("no_detail_evidence")
    filled = detail_row.filled_quantity
    if filled is None:
        return _unknown("filled_quantity_unreadable")
    if filled == 0 and (detail_row.remaining_quantity or 0) <= 0:
        return _unknown("uncertain_broker_terminal_evidence")
    # A fill proves acceptance (the next run classifies filled/partial); a
    # positive remaining quantity proves the order is still working.
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
    require_fill_price: bool = True,
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
        if status_row is None:
            return _unknown("no_exact_row")
        if status_row.marker_ambiguous:
            # Ambiguous terminal text must never be collapsed into a marker.
            return _unknown("ambiguous_marker_text")
        # kt00009 cannot price a fill, but a fill it reports is still broker
        # evidence: closing now would strand it outside every T-P4 booking read.
        if (status_row.filled_quantity or 0) > 0:
            return _unknown("status_listing_reports_fill")
        if status_row.marker == "cancel":
            # The cancel pair needs our own acknowledged request (design §3.2).
            if not own_cancel_acknowledged:
                return _unknown("cancel_without_own_acknowledged_request")
            if ledger.recorded_filled_quantity > 0:
                return _unknown("recorded_fill_contradicts_broker")
            return Verdict(
                verdict="cancelled",
                reason="own_cancel_acknowledged_and_broker_confirmed",
                requires_manual_review=False,
            )
        if status_row.marker in {"reject", "expiry"}:
            if ledger.recorded_filled_quantity > 0:
                return _unknown("recorded_fill_contradicts_broker")
            return Verdict(
                verdict=("rejected" if status_row.marker == "reject" else "expired"),
                reason=(
                    "broker_reject_confirmed"
                    if status_row.marker == "reject"
                    else "broker_expiry_confirmed"
                ),
                requires_manual_review=False,
            )
        return _unknown("no_detail_evidence")

    filled = detail_row.filled_quantity
    ordered = detail_row.ordered_quantity or ledger.quantity
    if filled is None:
        return _unknown("filled_quantity_unreadable")
    if (
        status_row is not None
        and status_row.filled_quantity is not None
        and status_row.filled_quantity != filled
    ):
        # The listings disagree about the fill; never book or close on a guess.
        return _unknown("fill_evidence_conflict")

    if filled > 0:
        price = detail_row.average_price
        if (price is None or price <= 0) and require_fill_price:
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
    if any(row.marker_ambiguous for row in (detail_row, status_row) if row is not None):
        return _unknown("ambiguous_marker_text")
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
