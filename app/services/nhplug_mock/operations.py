"""Operations layer (design actor O) behind the nh_mock_* MCP tools (#849).

Every mutating flow runs these steps in order, and each step refuses before
the next one starts:

1. argument grammar, including the limit-only check (no network, no DB)
2. ``dry_run``/``confirm`` exact booleans; a dry run stops here
3. caller idempotency key
4. ``NHPLUG_MOCK_ENABLED`` and the five Stage 2 confirmations
5. process credentials (missing keys are reported by name only)
6. the retained HMAC key registry (DB read)
7. a fresh ``/n2/acctinfo`` read that must list the configured account as
   ``acct_type=03`` (the only network call before an order)
8. ``account_ref``; for modify/cancel also ledger ownership and a complete
   broker listing that shows the order still open
9. T1 ``create_intent`` and the single #711 dispatcher

The answer after a dispatch comes from the durable ledger row, never from the
exception type: a fenced row is ``uncertain`` until manual reconciliation.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, TypeVar, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.brokers.nhplug.account_guard import MockAccountAllowlist
from app.services.brokers.nhplug.auth import NHPlugAuthClient
from app.services.brokers.nhplug.client import NHPlugMockClient
from app.services.brokers.nhplug.errors import (
    NHPlugMockAccountRejected,
    NHPlugMockBrokerRejected,
    NHPlugMockDisabled,
)
from app.services.brokers.nhplug.gating import _assert_mock_enabled
from app.services.brokers.nhplug.order_evidence import (
    EMPTY_IS_NOT_EVIDENCE,
    OrderListing,
    OrderRow,
    derive_order_status,
    determine_open_orders,
    strict_int,
)
from app.services.nhplug_mock.account_identity import (
    AccountIdentityError,
    KeyMaterial,
    load_retained_keys_from_env,
    resolve_account_ref,
)
from app.services.nhplug_mock.intent import InvalidIntent, OrderIntent
from app.services.nhplug_mock.ledger import (
    LedgerConflict,
    NHPlugMockLedger,
    own_number_attributes_match,
)
from app.services.nhplug_mock.readiness import Stage2Disabled, Stage2Readiness
from app.services.nhplug_mock.transport import Stage2Timing

ACCOUNT_MODE: Final[str] = "nh_mock"
SOURCE: Final[str] = "nhplug_mock"
REQUIRED_ENV_KEYS: Final[tuple[str, ...]] = (
    "NHPLUG_APP_KEY",
    "NHPLUG_APP_SECRET",
    "NHPLUG_MOCK_ACCOUNT_NO",
)
ORDER_TYPE_LIMIT: Final[str] = "limit"
# A bound place order can be modified or cancelled while the broker still
# lists it open; a bound modify request's number is its successor order.
PLACE_MODIFIABLE_STATES: Final[frozenset[str]] = frozenset(
    {"accepted", "open", "partially_filled"}
)
MODIFY_MODIFIABLE_STATES: Final[frozenset[str]] = frozenset({"accepted", "confirmed"})
_NOT_SENT_STATES: Final[frozenset[str]] = frozenset({"intent", "claimed", "withdrawn"})
_SEND_UNKNOWN_STATES: Final[frozenset[str]] = frozenset({"sending", "uncertain"})
# Rows a reconcile run is expected to settle: send-unknown rows and bound rows
# whose broker picture it re-verifies.
_RECONCILE_TARGET_STATES: Final[frozenset[str]] = _SEND_UNKNOWN_STATES | frozenset(
    {"accepted", "open", "partially_filled"}
)
_BOUND_STATES: Final[frozenset[str]] = frozenset(
    {
        "accepted",
        "open",
        "partially_filled",
        "filled",
        "cancelled",
        "modified",
        "confirmed",
    }
)
_SYMBOL_RE: Final = re.compile(r"^[0-9]{6}$")
_ORDER_NO_RE: Final = re.compile(r"^[1-9][0-9]{0,9}$")
_IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DAY_RE: Final = re.compile(r"^[0-9]{8}$")
_SEOUL: Final = ZoneInfo("Asia/Seoul")
_Flow = TypeVar("_Flow", bound=Callable[..., Awaitable[dict[str, Any]]])


class NHMockRefusal(Exception):
    """A refusal before any ledger write or order request."""

    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class _Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Session:
    client: NHPlugMockClient
    ledger: NHPlugMockLedger
    account_no: str = field(repr=False)
    keys: dict[int, KeyMaterial] | None = field(repr=False)


# --------------------------------------------------------------------------
# Runtime wiring (tests replace _engine and _new_client)
# --------------------------------------------------------------------------

# One OAuth client per credential pair, so its 24-hour token cache is reused.
_auth_cache: tuple[str, str, NHPlugAuthClient] | None = None


def _engine() -> AsyncEngine:
    from app.core.db import engine

    return engine


def _credentials() -> _Credentials:
    values = {name: (os.getenv(name) or "").strip() for name in REQUIRED_ENV_KEYS}
    missing = [name for name in REQUIRED_ENV_KEYS if not values[name]]
    if missing:
        raise NHMockRefusal("credentials_missing", missing_env_keys=missing)
    return _Credentials(
        app_key=values["NHPLUG_APP_KEY"],
        app_secret=values["NHPLUG_APP_SECRET"],
        account_no=values["NHPLUG_MOCK_ACCOUNT_NO"],
    )


def _new_client(credentials: _Credentials) -> NHPlugMockClient:
    """A fresh client per call, so account authority is always a fresh read."""

    global _auth_cache
    cached = _auth_cache
    if (
        cached is None
        or cached[0] != credentials.app_key
        or cached[1] != credentials.app_secret
    ):
        auth = NHPlugAuthClient(
            app_key=credentials.app_key, app_secret=credentials.app_secret
        )
        _auth_cache = (credentials.app_key, credentials.app_secret, auth)
    else:
        auth = cached[2]
    return NHPlugMockClient(
        app_key=credentials.app_key,
        app_secret=credentials.app_secret,
        token_provider=auth.get_access_token,
    )


def _seoul_today() -> date:
    return datetime.now(_SEOUL).date()


# --------------------------------------------------------------------------
# Argument grammar (no network, no DB)
# --------------------------------------------------------------------------


def _require_limit_order(order_type: object, price: object) -> int:
    """The only accepted order type is the exact string "limit" with a price."""

    if type(order_type) is not str or order_type != ORDER_TYPE_LIMIT:
        raise NHMockRefusal("limit_order_only", accepted_order_type=ORDER_TYPE_LIMIT)
    if type(price) is not int or price <= 0:
        raise NHMockRefusal("limit_price_required")
    return price


def _require_positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise NHMockRefusal("invalid_argument", argument=name)
    return value


def _require_symbol(symbol: object) -> str:
    if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
        raise NHMockRefusal("invalid_argument", argument="symbol")
    return symbol


def _require_side(side: object) -> str:
    if type(side) is not str or side not in {"buy", "sell"}:
        raise NHMockRefusal("invalid_argument", argument="side")
    return side


def _require_order_no(order_id: object) -> str:
    if type(order_id) is not str or _ORDER_NO_RE.fullmatch(order_id) is None:
        raise NHMockRefusal("invalid_argument", argument="order_id")
    return order_id


def _require_confirmation(dry_run: object, confirm: object) -> bool:
    """Return True for a dry run; a real call needs exact dry_run=False, confirm=True."""

    if type(dry_run) is not bool or type(confirm) is not bool:
        raise NHMockRefusal("invalid_argument", argument="dry_run/confirm")
    if dry_run:
        return True
    if confirm is not True:
        raise NHMockRefusal("confirm_required")
    return False


def _require_idempotency_key(key: object) -> str:
    if type(key) is not str or _IDEMPOTENCY_KEY_RE.fullmatch(key) is None:
        raise NHMockRefusal(
            "idempotency_key_required", pattern=_IDEMPOTENCY_KEY_RE.pattern
        )
    return key


def _parse_day(order_date: object) -> date:
    if order_date is None:
        return _seoul_today()
    if type(order_date) is not str or _DAY_RE.fullmatch(order_date) is None:
        raise NHMockRefusal("invalid_argument", argument="order_date")
    try:
        return datetime.strptime(order_date, "%Y%m%d").date()
    except ValueError:
        raise NHMockRefusal("invalid_argument", argument="order_date") from None


# --------------------------------------------------------------------------
# Gates and the account guard
# --------------------------------------------------------------------------


def _assert_gates(*, stage2: bool) -> None:
    try:
        _assert_mock_enabled()
        if stage2:
            Stage2Readiness.from_env().assert_ready()
    except NHPlugMockDisabled:
        raise NHMockRefusal("mock_gate_disabled") from None
    except Stage2Disabled as exc:
        raise NHMockRefusal("stage2_not_ready", gate=exc.code) from None


async def _require_verified_mock_account(
    client: NHPlugMockClient, account_no: str
) -> None:
    """The account guard: a fresh acctinfo read must list it as acct_type=03."""

    try:
        await client.verify_and_bind_mock_account(account_no)
        allowlist = client._account_allowlist
        if type(allowlist) is not MockAccountAllowlist:
            raise NHPlugMockAccountRejected("no broker-verified allowlist was bound")
        allowlist.assert_allowed(account_no)
    except NHPlugMockAccountRejected:
        raise NHMockRefusal("mock_account_rejected") from None
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal
        raise NHMockRefusal(
            "mock_account_unverified", error_type=type(exc).__name__
        ) from None


async def _verified_session(*, stage2: bool) -> _Session:
    _assert_gates(stage2=stage2)
    credentials = _credentials()
    engine = _engine()
    keys: dict[int, KeyMaterial] | None = None
    if stage2:
        try:
            keys = await load_retained_keys_from_env(engine)
        except AccountIdentityError as exc:
            raise NHMockRefusal(exc.code) from None
    client = _new_client(credentials)
    await _require_verified_mock_account(client, credentials.account_no)
    return _Session(
        client=client,
        ledger=NHPlugMockLedger(engine),
        account_no=credentials.account_no,
        keys=keys,
    )


async def _account_ref(session: _Session, *, create: bool) -> UUID:
    assert session.keys is not None
    try:
        return await resolve_account_ref(
            session.ledger.engine, session.account_no, session.keys, create=create
        )
    except AccountIdentityError as exc:
        if exc.code == "account_binding_missing":
            raise NHMockRefusal("order_not_owned") from None
        raise NHMockRefusal(exc.code) from None


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


def _base(tool: str) -> dict[str, Any]:
    return {"tool": tool, "account_mode": ACCOUNT_MODE, "source": SOURCE}


def refusal_response(tool: str, refusal: NHMockRefusal) -> dict[str, Any]:
    return {
        **_base(tool),
        "success": False,
        "status": "rejected_before_send",
        "error": refusal.code,
        "sent": False,
        **refusal.details,
    }


def _sanitized(tool: str) -> Callable[[_Flow], _Flow]:
    """No raw exception text leaves a tool; the dispatcher is never reached here.

    Every exception that can escape a public flow is raised before the single
    dispatch call site (which catches its own failures) or by a read, so the
    answer can say nothing was sent. Process-control exceptions pass through.
    """

    def decorate(flow: _Flow) -> _Flow:
        @functools.wraps(flow)
        async def guarded(**kwargs: Any) -> dict[str, Any]:
            try:
                return await flow(**kwargs)
            except NHMockRefusal as refusal:
                return refusal_response(tool, refusal)
            except Exception as exc:  # noqa: BLE001 - sanitized, type name only
                return {
                    **_base(tool),
                    "success": False,
                    "status": "error",
                    "error": "internal_error",
                    "error_type": type(exc).__name__,
                    "sent": False,
                }

        return cast(_Flow, guarded)

    return decorate


def _error_code(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    code = getattr(exc, "code", None)
    return code if type(code) is str else type(exc).__name__


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ledger_row_id": row["id"],
        "operation_kind": row["operation_kind"],
        "side": row["side"],
        "symbol": row["symbol"],
        "quantity": row["quantity"],
        "limit_price": row["price"],
        "original_order_id": row["original_order_id"],
        "amend_scope": row["amend_scope"],
        "order_date": str(row["order_date"]),
        "state": row["state"],
        "reconcile_state": row["reconcile_state"],
        "broker_order_id": row["broker_order_id"],
        "ack_evidence_order_id": row["ack_evidence_order_id"],
        "uncertain_reason": row["uncertain_reason"],
        "withdraw_reason": row["withdraw_reason"],
        "requires_manual_review": row["requires_manual_review"],
        "idempotency_key": row["idempotency_key"],
        "attempt_no": row["attempt_no"],
    }


def _row_response(
    tool: str,
    row: dict[str, Any] | None,
    *,
    replayed: bool,
    dispatch_error: BaseException | None = None,
    fallback_row_id: int | None = None,
) -> dict[str, Any]:
    result = _base(tool)
    if dispatch_error is not None:
        result["dispatch_error"] = _error_code(dispatch_error)
    if row is None:
        # The durable state could not be read back: never claim "not sent".
        result.update(
            success=False,
            status="unknown",
            ledger_row_id=fallback_row_id,
            reconcile_required=True,
            retry_allowed=False,
        )
        return result
    state = row["state"]
    result.update(_row_summary(row))
    result["replayed_existing_row"] = replayed
    if state in _NOT_SENT_STATES:
        result.update(
            success=False,
            status="not_submitted",
            sent=False,
            reconcile_required=False,
            retry_allowed=state in {"intent", "withdrawn"},
        )
    elif state in _SEND_UNKNOWN_STATES:
        result.update(
            success=False,
            status="uncertain",
            reconcile_required=True,
            retry_allowed=False,
            note=(
                "the order may exist at the broker; run nh_mock_reconcile_orders. "
                "A response order number is evidence only, not acceptance."
            ),
        )
    elif state in _BOUND_STATES:
        result.update(
            success=True, status=state, reconcile_required=False, retry_allowed=False
        )
    else:
        result.update(
            success=False,
            status=state,
            reconcile_required=state == "anomaly",
            retry_allowed=False,
        )
    return result


# --------------------------------------------------------------------------
# The single dispatch call site
# --------------------------------------------------------------------------


async def _dispatch_new_intent(
    tool: str, session: _Session, intent: OrderIntent, idempotency_key: str
) -> dict[str, Any]:
    """T1 then the #711 dispatcher; the only place this layer can send."""

    assert session.keys is not None
    try:
        readiness = Stage2Readiness.from_env()
        timing = Stage2Timing.from_env()
    except ValueError:
        raise NHMockRefusal("stage2_timing_invalid") from None
    try:
        intent.validate()
        row, should_claim = await session.ledger.create_intent(
            intent,
            readiness=readiness,
            idempotency_key=idempotency_key,
            order_date=_seoul_today(),
        )
    except InvalidIntent:
        raise NHMockRefusal("invalid_order_intent") from None
    except (LedgerConflict, Stage2Disabled) as exc:
        raise NHMockRefusal(exc.code) from None
    if not should_claim:
        return _row_response(tool, row, replayed=True)
    failure: Exception | None = None
    try:
        await session.client.dispatch_claimed_order(
            session.ledger,
            row["id"],
            row["client_request_id"],
            row["body_digest"],
            intent.account_ref,
            keys=session.keys,
            readiness=readiness,
            timing=timing,
            dry_run=False,
            confirm=True,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - the durable row decides the answer
        failure = exc
    try:
        current = await session.ledger.get(row["id"])
    except Exception:  # noqa: BLE001 - unknown state is reported as unknown
        current = None
    return _row_response(
        tool,
        current,
        replayed=False,
        dispatch_error=failure,
        fallback_row_id=row["id"],
    )


# --------------------------------------------------------------------------
# Ownership and broker-side modifiability (modify / cancel)
# --------------------------------------------------------------------------


async def _require_owned_modifiable(
    session: _Session, account_ref: UUID, order_id: str, symbol: str | None
) -> dict[str, Any]:
    row = await session.ledger.find_owned_order(account_ref, order_id)
    if row is None:
        referencing = await session.ledger.rows_referencing(account_ref, order_id)
        if any(
            r["ack_evidence_order_id"] == order_id and r["state"] == "uncertain"
            for r in referencing
        ):
            raise NHMockRefusal(
                "order_not_bound_reconcile_first",
                detail="the number is only uncertain evidence; reconcile binds it first",
            )
        raise NHMockRefusal(
            "order_not_owned",
            detail="modify/cancel is limited to orders this ledger dispatched and bound",
        )
    allowed = (
        PLACE_MODIFIABLE_STATES
        if row["operation_kind"] == "place"
        else MODIFY_MODIFIABLE_STATES
    )
    if row["state"] not in allowed:
        raise NHMockRefusal(
            "order_not_modifiable", ledger_state=row["state"], ledger_row_id=row["id"]
        )
    if row["order_date"] != _seoul_today():
        raise NHMockRefusal(
            "order_not_modifiable",
            reason="order_from_another_trading_day",
            ledger_row_id=row["id"],
        )
    if symbol is not None and symbol != row["symbol"]:
        raise NHMockRefusal("symbol_mismatch", ledger_row_id=row["id"])
    return row


async def _require_open_on_broker(
    session: _Session, owned: dict[str, Any], order_id: str
) -> OrderRow:
    assert session.keys is not None
    listing = await session.client.fetch_order_listing(
        ledger=session.ledger,
        keys=session.keys,
        order_date=owned["order_date"].strftime("%Y%m%d"),
        scope="all",
    )
    if not listing.complete:
        raise NHMockRefusal(
            "listing_incomplete",
            reason=listing.reason,
            note=EMPTY_IS_NOT_EVIDENCE,
        )
    target = listing.find(int(order_id))
    if target is None:
        raise NHMockRefusal("order_not_listed", note=EMPTY_IS_NOT_EVIDENCE)
    if (
        target.symbol != owned["symbol"]
        or target.side != owned["side"]
        or target.open_qty <= 0
        or derive_order_status(target) not in {"open", "partially_filled"}
    ):
        raise NHMockRefusal("order_not_open_on_broker", broker_order=target.evidence())
    return target


# --------------------------------------------------------------------------
# Public flows
# --------------------------------------------------------------------------


def _place_plan(symbol: str, side: str, quantity: int, price: int) -> dict[str, Any]:
    return {
        "operation_kind": "place",
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "limit_price": price,
        "order_type": ORDER_TYPE_LIMIT,
        "market": "KRX",
        "notional_krw": quantity * price,
    }


@_sanitized("nh_mock_preview_order")
async def preview_order(
    *,
    symbol: object,
    side: object,
    quantity: object,
    price: object,
    order_type: object = ORDER_TYPE_LIMIT,
) -> dict[str, Any]:
    tool = "nh_mock_preview_order"
    try:
        checked_price = _require_limit_order(order_type, price)
        plan = _place_plan(
            _require_symbol(symbol),
            _require_side(side),
            _require_positive_int("quantity", quantity),
            checked_price,
        )
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)
    return {
        **_base(tool),
        "success": True,
        "status": "preview",
        "sent": False,
        "network_calls": 0,
        "plan": plan,
        "note": "offline grammar check only; ownership, balance and gates are checked on send",
    }


