"""NHPLUG mock order operations shared by the MCP tools and the smoke CLI.

Scope (operator decision 2026-09-25, Stage 2): the broker-verified NH namuh
**mock** account only — balance/position, open-order, and fill reads, KRX
**limit** order placement, modification, and cancellation, plus ledger and
reconcile.  Market orders, live accounts, schedulers, and account assignment
are out of scope.

Double gate (hard rule 4): ``NHPLUG_MOCK_ENABLED`` is checked by the client at
every dispatch, and every mutation needs ``dry_run=False`` **and**
``confirm=True`` here *and* at the client dispatcher.  ``dry_run=True`` is
fully offline.

Evidence-first (hard rule 5): the ledger row is committed in ``submitting``
before the broker leg (no ledger, no send), acceptance requires a readable
broker order number, and fills/terminal states come only from reconcile.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo

from app.services.brokers.nhplug.client import (
    MAX_ORDER_NUMBER,
    MAX_ORDER_PRICE_KRW,
    MAX_ORDER_QUANTITY,
    NHPlugMockClient,
    TokenProvider,
)
from app.services.brokers.nhplug.contracts import (
    CommittedOrderIntent,
    DryRunConfirmContract,
)
from app.services.brokers.nhplug.errors import NHPlugMockDispatchUncertain
from app.services.brokers.nhplug.order_evidence import (
    ListingPage,
    OrderListing,
    assemble_listing,
    classify_listing_page,
    classify_order_ack,
    determine_open_orders,
    strict_decimal,
    strict_int,
)
from app.services.nhplug_mock.ledger_service import (
    LIVE_ORDER_STATUSES,
    UNBOUND_UNCERTAIN_STATUSES,
    NHPlugMockLedgerService,
)
from app.services.nhplug_mock.reconcile_plan import LedgerRowView, plan_reconcile

SOURCE: Final[str] = "nhplug_mock"
ACCOUNT_MODE: Final[str] = "nhplug_mock"
VENUE: Final[str] = "KRX"
LIMIT_ORDER_TYPE: Final[str] = "limit"
MAX_LISTING_PAGES: Final[int] = 20
KST: Final[ZoneInfo] = ZoneInfo("Asia/Seoul")

ClientFactory = Callable[[], Awaitable[NHPlugMockClient]]


@dataclass(frozen=True, slots=True)
class NHPlugMockCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)


def today_order_date(now: datetime | None = None) -> str:
    moment = now.astimezone(KST) if now is not None else datetime.now(KST)
    return moment.strftime("%Y%m%d")


async def open_verified_client(
    credentials: NHPlugMockCredentials,
    *,
    token_provider: TokenProvider,
    transport: Any = None,
) -> NHPlugMockClient:
    """Build a client and bind a freshly broker-verified ``acct_type=03`` allowlist."""

    client = NHPlugMockClient(
        app_key=credentials.app_key,
        app_secret=credentials.app_secret,
        token_provider=token_provider,
        transport=transport,
    )
    await client.verify_and_bind_mock_account(credentials.account_no)
    return client


def _base(**extra: Any) -> dict[str, Any]:
    return {"source": SOURCE, "account_mode": ACCOUNT_MODE, "venue": VENUE, **extra}


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return _base(success=False, error_code=code, error=message, **extra)


def _positive_int(value: object, ceiling: int) -> int | None:
    return value if type(value) is int and 0 < value <= ceiling else None


def validate_order_type(order_type: object, price: object) -> dict[str, Any] | None:
    """Limit-only scope: market or any non-limit type is refused clearly."""

    normalized = str(order_type).strip().lower() if order_type is not None else ""
    if normalized != LIMIT_ORDER_TYPE:
        return _error(
            "limit_orders_only",
            "NHPLUG mock Stage 2 accepts limit orders only; market and other "
            f"order types are refused (got order_type={order_type!r}).",
            dispatch_started=False,
        )
    if price is None:
        return _error(
            "limit_price_required",
            "A limit price is required; NHPLUG mock never sends a market order.",
            dispatch_started=False,
        )
    return None


def validate_place_request(
    *, side: object, symbol: object, quantity: object, price: object, order_type: object
) -> dict[str, Any] | None:
    if (refusal := validate_order_type(order_type, price)) is not None:
        return refusal
    if side not in {"buy", "sell"}:
        return _error("invalid_side", "side must be 'buy' or 'sell'.")
    if not isinstance(symbol, str) or not (len(symbol) == 6 and symbol.isdigit()):
        return _error("invalid_symbol", "symbol must be an exact six-digit KRX code.")
    if _positive_int(quantity, MAX_ORDER_QUANTITY) is None:
        return _error("invalid_quantity", "quantity must be a positive integer.")
    if _positive_int(price, MAX_ORDER_PRICE_KRW) is None:
        return _error("invalid_price", "price must be a positive integer KRW.")
    return None


def _order_no(order_id: object) -> int | None:
    if isinstance(order_id, int) and not isinstance(order_id, bool):
        return order_id if 0 < order_id <= MAX_ORDER_NUMBER else None
    if isinstance(order_id, str) and order_id.strip().isdigit():
        number = int(order_id.strip())
        return number if 0 < number <= MAX_ORDER_NUMBER else None
    return None


def _confirm_refusal(dry_run: object, confirm: object) -> dict[str, Any] | None:
    if dry_run is False and confirm is not True:
        return _error(
            "confirm_required",
            "NHPLUG mock order mutations require confirm=True when dry_run=False.",
            dispatch_started=False,
        )
    return None


async def collect_listing(
    client: NHPlugMockClient,
    *,
    order_date: str,
    scope: str,
    max_pages: int = MAX_LISTING_PAGES,
) -> OrderListing:
    """Fully paginate one listing scope; any failure yields an incomplete listing."""

    pages: list[ListingPage] = []
    key: str | None = None
    seen: set[str] = set()
    try:
        for _ in range(max_pages):
            result = await client.fetch_order_listing_page(
                order_date=order_date, scope=scope, continuation_key=key
            )
            page = classify_listing_page(
                result.payload,
                header_continuation_key=result.continuation_key,
                header_continuation_flag=result.continuation_flag,
            )
            pages.append(page)
            if not page.usable or not page.has_next or page.continuation_key is None:
                break
            if page.continuation_key in seen:
                break
            seen.add(page.continuation_key)
            key = page.continuation_key
    except Exception as exc:  # noqa: BLE001 - any read failure is "unknown"
        return OrderListing(
            scope=scope,
            complete=False,
            reason=f"request_failed:{type(exc).__name__}",
            pages=len(pages),
        )
    return assemble_listing(scope, pages)


def _listing_summary(listing: OrderListing) -> dict[str, Any]:
    return {
        "scope": listing.scope,
        "complete": listing.complete,
        "reason": listing.reason,
        "pages": listing.pages,
        "response_codes": list(listing.response_codes),
        "row_count": len(listing.rows),
    }


def _ledger_views(rows: Any) -> list[LedgerRowView]:
    return [
        LedgerRowView(
            id=row.id,
            operation_kind=row.operation_kind,
            status=row.status,
            symbol=row.symbol,
            side=row.side,
            quantity=None if row.quantity is None else int(row.quantity),
            price=None if row.price is None else Decimal(row.price),
            broker_order_id=row.broker_order_id,
            original_order_id=row.original_order_id,
            ack_order_id=row.ack_order_id,
        )
        for row in rows
    ]


async def get_open_orders(
    client: NHPlugMockClient,
    *,
    order_date: str,
    ledger: NHPlugMockLedgerService | None = None,
) -> dict[str, Any]:
    """Two-scope open-order read: ``present`` from positive rows, else ``unknown``."""

    all_listing = await collect_listing(client, order_date=order_date, scope="all")
    open_listing = await collect_listing(client, order_date=order_date, scope="open")
    live_numbers: list[int] = []
    unbound = False
    ledger_checked = False
    if ledger is not None:
        rows = await ledger.list_for_date(order_date)
        ledger_checked = True
        for row in rows:
            if row.operation_kind == "cancel":
                continue
            if row.status in LIVE_ORDER_STATUSES and row.broker_order_id:
                number = _order_no(row.broker_order_id)
                if number is not None:
                    live_numbers.append(number)
            elif row.status in UNBOUND_UNCERTAIN_STATUSES:
                unbound = True
    determination = determine_open_orders(
        all_listing=all_listing,
        open_listing=open_listing,
        ledger_live_order_nos=live_numbers,
        ledger_has_unbound_uncertain=unbound,
    )
    return _base(
        success=determination.state != "unknown",
        order_date=order_date,
        open_orders_state=determination.state,
        open_orders=[row.evidence() for row in determination.open_rows],
        reasons=list(determination.reasons),
        ledger_cross_checked=ledger_checked,
        sources=[_listing_summary(all_listing), _listing_summary(open_listing)],
        note=(
            "There is no 'none' state: without a positively listed open row "
            "the answer is unknown, because an empty open-orders response is "
            "not evidence that nothing is open."
        ),
    )


async def get_order_history(
    client: NHPlugMockClient, *, order_date: str, scope: str = "all"
) -> dict[str, Any]:
    if scope not in {"all", "filled", "open"}:
        return _error("invalid_scope", "scope must be all, filled, or open.")
    listing = await collect_listing(client, order_date=order_date, scope=scope)
    return _base(
        success=listing.complete,
        order_date=order_date,
        complete=listing.complete,
        orders=[row.evidence() for row in listing.rows] if listing.complete else [],
        source=_listing_summary(listing),
        **(
            {}
            if listing.complete
            else {"error_code": "listing_incomplete", "error": listing.reason}
        ),
    )


async def get_positions(client: NHPlugMockClient) -> dict[str, Any]:
    """Balance and positions; an error-shaped page is never "no positions"."""

    positions: list[dict[str, Any]] = []
    cash: dict[str, Any] | None = None
    key: str | None = None
    seen: set[str] = set()
    try:
        for page_index in range(MAX_LISTING_PAGES):
            result = await client.fetch_balance_page(continuation_key=key)
            payload = result.payload
            code = payload.get("rsp_cd")
            if not isinstance(code, str) or code not in {
                "00000",
                "00165",
                "00166",
                "00218",
                "00221",
                "13578",
            }:
                return _error(
                    "balance_unreadable",
                    "balance response code is not a recognized read code",
                    positions_state="unknown",
                    response_code=str(code) if code is not None else None,
                )
            summary = payload.get("Output_0")
            if page_index == 0 and isinstance(summary, dict):
                cash = {
                    "deposit_krw": strict_int(summary.get("dca")),
                    "orderable_krw": strict_int(summary.get("orr_pbl_amt")),
                    "d2_deposit_krw": strict_int(summary.get("nxt2_dd_dca")),
                }
            rows = payload.get("Output_1", [])
            if not isinstance(rows, list):
                return _error(
                    "balance_unreadable",
                    "balance rows are not a list",
                    positions_state="unknown",
                )
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("iem_cd"), str):
                    return _error(
                        "balance_unreadable",
                        "balance row is malformed",
                        positions_state="unknown",
                    )
                quantity = strict_decimal(row.get("itg_bnc_qty"))
                if quantity is None:
                    return _error(
                        "balance_unreadable",
                        "balance row has no readable quantity",
                        positions_state="unknown",
                    )
                positions.append(
                    {
                        "symbol": row["iem_cd"].strip(),
                        "quantity": str(quantity),
                        "avg_price": str(strict_decimal(row.get("phs_pr"))),
                        "current_price": str(strict_decimal(row.get("now_pr"))),
                        "evaluation_krw": strict_int(row.get("eal_amt")),
                    }
                )
            flag = (result.continuation_flag or "").upper()
            if (
                not result.continuation_key
                or flag == "N"
                or (flag != "Y" and code not in {"00165", "00218"})
            ):
                break
            if result.continuation_key in seen:
                return _error(
                    "balance_unreadable",
                    "balance pagination repeated a continuation key",
                    positions_state="unknown",
                )
            seen.add(result.continuation_key)
            key = result.continuation_key
        else:
            return _error(
                "balance_unreadable",
                "balance pagination was truncated",
                positions_state="unknown",
            )
    except Exception as exc:  # noqa: BLE001 - a failed read is unknown, not empty
        return _error(
            "balance_unreadable",
            f"balance read failed: {type(exc).__name__}",
            positions_state="unknown",
        )
    return _base(
        success=True,
        positions_state="present" if positions else "none_reported",
        positions=positions,
        cash=cash,
    )


def preview_place(
    *, side: str, symbol: str, quantity: int, price: int
) -> dict[str, Any]:
    return _base(
        success=True,
        dry_run=True,
        dispatch_started=False,
        operation="place",
        order_type=LIMIT_ORDER_TYPE,
        side=side,
        symbol=symbol,
        quantity=quantity,
        price=price,
        would_send={
            "price_type": "01 (limit)",
            "order_condition": "00 (none)",
            "market": VENUE,
            "sor_split": "N",
        },
        note="dry_run=True performs no network call and writes no ledger row.",
    )


async def _dispatch_and_record(
    *,
    ledger: NHPlugMockLedgerService,
    row_id: int,
    send: Callable[[], Awaitable[dict[str, Any]]],
    response: dict[str, Any],
) -> dict[str, Any]:
    """Run one confirmed broker leg and record its evidence-first outcome."""

    try:
        payload = await send()
    except NHPlugMockDispatchUncertain:
        try:
            await ledger.record_dispatch_uncertain(row_id)
            ledger_written = True
        except Exception:  # noqa: BLE001 - the broker leg outcome must surface
            ledger_written = False
        return {
            **response,
            "success": False,
            "status": "acceptance_uncertain",
            "dispatch_started": True,
            "reconcile_required": True,
            "retry_allowed": False,
            "ledger_written": ledger_written,
            "error_code": "dispatch_outcome_unknown",
            "error": "the order may have reached the broker; reconcile before any retry",
        }
    except Exception as exc:  # noqa: BLE001 - every other failure is pre-send
        refusal = type(exc).__name__
        try:
            await ledger.record_not_submitted(row_id, refusal=refusal)
            ledger_written = True
        except Exception:  # noqa: BLE001
            ledger_written = False
        return {
            **response,
            "success": False,
            "status": "not_submitted",
            "dispatch_started": False,
            "reconcile_required": False,
            "ledger_written": ledger_written,
            "error_code": "refused_before_dispatch",
            "error": refusal,
        }

    ack = classify_order_ack(payload)
    try:
        await ledger.record_ack(row_id, ack)
        ledger_written = True
    except Exception:  # noqa: BLE001 - never lose a broker acknowledgement
        ledger_written = False
    return {
        **response,
        "success": ack.state == "accepted",
        "status": ack.state,
        "dispatch_started": True,
        "broker_order_id": ack.broker_order_id,
        "broker_response_code": ack.response_code,
        "broker_message": ack.response_message,
        "reconcile_required": True,
        # Never auto-retry: a rejection without an order number is judged by
        # code only, and reconcile may still find the order at the broker.
        "retry_allowed": False,
        "ledger_written": ledger_written,
    }


async def _record_submitting(
    ledger: NHPlugMockLedgerService, **kwargs: Any
) -> tuple[int | None, CommittedOrderIntent | None, dict[str, Any] | None]:
    try:
        row = await ledger.record_submitting(**kwargs)
        intent = await ledger.committed_intent(row.id)
    except Exception as exc:  # noqa: BLE001 - no ledger row, no send
        return (
            None,
            None,
            _error(
                "ledger_unavailable",
                f"ledger write failed before dispatch ({type(exc).__name__}); "
                "the order was not sent.",
                status="not_submitted",
                dispatch_started=False,
            ),
        )
    return row.id, intent, None


async def place_limit_order(
    *,
    client_factory: ClientFactory,
    ledger: NHPlugMockLedgerService,
    side: str,
    symbol: str,
    quantity: int,
    price: int | None,
    order_type: str = LIMIT_ORDER_TYPE,
    dry_run: bool = True,
    confirm: bool = False,
    strategy: str | None = None,
    reason: str | None = None,
    correlation_id: str | None = None,
    order_date: str | None = None,
) -> dict[str, Any]:
    if (
        refusal := validate_place_request(
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            order_type=order_type,
        )
    ) is not None:
        return refusal
    assert price is not None
    if dry_run is not False:
        return preview_place(side=side, symbol=symbol, quantity=quantity, price=price)
    if (refusal := _confirm_refusal(dry_run, confirm)) is not None:
        return refusal

    try:
        client = await client_factory()
    except Exception as exc:  # noqa: BLE001 - verification failed before any order
        return _error(
            "account_verification_failed",
            f"mock account verification failed: {type(exc).__name__}",
            status="not_submitted",
            dispatch_started=False,
        )
    date = order_date or today_order_date()
    row_id, intent, refusal = await _record_submitting(
        ledger,
        order_date=date,
        operation_kind="place",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        strategy=strategy,
        reason=reason,
        correlation_id=correlation_id,
    )
    if refusal is not None or row_id is None or intent is None:
        return refusal or _error("ledger_unavailable", "ledger row missing")
    return await _dispatch_and_record(
        ledger=ledger,
        row_id=row_id,
        send=lambda: client.submit_limit_order(
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            authorization=DryRunConfirmContract(dry_run=False, confirm=True),
            intent=intent,
        ),
        response=_base(
            dry_run=False,
            operation="place",
            order_type=LIMIT_ORDER_TYPE,
            side=side,
            symbol=symbol,
            quantity=quantity,
            price=price,
            order_date=date,
            ledger_id=row_id,
        ),
    )


async def modify_limit_order(
    *,
    client_factory: ClientFactory,
    ledger: NHPlugMockLedgerService,
    order_id: str,
    symbol: str,
    new_price: int | None,
    new_quantity: int | None = None,
    order_type: str = LIMIT_ORDER_TYPE,
    dry_run: bool = True,
    confirm: bool = False,
    order_date: str | None = None,
) -> dict[str, Any]:
    if (refusal := validate_order_type(order_type, new_price)) is not None:
        return refusal
    order_no = _order_no(order_id)
    if order_no is None:
        return _error("invalid_order_id", "order_id must be a positive integer.")
    if not isinstance(symbol, str) or not (len(symbol) == 6 and symbol.isdigit()):
        return _error("invalid_symbol", "symbol must be an exact six-digit KRX code.")
    if _positive_int(new_price, MAX_ORDER_PRICE_KRW) is None:
        return _error("invalid_price", "new_price must be a positive integer KRW.")
    if (
        new_quantity is not None
        and _positive_int(new_quantity, MAX_ORDER_QUANTITY) is None
    ):
        return _error("invalid_quantity", "new_quantity must be a positive integer.")
    assert new_price is not None
    if dry_run is not False:
        return _base(
            success=True,
            dry_run=True,
            dispatch_started=False,
            operation="modify",
            order_id=str(order_no),
            symbol=symbol,
            new_price=new_price,
            new_quantity=new_quantity,
            note="dry_run=True performs no network call and writes no ledger row.",
        )
    if (refusal := _confirm_refusal(dry_run, confirm)) is not None:
        return refusal

    try:
        client = await client_factory()
    except Exception as exc:  # noqa: BLE001
        return _error(
            "account_verification_failed",
            f"mock account verification failed: {type(exc).__name__}",
            status="not_submitted",
            dispatch_started=False,
        )
    date = order_date or today_order_date()
    listing = await collect_listing(client, order_date=date, scope="all")
    if not listing.complete:
        return _error(
            "original_order_unverified",
            "the original order could not be verified from a complete listing "
            f"({listing.reason}); nothing was sent.",
            status="not_submitted",
            dispatch_started=False,
        )
    original = listing.find(order_no)
    if original is None or original.symbol != symbol or original.open_qty <= 0:
        return _error(
            "original_order_not_open",
            "the original order is not an open order for this symbol; nothing was sent.",
            status="not_submitted",
            dispatch_started=False,
        )
    quantity = original.open_qty if new_quantity is None else new_quantity
    if quantity > original.open_qty:
        return _error(
            "quantity_exceeds_open",
            "new_quantity exceeds the open quantity; nothing was sent.",
            status="not_submitted",
            dispatch_started=False,
        )
    full = quantity == original.open_qty
    row_id, intent, refusal = await _record_submitting(
        ledger,
        order_date=date,
        operation_kind="modify",
        symbol=symbol,
        side=original.side,
        quantity=quantity,
        price=new_price,
        original_order_id=str(order_no),
    )
    if refusal is not None or row_id is None or intent is None:
        return refusal or _error("ledger_unavailable", "ledger row missing")
    return await _dispatch_and_record(
        ledger=ledger,
        row_id=row_id,
        send=lambda: client.modify_limit_order(
            original_order_no=order_no,
            symbol=symbol,
            quantity=quantity,
            price=new_price,
            full_quantity=full,
            authorization=DryRunConfirmContract(dry_run=False, confirm=True),
            intent=intent,
        ),
        response=_base(
            dry_run=False,
            operation="modify",
            original_order_id=str(order_no),
            symbol=symbol,
            new_price=new_price,
            quantity=quantity,
            full_quantity=full,
            order_date=date,
            ledger_id=row_id,
        ),
    )


async def cancel_order(
    *,
    client_factory: ClientFactory,
    ledger: NHPlugMockLedgerService,
    order_id: str,
    symbol: str,
    cancel_quantity: int | None = None,
    dry_run: bool = True,
    confirm: bool = False,
    order_date: str | None = None,
) -> dict[str, Any]:
    order_no = _order_no(order_id)
    if order_no is None:
        return _error("invalid_order_id", "order_id must be a positive integer.")
    if not isinstance(symbol, str) or not (len(symbol) == 6 and symbol.isdigit()):
        return _error("invalid_symbol", "symbol must be an exact six-digit KRX code.")
    if (
        cancel_quantity is not None
        and _positive_int(cancel_quantity, MAX_ORDER_QUANTITY) is None
    ):
        return _error("invalid_quantity", "cancel_quantity must be a positive integer.")
    if dry_run is not False:
        return _base(
            success=True,
            dry_run=True,
            dispatch_started=False,
            operation="cancel",
            order_id=str(order_no),
            symbol=symbol,
            cancel_quantity=cancel_quantity,
            note="dry_run=True performs no network call and writes no ledger row.",
        )
    if (refusal := _confirm_refusal(dry_run, confirm)) is not None:
        return refusal

    try:
        client = await client_factory()
    except Exception as exc:  # noqa: BLE001
        return _error(
            "account_verification_failed",
            f"mock account verification failed: {type(exc).__name__}",
            status="not_submitted",
            dispatch_started=False,
        )
    date = order_date or today_order_date()
    listing = await collect_listing(client, order_date=date, scope="all")
    side: str | None = None
    verified_by: str
    if listing.complete:
        original = listing.find(order_no)
        if original is None or original.symbol != symbol or original.open_qty <= 0:
            return _error(
                "original_order_not_open",
                "the original order is not an open order for this symbol; nothing was sent.",
                status="not_submitted",
                dispatch_started=False,
            )
        if cancel_quantity is not None and cancel_quantity > original.open_qty:
            return _error(
                "quantity_exceeds_open",
                "cancel_quantity exceeds the open quantity; nothing was sent.",
                status="not_submitted",
                dispatch_started=False,
            )
        if cancel_quantity == original.open_qty:
            cancel_quantity = None
        side = original.side
        verified_by = "broker_listing"
    else:
        # A cancel only reduces risk; our own accepted ledger row is enough
        # local evidence for a *full* cancel when the listing is unreadable.
        known = await ledger.find_by_broker_order_id(
            order_date=date, broker_order_id=str(order_no)
        )
        if (
            known is None
            or known.symbol != symbol
            or known.operation_kind == "cancel"
            or known.status not in LIVE_ORDER_STATUSES
            or cancel_quantity is not None
        ):
            return _error(
                "original_order_unverified",
                "the original order could not be verified (listing "
                f"{listing.reason}; no live ledger row); nothing was sent.",
                status="not_submitted",
                dispatch_started=False,
            )
        side = known.side
        verified_by = "ledger_row"
    row_id, intent, refusal = await _record_submitting(
        ledger,
        order_date=date,
        operation_kind="cancel",
        symbol=symbol,
        side=side,
        quantity=cancel_quantity,
        price=None,
        original_order_id=str(order_no),
    )
    if refusal is not None or row_id is None or intent is None:
        return refusal or _error("ledger_unavailable", "ledger row missing")
    return await _dispatch_and_record(
        ledger=ledger,
        row_id=row_id,
        send=lambda: client.cancel_order(
            original_order_no=order_no,
            symbol=symbol,
            quantity=cancel_quantity,
            authorization=DryRunConfirmContract(dry_run=False, confirm=True),
            intent=intent,
        ),
        response=_base(
            dry_run=False,
            operation="cancel",
            original_order_id=str(order_no),
            symbol=symbol,
            cancel_quantity=cancel_quantity,
            original_verified_by=verified_by,
            order_date=date,
            ledger_id=row_id,
        ),
    )


async def reconcile_orders(
    client: NHPlugMockClient,
    ledger: NHPlugMockLedgerService,
    *,
    order_date: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Two-source reconcile; dry_run (default) plans without writing."""

    all_listing = await collect_listing(client, order_date=order_date, scope="all")
    open_listing = await collect_listing(client, order_date=order_date, scope="open")
    filled_listing = await collect_listing(
        client, order_date=order_date, scope="filled"
    )
    rows = list(await ledger.list_for_date(order_date))
    planned = plan_reconcile(
        _ledger_views(rows),
        all_listing=all_listing,
        open_listing=open_listing,
        filled_listing=filled_listing,
        all_claimed_order_ids=[row.broker_order_id for row in rows],
    )
    results: list[dict[str, Any]] = []
    for item in planned:
        entry: dict[str, Any] = {
            "ledger_id": item.row_id,
            "reconcile_state": item.update.reconcile_state,
            "status": item.update.status,
            "broker_order_id": item.update.broker_order_id,
            "filled_qty": item.update.filled_qty,
            "note": item.update.note,
        }
        if not dry_run:
            try:
                await ledger.apply_reconcile(item.row_id, item.update)
                entry["applied"] = True
            except Exception as exc:  # noqa: BLE001 - one row never blocks others
                entry["applied"] = False
                entry["apply_error"] = type(exc).__name__
        results.append(entry)
    unknown = sum(
        1 for r in results if r["reconcile_state"] in {"unknown", "source_disagreement"}
    )
    return _base(
        success=all_listing.complete and open_listing.complete,
        dry_run=dry_run,
        order_date=order_date,
        rows_considered=len(rows),
        planned=len(results),
        unresolved=unknown,
        results=results,
        sources=[
            _listing_summary(all_listing),
            _listing_summary(open_listing),
            _listing_summary(filled_listing),
        ],
    )
