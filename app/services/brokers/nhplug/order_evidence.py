"""Pure, fail-closed interpretation of NHPLUG mock order responses.

No I/O, no clock, no database.  The rules here encode one lesson above all:
**an empty or error-shaped order listing is not evidence of "no open orders"**
(the Kiwoom ``kt00009`` incident).  The vendor documents that response blocks
are omitted when there is no data, and the same ``rsp_cd`` can mean different
things per API, so:

* a page is *usable* only when its envelope is well-formed, its business code
  is a known read code, every row parses, and pagination terminates;
* a listing is *complete* only when every page is usable and no continuation
  key remains unfollowed;
* there is no "no open orders" answer at all: without a positively listed
  open row the result is ``unknown`` (absence is never evidence);
* terminal order states need two positive sources (see ``reconcile_plan``).

Broker business messages are redacted of long digit runs before they are
surfaced; customer-name and account fields are never copied out of a row.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from uuid import UUID

ROW_BLOCK_KEY: Final[str] = "Output_1"

# Read codes observed for this vendor.  A code outside this set, or no code
# at all, makes the page unusable rather than "empty".
READ_OK_CODES: Final[frozenset[str]] = frozenset({"00000", "00166", "00221"})
READ_CONTINUE_CODES: Final[frozenset[str]] = frozenset({"00165", "00218"})
READ_NO_ROWS_CODE: Final[str] = "13578"

_BODY_CONTINUATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^cts(?:z\d+)?$", re.IGNORECASE
)
_LONG_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\d{6,}")
_MAX_MESSAGE_LENGTH: Final[int] = 160

# There is deliberately no "none" state: no listing shape — empty, block
# absent, 13578, or error — is accepted as evidence that nothing is open.
OpenOrdersState = Literal["present", "unknown"]
EMPTY_IS_NOT_EVIDENCE: Final[str] = (
    "empty_open_listing_is_not_evidence_of_no_open_orders"
)
OrderStatus = Literal[
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "modified",
    "rejected",
    "unknown",
]
TERMINAL_ORDER_STATUSES: Final[frozenset[str]] = frozenset(
    {"filled", "cancelled", "modified", "rejected"}
)


def strict_int(value: object) -> int | None:
    """Parse an exact non-negative integer; bools, floats, and junk are None."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if text.isdigit():
            return int(text)
    return None