@_sanitized("nh_mock_place_order")
async def place_order(
    *,
    symbol: object,
    side: object,
    quantity: object,
    price: object,
    order_type: object = ORDER_TYPE_LIMIT,
    idempotency_key: object = None,
    dry_run: object = True,
    confirm: object = False,
) -> dict[str, Any]:
    tool = "nh_mock_place_order"
    try:
        checked_price = _require_limit_order(order_type, price)
        plan = _place_plan(
            _require_symbol(symbol),
            _require_side(side),
            _require_positive_int("quantity", quantity),
            checked_price,
        )
        if _require_confirmation(dry_run, confirm):
            return {
                **_base(tool),
                "success": True,
                "status": "dry_run",
                "sent": False,
                "plan": plan,
            }
        key = _require_idempotency_key(idempotency_key)
        session = await _verified_session(stage2=True)
        account_ref = await _account_ref(session, create=True)
        intent = OrderIntent(
            "place",
            plan["side"],
            plan["symbol"],
            plan["quantity"],
            plan["limit_price"],
            None,
            None,
            account_ref,
        )
        return await _dispatch_new_intent(tool, session, intent, key)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)


@_sanitized("nh_mock_modify_order")
async def modify_order(
    *,
    order_id: object,
    new_price: object,
    new_quantity: object,
    symbol: object = None,
    order_type: object = ORDER_TYPE_LIMIT,
    idempotency_key: object = None,
    dry_run: object = True,
    confirm: object = False,
) -> dict[str, Any]:
    tool = "nh_mock_modify_order"
    try:
        number = _require_order_no(order_id)
        price = _require_limit_order(order_type, new_price)
        quantity = _require_positive_int("new_quantity", new_quantity)
        checked_symbol = None if symbol is None else _require_symbol(symbol)
        plan = {
            "operation_kind": "modify",
            "original_order_id": number,
            "new_quantity": quantity,
            "limit_price": price,
            "order_type": ORDER_TYPE_LIMIT,
        }
        if _require_confirmation(dry_run, confirm):
            return {
                **_base(tool),
                "success": True,
                "status": "dry_run",
                "sent": False,
                "plan": plan,
                "note": "ownership and broker open quantity are checked on send",
            }
        key = _require_idempotency_key(idempotency_key)
        session = await _verified_session(stage2=True)
        account_ref = await _account_ref(session, create=False)
        owned = await _require_owned_modifiable(
            session, account_ref, number, checked_symbol
        )
        target = await _require_open_on_broker(session, owned, number)
        if quantity > target.open_qty:
            raise NHMockRefusal("quantity_exceeds_open", open_quantity=target.open_qty)
        scope = "full" if quantity == target.open_qty else "partial"
        intent = OrderIntent(
            "modify",
            owned["side"],
            owned["symbol"],
            quantity,
            price,
            number,
            scope,
            account_ref,
        )
        return await _dispatch_new_intent(tool, session, intent, key)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)


@_sanitized("nh_mock_cancel_order")
async def cancel_order(
    *,
    order_id: object,
    cancel_quantity: object = None,
    symbol: object = None,
    idempotency_key: object = None,
    dry_run: object = True,
    confirm: object = False,
) -> dict[str, Any]:
    tool = "nh_mock_cancel_order"
    try:
        number = _require_order_no(order_id)
        quantity = (
            None
            if cancel_quantity is None
            else _require_positive_int("cancel_quantity", cancel_quantity)
        )
        checked_symbol = None if symbol is None else _require_symbol(symbol)
        plan = {
            "operation_kind": "cancel",
            "original_order_id": number,
            "cancel_quantity": quantity,
        }
        if _require_confirmation(dry_run, confirm):
            return {
                **_base(tool),
                "success": True,
                "status": "dry_run",
                "sent": False,
                "plan": plan,
                "note": "ownership and broker open quantity are checked on send",
            }
        key = _require_idempotency_key(idempotency_key)
        session = await _verified_session(stage2=True)
        account_ref = await _account_ref(session, create=False)
        owned = await _require_owned_modifiable(
            session, account_ref, number, checked_symbol
        )
        target = await _require_open_on_broker(session, owned, number)
        if quantity is not None and quantity > target.open_qty:
            raise NHMockRefusal("quantity_exceeds_open", open_quantity=target.open_qty)
        full = quantity is None or quantity == target.open_qty
        intent = OrderIntent(
            "cancel",
            owned["side"],
            owned["symbol"],
            None if full else quantity,
            None,
            number,
            "full" if full else "partial",
            account_ref,
        )
        return await _dispatch_new_intent(tool, session, intent, key)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