def strict_decimal(value: object) -> Decimal | None:
    """Exact decimal parsing for broker numerics; floats go through ``repr``."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, str) and value.strip():
        try:
            parsed = Decimal(value.strip().replace(",", ""))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def redact_message(value: object) -> str | None:
    """Keep a short broker message for operators, masking long digit runs."""

    if not isinstance(value, str) or not value.strip():
        return None
    masked = _LONG_DIGITS_RE.sub("[redacted]", value.strip())
    return masked[:_MAX_MESSAGE_LENGTH]


def _side_from_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    has_buy = "매수" in value
    has_sell = "매도" in value
    if has_buy == has_sell:
        return None
    return "buy" if has_buy else "sell"


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class OrderRow:
    """One normalized listing row; customer and account fields are dropped."""

    order_no: int
    symbol: str
    order_qty: int
    filled_qty: int
    open_qty: int
    side: str | None = None
    original_order_no: int | None = None
    order_price: Decimal | None = None
    avg_fill_price: Decimal | None = None
    cancelled_qty: int | None = None
    modified_qty: int | None = None
    correction_kind: str | None = None
    rejection_reason: str | None = None
    order_time: str | None = None

    def evidence(self) -> dict[str, Any]:
        """JSON-safe evidence summary for the ledger (decimals as strings)."""

        return {
            "order_no": str(self.order_no),
            "original_order_no": None
            if self.original_order_no is None
            else str(self.original_order_no),
            "symbol": self.symbol,
            "side": self.side,
            "order_qty": self.order_qty,
            "order_price": None if self.order_price is None else str(self.order_price),
            "filled_qty": self.filled_qty,
            "avg_fill_price": None
            if self.avg_fill_price is None
            else str(self.avg_fill_price),
            "open_qty": self.open_qty,
            "cancelled_qty": self.cancelled_qty,
            "modified_qty": self.modified_qty,
            "correction_kind": self.correction_kind,
            "rejection_reason": redact_message(self.rejection_reason),
            "order_time": self.order_time,
        }


def parse_order_row(row: object) -> OrderRow | None:
    """Parse one listing row, or None when any required field is unusable."""

    if not isinstance(row, Mapping):
        return None
    order_no = strict_int(row.get("itg_orr_no"))
    symbol = _text(row.get("iem_cd"))
    order_qty = strict_int(row.get("orr_qty"))
    filled_qty = strict_int(row.get("tot_cns_qty"))
    open_qty = strict_int(row.get("ny_cns_qty"))
    if (
        order_no is None
        or order_no <= 0
        or symbol is None
        or order_qty is None
        or filled_qty is None
        or open_qty is None
    ):
        return None
    original = strict_int(row.get("org_itg_orr_no"))
    return OrderRow(
        order_no=order_no,
        symbol=symbol,
        order_qty=order_qty,
        filled_qty=filled_qty,
        open_qty=open_qty,
        side=_side_from_name(row.get("sby_dit_cd_nm")),
        original_order_no=original if original else None,
        order_price=strict_decimal(row.get("orr_pr")),
        avg_fill_price=strict_decimal(row.get("cns_avg_uit_pr")),
        cancelled_qty=strict_int(row.get("can_qty")),
        modified_qty=strict_int(row.get("cor_qty")),
        correction_kind=_text(row.get("cor_can_dit_cd_nm")),
        rejection_reason=_text(row.get("orr_rjt_rsn_cd_nm")),
        order_time=_text(row.get("orr_tm")),
    )


@dataclass(frozen=True, slots=True)
class ListingPage:
    """Classification of one listing page."""

    usable: bool
    rows: tuple[OrderRow, ...] = ()
    reason: str | None = None
    response_code: str | None = None
    continuation_key: str | None = field(default=None, repr=False)
    has_next: bool = False


def _body_continuation_key(payload: Mapping[str, Any]) -> str | None:
    found: str | None = None
    for key, block in payload.items():
        if not isinstance(key, str) or not key.startswith("Output"):
            continue
        for row in block if isinstance(block, list) else [block]:
            if not isinstance(row, Mapping):
                continue
            for field_name, value in row.items():
                if (
                    isinstance(field_name, str)
                    and _BODY_CONTINUATION_RE.match(field_name)
                    and isinstance(value, str)
                    and value.strip()
                ):
                    found = value.strip()
    return found


def classify_listing_page(
    payload: object,
    *,
    header_continuation_key: str | None = None,
    header_continuation_flag: str | None = None,
) -> ListingPage:
    """Classify one page; unknown shapes are unusable, never "empty"."""

    if not isinstance(payload, Mapping):
        return ListingPage(usable=False, reason="response_not_object")
    if "error_code" in payload or "error_description" in payload:
        return ListingPage(usable=False, reason="gateway_error_envelope")
    raw_code = payload.get("rsp_cd")
    code = str(raw_code) if isinstance(raw_code, str | int) else None
    if code is None:
        return ListingPage(usable=False, reason="missing_response_code")
    if code not in READ_OK_CODES | READ_CONTINUE_CODES | {READ_NO_ROWS_CODE}:
        return ListingPage(
            usable=False, reason="unrecognized_response_code", response_code=code
        )

    block = payload.get(ROW_BLOCK_KEY)
    if block is None:
        raw_rows: list[Any] = []
    elif isinstance(block, list):
        raw_rows = block
    else:
        return ListingPage(
            usable=False, reason="row_block_not_a_list", response_code=code
        )
    if code == READ_NO_ROWS_CODE and raw_rows:
        return ListingPage(
            usable=False, reason="no_rows_code_with_rows", response_code=code
        )

    rows: list[OrderRow] = []
    for raw in raw_rows:
        parsed = parse_order_row(raw)
        if parsed is None:
            return ListingPage(
                usable=False, reason="malformed_order_row", response_code=code
            )
        rows.append(parsed)

    key = header_continuation_key or _body_continuation_key(payload)
    flag = (header_continuation_flag or "").strip().upper()
    # A page is final only when *no* continuation signal is present: no key
    # (header or body), no continue code, and no "Y" flag.  Any signal —
    # including one contradicted by cts_flag=N — means "more pages"; a key is
    # followed, and a signal without a followable key leaves the listing
    # incomplete (unknown), never final.
    has_next = key is not None or code in READ_CONTINUE_CODES or flag == "Y"
    return ListingPage(
        usable=True,
        rows=tuple(rows),
        response_code=code,
        continuation_key=key if has_next else None,
        has_next=has_next,
    )


@dataclass(frozen=True, slots=True)
class OrderListing:
    """A fully paginated listing for one scope, or an explicit incomplete one."""

    scope: str
    complete: bool
    rows: tuple[OrderRow, ...] = ()
    reason: str | None = None
    pages: int = 0
    response_codes: tuple[str | None, ...] = ()
    account_ref: UUID | None = None
    order_date: date | None = None

    def find(self, order_no: int) -> OrderRow | None:
        for row in self.rows:
            if row.order_no == order_no:
                return row
        return None


def assemble_listing(
    scope: str, pages: Sequence[ListingPage], *, truncated: bool = False
) -> OrderListing:
    """Combine pages; any unusable page, loop, or truncation is incomplete."""

    if not pages:
        return OrderListing(scope=scope, complete=False, reason="no_pages")
    codes = tuple(page.response_code for page in pages)
    rows: list[OrderRow] = []
    seen_keys: set[str] = set()
    for page in pages:
        if not page.usable:
            return OrderListing(
                scope=scope,
                complete=False,
                reason=page.reason or "unusable_page",
                pages=len(pages),
                response_codes=codes,
            )
        rows.extend(page.rows)
        if page.continuation_key is not None:
            if page.continuation_key in seen_keys:
                return OrderListing(
                    scope=scope,
                    complete=False,
                    reason="continuation_key_repeated",
                    pages=len(pages),
                    response_codes=codes,
                )
            seen_keys.add(page.continuation_key)
    if truncated or pages[-1].has_next:
        return OrderListing(
            scope=scope,
            complete=False,
            reason="pagination_truncated",
            pages=len(pages),
            response_codes=codes,
        )
    unique: dict[int, OrderRow] = {}
    for row in rows:
        previous = unique.get(row.order_no)
        if previous is not None and previous != row:
            return OrderListing(
                scope=scope,
                complete=False,
                reason="conflicting_duplicate_order_rows",
                pages=len(pages),
                response_codes=codes,
            )
        unique[row.order_no] = row
    return OrderListing(
        scope=scope,
        complete=True,
        rows=tuple(unique.values()),
        pages=len(pages),
        response_codes=codes,
    )


@dataclass(frozen=True, slots=True)
class OpenOrdersDetermination:
    state: OpenOrdersState
    open_rows: tuple[OrderRow, ...]
    reasons: tuple[str, ...]


def determine_open_orders(
    *,
    all_listing: OrderListing,
    open_listing: OrderListing,
    ledger_live_order_nos: Iterable[int] = (),
    ledger_has_unbound_uncertain: bool = False,
) -> OpenOrdersDetermination:
    """Positive evidence only: ``present`` or ``unknown``, never "none".

    An open order is ``present`` when either complete scope positively lists
    it with an open quantity.  When no open row is found, the answer is
    ``unknown`` with ``EMPTY_IS_NOT_EVIDENCE`` (plus any source problems):
    the Kiwoom kt00009 lesson is that an empty open-orders response can be
    wrong, and no second listing of the same API can prove it right.
    """

    reasons: list[str] = []
    open_rows: dict[int, OrderRow] = {}
    if all_listing.complete:
        for row in all_listing.rows:
            if row.open_qty > 0:
                open_rows[row.order_no] = row
    else:
        reasons.append(f"all_scope_incomplete:{all_listing.reason}")
    if open_listing.complete:
        for row in open_listing.rows:
            if row.open_qty <= 0:
                # Contradictory, not positive, evidence: never "present".
                reasons.append("open_scope_row_without_open_quantity")
                continue
            open_rows.setdefault(row.order_no, row)
    else:
        reasons.append(f"open_scope_incomplete:{open_listing.reason}")

    if all_listing.complete and open_listing.complete:
        all_open = {r.order_no for r in all_listing.rows if r.open_qty > 0}
        scoped_open = {r.order_no for r in open_listing.rows}
        if all_open != scoped_open:
            reasons.append("open_order_sources_disagree")
        listed = {r.order_no for r in all_listing.rows}
        missing = sorted(set(ledger_live_order_nos) - listed)
        if missing:
            reasons.append("ledger_live_order_missing_from_listing")
    if ledger_has_unbound_uncertain:
        reasons.append("ledger_has_order_with_unknown_broker_number")

    rows = tuple(sorted(open_rows.values(), key=lambda r: r.order_no))
    if rows:
        # Positive evidence of an open order is trustworthy from either source.
        return OpenOrdersDetermination(
            state="present", open_rows=rows, reasons=tuple(reasons)
        )
    reasons.append(EMPTY_IS_NOT_EVIDENCE)
    return OpenOrdersDetermination(
        state="unknown", open_rows=(), reasons=tuple(reasons)
    )


def derive_order_status(row: OrderRow) -> OrderStatus:
    """Map broker quantities to a status; inconsistent arithmetic is unknown."""

    if row.order_qty <= 0:
        return "unknown"
    cancelled = row.cancelled_qty or 0
    modified = row.modified_qty or 0
    if (
        row.rejection_reason
        and row.filled_qty == 0
        and row.open_qty == 0
        and cancelled == 0
        and modified == 0
    ):
        return "rejected"
    # Every quantity must be accounted for exactly once.
    if row.filled_qty + row.open_qty + cancelled + modified != row.order_qty:
        return "unknown"
    if row.open_qty > 0:
        return "partially_filled" if row.filled_qty > 0 else "open"
    if row.filled_qty == row.order_qty:
        return "filled"
    if modified > 0:
        return "modified"
    if cancelled > 0:
        return "cancelled"
    return "unknown"