async def _balance(tool: str) -> tuple[_Session, dict[str, Any]]:
    session = await _verified_session(stage2=False)
    try:
        payload = await session.client.fetch_balance(act_no=session.account_no)
    except NHPlugMockBrokerRejected as exc:
        raise NHMockRefusal(
            "broker_read_rejected", broker_response_code=exc.response_code
        ) from None
    except Exception as exc:  # noqa: BLE001 - reads fail closed with a type only
        raise NHMockRefusal(
            "broker_read_failed", error_type=type(exc).__name__
        ) from None
    return session, payload


def _parse_position(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    symbol = row.get("iem_cd")
    quantity = strict_int(row.get("itg_bnc_qty"))
    if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
        return None
    if quantity is None:
        return None
    name = row.get("iem_nm")
    return {
        "symbol": symbol,
        "name": name if type(name) is str else None,
        "quantity": quantity,
        "avg_price": strict_int(row.get("phs_pr")),
        "current_price": strict_int(row.get("now_pr")),
        "evaluation_amount": strict_int(row.get("eal_amt")),
    }


@_sanitized("nh_mock_get_positions")
async def get_positions() -> dict[str, Any]:
    tool = "nh_mock_get_positions"
    try:
        _, payload = await _balance(tool)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)
    rows = payload.get("Output_1")
    if not isinstance(rows, list):
        return {
            **_base(tool),
            "success": False,
            "status": "unknown",
            "positions_state": "unknown",
            "positions": [],
            "reason": "holdings_block_absent",
            "rsp_cd": payload.get("rsp_cd"),
        }
    parsed = [_parse_position(row) for row in rows]
    positions = [p for p in parsed if p is not None]
    unparsed = len(parsed) - len(positions)
    state = "unknown" if unparsed else ("reported" if positions else "empty_reported")
    return {
        **_base(tool),
        "success": unparsed == 0,
        "status": "ok" if unparsed == 0 else "unknown",
        "positions_state": state,
        "positions": positions,
        "unparsed_rows": unparsed,
    }


@_sanitized("nh_mock_get_orderable_cash")
async def get_orderable_cash() -> dict[str, Any]:
    tool = "nh_mock_get_orderable_cash"
    try:
        _, payload = await _balance(tool)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)
    summary = payload.get("Output_0")
    cash = strict_int(summary.get("orr_pbl_amt")) if isinstance(summary, dict) else None
    return {
        **_base(tool),
        "success": cash is not None,
        "status": "ok" if cash is not None else "unknown",
        "cash": cash,
        "currency": "KRW",
        "cash_source": "balance.orr_pbl_amt"
        if cash is not None
        else "balance_unparsed",
    }


async def _listing(session: _Session, day: date, scope: str) -> OrderListing:
    assert session.keys is not None
    try:
        return await session.client.fetch_order_listing(
            ledger=session.ledger,
            keys=session.keys,
            order_date=day.strftime("%Y%m%d"),
            scope=scope,
        )
    except AccountIdentityError as exc:
        raise NHMockRefusal(exc.code) from None
    except Exception as exc:  # noqa: BLE001 - a failed listing is not "no rows"
        raise NHMockRefusal(
            "broker_read_failed", scope=scope, error_type=type(exc).__name__
        ) from None


def _open_orders(
    all_listing: OrderListing, open_listing: OrderListing, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    live = [
        int(r["broker_order_id"])
        for r in rows
        if r["broker_order_id"] is not None
        and r["state"] in {"accepted", "open", "partially_filled"}
    ]
    determination = determine_open_orders(
        all_listing=all_listing,
        open_listing=open_listing,
        ledger_live_order_nos=live,
        ledger_has_unbound_uncertain=any(
            r["state"] in _SEND_UNKNOWN_STATES for r in rows
        ),
    )
    return {
        "open_orders_state": determination.state,
        "open_orders": [row.evidence() for row in determination.open_rows],
        "open_orders_reasons": list(determination.reasons),
    }


@_sanitized("nh_mock_get_order_history")
async def get_order_history(*, order_date: object = None) -> dict[str, Any]:
    tool = "nh_mock_get_order_history"
    try:
        day = _parse_day(order_date)
        session = await _verified_session(stage2=True)
        all_listing = await _listing(session, day, "all")
        open_listing = await _listing(session, day, "open")
        account_ref = all_listing.account_ref
        rows = (
            await session.ledger.rows_for_day(account_ref, day)
            if account_ref is not None
            else []
        )
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)
    complete = all_listing.complete
    incomplete_scopes = [
        name
        for name, listing in (("all", all_listing), ("open", open_listing))
        if not listing.complete
    ]
    return {
        **_base(tool),
        "success": not incomplete_scopes,
        "status": "ok" if not incomplete_scopes else "unknown",
        "incomplete_scopes": incomplete_scopes,
        "order_date": day.strftime("%Y%m%d"),
        "orders_state": "complete" if complete else "unknown",
        "orders": [row.evidence() for row in all_listing.rows] if complete else [],
        "listing_reason": all_listing.reason,
        **_open_orders(all_listing, open_listing, rows),
        "ledger_rows": [_row_summary(r) for r in rows],
        "note": EMPTY_IS_NOT_EVIDENCE,
    }


@_sanitized("nh_mock_get_order_detail")
async def get_order_detail(
    *, order_id: object, order_date: object = None
) -> dict[str, Any]:
    tool = "nh_mock_get_order_detail"
    try:
        number = _require_order_no(order_id)
        day = _parse_day(order_date)
        session = await _verified_session(stage2=True)
        listing = await _listing(session, day, "all")
        account_ref = listing.account_ref
        rows = (
            await session.ledger.rows_referencing(account_ref, number)
            if account_ref is not None
            else []
        )
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)
    broker: dict[str, Any]
    if not listing.complete:
        broker = {
            "broker_view": "unknown",
            "reason": "order_listing_incomplete",
            "listing_reason": listing.reason,
        }
    elif (found := listing.find(int(number))) is None:
        broker = {"broker_view": "not_listed", "note": EMPTY_IS_NOT_EVIDENCE}
    else:
        broker = {
            "broker_view": "listed",
            "broker_order": found.evidence(),
            "derived_status": derive_order_status(found),
        }
    return {
        **_base(tool),
        # Ledger rows alone never make an incomplete listing a success.
        "success": listing.complete
        and (broker["broker_view"] == "listed" or bool(rows)),
        "status": broker["broker_view"],
        "order_id": number,
        "order_date": day.strftime("%Y%m%d"),
        "owned_by_ledger": any(
            r["broker_order_id"] == number
            and r["operation_kind"] in {"place", "modify"}
            for r in rows
        ),
        "ledger_rows": [_row_summary(r) for r in rows],
        **broker,
    }


# --------------------------------------------------------------------------
# Manual reconciliation (design actors V and R)
# --------------------------------------------------------------------------


def _planned_action(row: dict[str, Any]) -> str:
    state = row["state"]
    if state == "uncertain":
        return (
            "verify_own_number"
            if row["ack_evidence_order_id"] is not None
            else "record_candidates"
        )
    if state in {"accepted", "open", "partially_filled"}:
        return "reconcile_bound"
    if state in {"intent", "claimed", "sending"}:
        return "recovery_if_expired"
    return "none"


def _needs_review(row: dict[str, Any]) -> bool:
    return row["state"] == "anomaly" or row["requires_manual_review"] is True


def _reconcile_status(
    *,
    incomplete_scopes: list[str],
    unverified: list[int],
    unresolved: list[int],
    needs_review: list[int],
    resolved: list[int],
) -> str:
    """One rule for the confirmed answer and the dry-run prediction.

    ``reconciled`` only when every targeted row is resolved. An unverified row
    or an incomplete scope is ``unknown``; otherwise any unresolved row makes the
    run ``partial`` (some rows resolved) or ``uncertain`` (none resolved); with
    none unresolved, an anomaly or manual-review row makes it ``needs_review``.
    A needs-review row is never counted as resolved.
    """

    if incomplete_scopes or unverified:
        return "unknown"
    if unresolved:
        return "partial" if resolved else "uncertain"
    if needs_review:
        return "needs_review"
    return "reconciled"


def _dry_run_prediction(
    rows: list[dict[str, Any]], all_listing: OrderListing, incomplete: list[str]
) -> dict[str, Any]:
    """Predict the confirmed status without writes.

    A send-unknown row is predicted unresolved when no confirmed write can bind
    it: a sending row, an uncertain row without its own number, or one whose
    number the complete all-scope listing does not show. A row needs review
    when it already is anomaly or flagged for manual review, when candidate
    recording will flag it (uncertain, no own number, complete listing), or
    when its listed number carries other attributes (T9b records anomaly).
    Bound rows, and uncertain rows whose listed number matches, are settled
    only by the confirmed run's ledger checks, which can still report
    ``unknown``; with nothing else to report the prediction is
    ``verification_pending``, never ``reconciled``.
    """

    unresolved: list[int] = []
    needs_review: list[int] = []
    pending: list[int] = []
    for row in rows:
        target = None
        number = row["ack_evidence_order_id"]
        if row["state"] == "uncertain" and number is not None and all_listing.complete:
            target = all_listing.find(int(number))
        if _needs_review(row) or (
            row["state"] == "uncertain"
            and all_listing.complete
            and (
                number is None
                or (target is not None and not own_number_attributes_match(row, target))
            )
        ):
            needs_review.append(row["id"])
        if row["state"] not in _RECONCILE_TARGET_STATES:
            continue
        if row["state"] == "sending" or (
            row["state"] == "uncertain" and target is None
        ):
            unresolved.append(row["id"])
        elif row["id"] not in needs_review:
            pending.append(row["id"])
    status = _reconcile_status(
        incomplete_scopes=incomplete,
        unverified=[],
        unresolved=unresolved,
        needs_review=needs_review,
        resolved=pending,
    )
    if status == "reconciled" and pending:
        status = "verification_pending"
    return {
        "would_be_status": status,
        "would_be_unresolved_row_ids": unresolved,
        "would_be_needs_review_row_ids": needs_review,
        "verification_pending_row_ids": pending,
    }


@_sanitized("nh_mock_reconcile_orders")
async def reconcile_orders(
    *, order_date: object = None, dry_run: object = True, confirm: object = False
) -> dict[str, Any]:
    tool = "nh_mock_reconcile_orders"
    try:
        day = _parse_day(order_date)
        planning_only = _require_confirmation(dry_run, confirm)
        session = await _verified_session(stage2=True)
        readiness = Stage2Readiness.from_env()
        try:
            timing = Stage2Timing.from_env()
        except ValueError:
            raise NHMockRefusal("stage2_timing_invalid") from None
        all_listing = await _listing(session, day, "all")
        open_listing = await _listing(session, day, "open")
        filled_listing = await _listing(session, day, "filled")
        account_ref = all_listing.account_ref
        if account_ref is None:
            raise NHMockRefusal("listing_scope_missing")
        # V recovery runs at the start of a confirmed reconcile, never on a timer.
        recovered: tuple[int, int, int] | None = None
        if not planning_only:
            recovered = await session.ledger.recover_expired(
                intent_stale_seconds=timing.intent_stale_seconds
            )
        rows = await session.ledger.rows_for_day(account_ref, day)
    except NHMockRefusal as refusal:
        return refusal_response(tool, refusal)

    listings = {
        "all": {"complete": all_listing.complete, "reason": all_listing.reason},
        "open": {"complete": open_listing.complete, "reason": open_listing.reason},
        "filled": {
            "complete": filled_listing.complete,
            "reason": filled_listing.reason,
        },
    }
    base = {
        **_base(tool),
        "order_date": day.strftime("%Y%m%d"),
        "listings": listings,
        **_open_orders(all_listing, open_listing, rows),
    }
    incomplete_scopes = [
        name
        for name, listing in (
            ("all", all_listing),
            ("open", open_listing),
            ("filled", filled_listing),
        )
        if not listing.complete
    ]
    if planning_only:
        return {
            **base,
            "success": True,
            "status": "dry_run",
            "ledger_writes": 0,
            "incomplete_scopes": incomplete_scopes,
            **_dry_run_prediction(rows, all_listing, incomplete_scopes),
            "rows": [
                {**_row_summary(r), "planned_action": _planned_action(r)} for r in rows
            ],
        }

    targeted = {r["id"] for r in rows if r["state"] in _RECONCILE_TARGET_STATES}
    results: dict[int, dict[str, Any]] = {}
    if all_listing.complete:
        for row in rows:
            if row["state"] != "uncertain":
                continue
            try:
                if row["ack_evidence_order_id"] is not None:
                    bound = await session.ledger.verify_own_number(
                        row["id"], all_listing, readiness=readiness
                    )
                    results[row["id"]] = {
                        "action": "verify_own_number",
                        "bound": bound,
                    }
                else:
                    candidates = await session.ledger.record_uncertain_candidates(
                        row["id"], all_listing, readiness=readiness
                    )
                    results[row["id"]] = {
                        "action": "record_candidates",
                        "candidate_count": len(candidates),
                    }
            except (LedgerConflict, Stage2Disabled) as exc:
                results[row["id"]] = {"action": "error", "error": exc.code}
        rows = await session.ledger.rows_for_day(account_ref, day)
        for row in rows:
            if row["state"] not in {"accepted", "open", "partially_filled"}:
                continue
            if not open_listing.complete:
                # Skipped is not verified: the response below cannot say reconciled.
                results[row["id"]] = {
                    **results.get(row["id"], {}),
                    "reconcile": "skipped",
                    "reason": "open_scope_incomplete",
                }
                continue
            try:
                outcome = await session.ledger.reconcile_bound(
                    row["id"],
                    all_listing,
                    open_listing,
                    filled_listing,
                    readiness=readiness,
                )
                results[row["id"]] = {
                    **results.get(row["id"], {}),
                    "reconcile": outcome,
                }
            except (LedgerConflict, Stage2Disabled) as exc:
                results[row["id"]] = {
                    **results.get(row["id"], {}),
                    "reconcile_error": exc.code,
                }
    final_rows = await session.ledger.rows_for_day(account_ref, day)
    unresolved = [r for r in final_rows if r["state"] in _SEND_UNKNOWN_STATES]
    # A bound row counts as verified only when reconcile_bound returned a state;
    # "unknown", "skipped", or an error leaves the broker picture unverified.
    unverified = sorted(
        row_id
        for row_id, result in results.items()
        if "reconcile_error" in result
        or result.get("reconcile") in {"unknown", "skipped"}
        or result.get("action") == "error"
    )
    unresolved_ids = [r["id"] for r in unresolved]
    needs_review = [r["id"] for r in final_rows if _needs_review(r)]
    resolved = sorted(
        targeted - set(unresolved_ids) - set(unverified) - set(needs_review)
    )
    status = _reconcile_status(
        incomplete_scopes=incomplete_scopes,
        unverified=unverified,
        unresolved=unresolved_ids,
        needs_review=needs_review,
        resolved=resolved,
    )
    return {
        **base,
        "success": status == "reconciled" and not unresolved,
        "status": status,
        "incomplete_scopes": incomplete_scopes,
        "unverified_row_ids": unverified,
        "needs_review_row_ids": needs_review,
        "resolved_row_ids": resolved,
        "recovered": None
        if recovered is None
        else {
            "stale_intent_withdrawn": recovered[0],
            "claim_deadline_withdrawn": recovered[1],
            "lease_expired_uncertain": recovered[2],
        },
        "unresolved_row_ids": unresolved_ids,
        "rows": [
            {**_row_summary(r), "result": results.get(r["id"])} for r in final_rows
        ],
    }
