"""Guarded Alpaca Paper sell/reduce/close smoke workflow (ROB-86).

Default mode is dry-run: source/target validation, paper-only preflight reads,
preview, and confirm=False validation only. Execute mode is intentionally narrow
and submits exactly one Alpaca Paper crypto sell-limit order after all guards
pass. This module does not expose or call close-position, liquidate, bulk,
generic broker, KIS, Upbit, watch, order-intent, scheduler, or direct DB
mutation routes.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper import (
    alpaca_paper_get_account,
    alpaca_paper_get_order,
    alpaca_paper_list_fills,
    alpaca_paper_list_orders,
    alpaca_paper_list_positions,
)
from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_submit_order
from app.mcp_server.tooling.alpaca_paper_preview import (
    ALPACA_PAPER_CRYPTO_ALLOWED_SYMBOLS,
    ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD,
    alpaca_paper_preview_order,
)
from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import (
    AlpacaPaperLedgerService,
    ApprovalProvenance,
)

ROB86_EXECUTION_VENUE = "alpaca_paper_crypto"
ROB86_ALLOWED_SOURCE_RECONCILE = frozenset(
    {"filled_position_matched", "partial_fill_position_matched"}
)
ROB86_ALLOWED_SOURCE_LIFECYCLE = frozenset({"filled", "partially_filled"})
OPEN_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "held",
        "done_for_day",
        "pending_cancel",
        "pending_replace",
        "replaced",
        "stopped",
        "calculated",
        "open",
    }
)
FINAL_ORDER_STATUSES = frozenset(
    {"filled", "partially_filled", "canceled", "rejected", "expired"}
)


class SellCloseStopError(RuntimeError):
    """Raised when a ROB-86 guard fails closed before broker mutation."""


@dataclass(frozen=True)
class SourceLedgerSnapshot:
    client_order_id: str
    execution_symbol: str
    execution_venue: str | None
    side: str
    lifecycle_state: str | None
    reconcile_status: str | None
    qty: Decimal
    signal_symbol: str | None = None
    signal_venue: str | None = None
    execution_asset_class: str | None = None


@dataclass(frozen=True)
class SellClosePayload:
    source_client_order_id: str
    signal_symbol: str | None
    signal_venue: str | None
    execution_symbol: str
    execution_venue: str
    asset_class: str
    qty: Decimal
    limit_price_usd: Decimal
    time_in_force: str
    client_order_id: str
    order_request: dict[str, Any]
    source: SourceLedgerSnapshot
    close_intent: str
    provenance: ApprovalProvenance


@dataclass(frozen=True)
class SellClosePreflightSnapshot:
    account_status: str | None
    open_order_count: int
    conflicting_open_sell_order_count: int
    matching_position_count: int
    matching_position_qty: Decimal
    recent_fill_count: int
    close_intent: str


@dataclass(frozen=True)
class SellCloseValidationResult:
    preview: dict[str, Any]
    confirm_false: dict[str, Any]
    ledger_row: Any | None = None


@dataclass(frozen=True)
class SellCloseReconcileResult:
    client_order_id: str
    broker_order_id: str | None
    order_status: str | None
    lifecycle_state: str
    fill_count: int
    post_position_qty: Decimal
    reconcile_status: str
    ledger_row: Any | None = None


ReadFn = Callable[..., Awaitable[dict[str, Any]]]
PreviewFn = Callable[..., Awaitable[dict[str, Any]]]
SubmitFn = Callable[..., Awaitable[dict[str, Any]]]
SourceLookupFn = Callable[[str], Awaitable[Sequence[Any]]]
SleepFn = Callable[[float], Awaitable[None]]


def _parse_decimal(value: Decimal | int | float | str | None, *, field_name: str) -> Decimal:
    if value is None:
        raise SellCloseStopError(f"{field_name} is required")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SellCloseStopError(f"{field_name} must be a decimal value") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SellCloseStopError(f"{field_name} must be > 0")
    return parsed


def _utc_client_order_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    return f"rob86-sell-{stamp}"


def _compact_order_request(order_request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in order_request.items()
    }


def _symbol_of(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or item.get("asset") or "").upper()


def _symbols_match(left: str, right: str) -> bool:
    normalized_left = left.upper()
    normalized_right = right.upper()
    return normalized_left == normalized_right or normalized_left.replace(
        "/", ""
    ) == normalized_right.replace("/", "")


def _status_of(order: dict[str, Any]) -> str | None:
    status = order.get("status")
    return str(status).lower() if status is not None else None


def _extract_order(payload: dict[str, Any]) -> dict[str, Any]:
    order = payload.get("order")
    return order if isinstance(order, dict) else {}


def _decimal_from_item(item: dict[str, Any] | None, *keys: str) -> Decimal:
    if item is None:
        return Decimal("0")
    for key in keys:
        raw = item.get(key)
        if raw is not None:
            try:
                parsed = Decimal(str(raw))
            except InvalidOperation:
                return Decimal("0")
            return parsed if parsed.is_finite() and parsed > 0 else Decimal("0")
    return Decimal("0")


def _safe_account_status(account_payload: dict[str, Any]) -> str | None:
    account = account_payload.get("account")
    if not isinstance(account, dict):
        return None
    status = account.get("status")
    return str(status).lower() if status is not None else None


def _attr(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _normalize_source_rows(rows: Sequence[Any]) -> SourceLedgerSnapshot:
    if len(rows) != 1:
        raise SellCloseStopError("source ledger row count must be exactly 1")
    row = rows[0]
    client_order_id = str(_attr(row, "client_order_id") or "").strip()
    if not client_order_id:
        raise SellCloseStopError("source client_order_id is missing")
    execution_symbol = str(_attr(row, "execution_symbol") or "").upper()
    side = str(_attr(row, "side") or "").lower()
    lifecycle_state = (
        str(_attr(row, "lifecycle_state")).lower()
        if _attr(row, "lifecycle_state") is not None
        else None
    )
    reconcile_status = (
        str(_attr(row, "reconcile_status"))
        if _attr(row, "reconcile_status") is not None
        else None
    )
    qty = _parse_decimal(
        _attr(row, "filled_qty") or _attr(row, "requested_qty"), field_name="source_qty"
    )
    return SourceLedgerSnapshot(
        client_order_id=client_order_id,
        execution_symbol=execution_symbol,
        execution_venue=_attr(row, "execution_venue"),
        side=side,
        lifecycle_state=lifecycle_state,
        reconcile_status=reconcile_status,
        qty=qty,
        signal_symbol=_attr(row, "signal_symbol"),
        signal_venue=_attr(row, "signal_venue"),
        execution_asset_class=_attr(row, "execution_asset_class"),
    )


def _validate_source(source: SourceLedgerSnapshot, *, symbol: str, qty: Decimal) -> None:
    if source.side != "buy":
        raise SellCloseStopError("source ledger row must be a buy")
    if source.lifecycle_state not in ROB86_ALLOWED_SOURCE_LIFECYCLE:
        raise SellCloseStopError("source ledger row must be filled or partially_filled")
    if source.reconcile_status not in ROB86_ALLOWED_SOURCE_RECONCILE:
        raise SellCloseStopError("source ledger row must be position-matched")
    if source.execution_asset_class not in {None, "crypto"}:
        raise SellCloseStopError("source ledger row must be crypto-compatible")
    if not _symbols_match(source.execution_symbol, symbol):
        raise SellCloseStopError("source execution symbol does not match target")
    if qty > source.qty:
        raise SellCloseStopError("sell qty exceeds source filled/requested qty")


def _validate_symbol(symbol: str) -> str:
    normalized = (symbol or "").strip().upper()
    unsafe = {"", "ALL", "BULK", "*", "LIQUIDATE", "CLOSE", "CLOSE_ALL"}
    if normalized in unsafe:
        raise SellCloseStopError("symbol must be one exact Alpaca crypto USD pair")
    if any(token in normalized for token in (",", "..", "\\", "?", "#")):
        raise SellCloseStopError("symbol must not contain bulk/list/path/query syntax")
    if normalized.count("/") != 1:
        raise SellCloseStopError("symbol must be one Alpaca crypto USD pair")
    if normalized not in ALPACA_PAPER_CRYPTO_ALLOWED_SYMBOLS:
        allowed = ", ".join(sorted(ALPACA_PAPER_CRYPTO_ALLOWED_SYMBOLS))
        raise SellCloseStopError(f"symbol must be one of: {allowed}")
    return normalized


def _validate_client_order_id(client_order_id: str) -> str:
    stripped = (client_order_id or "").strip()
    if not stripped:
        raise SellCloseStopError("client_order_id must not be blank")
    if len(stripped) > 48:
        raise SellCloseStopError("client_order_id must be <= 48 characters")
    lowered = stripped.lower()
    if any(token in lowered for token in ("all", "bulk", "liquidate", "close-all")):
        raise SellCloseStopError("client_order_id must not contain bulk/close-all hints")
    return stripped


async def load_source_ledger_rows(source_client_order_id: str) -> list[AlpacaPaperOrderLedger]:
    """Read source ledger rows by exact client_order_id without mutating state."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.client_order_id == source_client_order_id
            )
        )
        return list(result.scalars().all())


async def build_sell_close_payload(
    *,
    source_client_order_id: str,
    symbol: str,
    qty: Decimal | int | float | str,
    limit_price_usd: Decimal | int | float | str,
    client_order_id: str | None = None,
    time_in_force: str = "gtc",
    source_lookup_fn: SourceLookupFn = load_source_ledger_rows,
    now: datetime | None = None,
) -> SellClosePayload:
    """Build a bounded crypto sell-limit payload after exact source validation."""
    source_id = (source_client_order_id or "").strip()
    if not source_id:
        raise SellCloseStopError("source_client_order_id is required")
    if source_id.lower() in {"all", "bulk", "*"}:
        raise SellCloseStopError("source_client_order_id must be exact")
    execution_symbol = _validate_symbol(symbol)
    parsed_qty = _parse_decimal(qty, field_name="qty")
    parsed_limit = _parse_decimal(limit_price_usd, field_name="limit_price_usd")
    if parsed_qty * parsed_limit > ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD:
        raise SellCloseStopError(
            f"estimated notional exceeds ROB-86 max ({ALPACA_PAPER_CRYPTO_MAX_NOTIONAL_USD})"
        )
    normalized_tif = (time_in_force or "").strip().lower()
    if normalized_tif not in {"gtc", "ioc"}:
        raise SellCloseStopError("time_in_force must be gtc or ioc for crypto")
    coid = _validate_client_order_id(client_order_id or _utc_client_order_id(now))
    source = _normalize_source_rows(await source_lookup_fn(source_id))
    if source.client_order_id != source_id:
        raise SellCloseStopError("source client_order_id mismatch")
    _validate_source(source, symbol=execution_symbol, qty=parsed_qty)
    close_intent = "close" if parsed_qty == source.qty else "reduce"
    order_request: dict[str, Any] = {
        "symbol": execution_symbol,
        "side": "sell",
        "type": "limit",
        "qty": parsed_qty,
        "time_in_force": normalized_tif,
        "limit_price": parsed_limit,
        "client_order_id": coid,
        "asset_class": "crypto",
    }
    provenance = ApprovalProvenance(
        signal_symbol=source.signal_symbol,
        signal_venue=source.signal_venue,
        execution_asset_class="crypto",
        workflow_stage="rob86_guarded_sell_close",
        purpose="paper_sell_close_smoke",
    )
    return SellClosePayload(
        source_client_order_id=source_id,
        signal_symbol=source.signal_symbol,
        signal_venue=source.signal_venue,
        execution_symbol=execution_symbol,
        execution_venue=ROB86_EXECUTION_VENUE,
        asset_class="crypto",
        qty=parsed_qty,
        limit_price_usd=parsed_limit,
        time_in_force=normalized_tif,
        client_order_id=coid,
        order_request=order_request,
        source=source,
        close_intent=close_intent,
        provenance=provenance,
    )


async def collect_sell_close_preflight_snapshot(
    payload: SellClosePayload,
    *,
    get_account_fn: ReadFn = alpaca_paper_get_account,
    list_orders_fn: ReadFn = alpaca_paper_list_orders,
    list_positions_fn: ReadFn = alpaca_paper_list_positions,
    list_fills_fn: ReadFn = alpaca_paper_list_fills,
) -> SellClosePreflightSnapshot:
    """Read account/order/position/fill state and summarize without raw ids."""
    account_payload = await get_account_fn()
    orders_payload = await list_orders_fn(status="open", limit=50)
    positions_payload = await list_positions_fn()
    fills_payload = await list_fills_fn(limit=20)
    orders = orders_payload.get("orders") or []
    positions = positions_payload.get("positions") or []
    fills = fills_payload.get("fills") or []
    conflicts = [
        order
        for order in orders
        if isinstance(order, dict)
        and _symbols_match(_symbol_of(order), payload.execution_symbol)
        and str(order.get("side") or "").lower() == "sell"
        and (
            payload.source_client_order_id in str(order.get("client_order_id") or "")
            or str(order.get("client_order_id") or "") == payload.client_order_id
            or _status_of(order) in OPEN_ORDER_STATUSES
        )
    ]
    matching_positions = [
        position
        for position in positions
        if isinstance(position, dict)
        and _symbols_match(_symbol_of(position), payload.execution_symbol)
    ]
    position_qty = _decimal_from_item(
        matching_positions[0] if len(matching_positions) == 1 else None,
        "qty_available",
        "available_qty",
        "qty",
        "quantity",
    )
    close_intent = "close" if position_qty == payload.qty else "reduce"
    return SellClosePreflightSnapshot(
        account_status=_safe_account_status(account_payload),
        open_order_count=len(orders),
        conflicting_open_sell_order_count=len(conflicts),
        matching_position_count=len(matching_positions),
        matching_position_qty=position_qty,
        recent_fill_count=len(fills),
        close_intent=close_intent,
    )


def validate_sell_close_preflight(
    snapshot: SellClosePreflightSnapshot, payload: SellClosePayload
) -> None:
    """Fail closed on account/position/conflict ambiguity before sell submit."""
    if snapshot.account_status and snapshot.account_status not in {"active", "ok"}:
        raise SellCloseStopError(f"account status is not active: {snapshot.account_status}")
    if snapshot.conflicting_open_sell_order_count > 0:
        raise SellCloseStopError("conflicting open sell order exists")
    if snapshot.matching_position_count != 1:
        raise SellCloseStopError("exactly one matching position is required")
    if snapshot.matching_position_qty < payload.qty:
        raise SellCloseStopError("matching position qty is below requested sell qty")


async def validate_sell_close_preview_and_confirm_false(
    payload: SellClosePayload,
    *,
    preview_fn: PreviewFn = alpaca_paper_preview_order,
    submit_fn: SubmitFn = alpaca_paper_submit_order,
    ledger: AlpacaPaperLedgerService | None = None,
) -> SellCloseValidationResult:
    """Run mandatory no-mutation preview and confirm=False validation."""
    preview = await preview_fn(**payload.order_request)
    if preview.get("success") is not True or preview.get("preview") is not True:
        raise SellCloseStopError("preview did not succeed")
    if preview.get("submitted") is True:
        raise SellCloseStopError("preview unexpectedly submitted")

    confirm_false = await submit_fn(**payload.order_request, confirm=False)
    if confirm_false.get("submitted") is not False:
        raise SellCloseStopError("confirm=False validation unexpectedly submitted")
    if confirm_false.get("blocked_reason") != "confirmation_required":
        raise SellCloseStopError("confirm=False did not block with confirmation_required")
    if confirm_false.get("client_order_id") != payload.client_order_id:
        raise SellCloseStopError("confirm=False client_order_id mismatch")

    ledger_row = None
    if ledger is not None:
        ledger_row = await ledger.record_preview(
            client_order_id=payload.client_order_id,
            execution_symbol=payload.execution_symbol,
            execution_venue=payload.execution_venue,
            instrument_type=InstrumentType.crypto,
            side="sell",
            order_type="limit",
            time_in_force=payload.time_in_force,
            requested_qty=payload.qty,
            requested_price=payload.limit_price_usd,
            currency="USD",
            preview_payload=_compact_order_request(payload.order_request),
            validation_summary={
                "preview_success": True,
                "confirm_false_blocked_reason": "confirmation_required",
                "source_client_order_id": payload.source_client_order_id,
                "close_intent": payload.close_intent,
                "execution_venue": payload.execution_venue,
                "signal_venue": payload.signal_venue,
            },
            provenance=payload.provenance,
            raw_response={"preview": preview, "confirm_false": confirm_false},
        )
    return SellCloseValidationResult(
        preview=preview, confirm_false=confirm_false, ledger_row=ledger_row
    )


def _filter_fills_for_order(
    fills: list[Any], broker_order_id: str | None
) -> list[dict[str, Any]]:
    if broker_order_id is None:
        return []
    return [
        fill
        for fill in fills
        if isinstance(fill, dict)
        and str(fill.get("order_id") or fill.get("id") or "") == broker_order_id
    ]


def _derive_reconcile_status(
    *,
    status: str | None,
    fills: list[dict[str, Any]],
    post_position_qty: Decimal,
    requested_qty: Decimal,
    poll_timed_out: bool,
) -> str:
    if poll_timed_out:
        return "open_after_poll_timeout"
    if status == "partially_filled":
        return "partial_sell_position_matched"
    if status == "filled" and fills and post_position_qty == 0:
        return "closed_position_matched"
    if status == "filled" and fills and post_position_qty > 0:
        return "reduced_position_matched"
    if status in {"rejected", "expired", "canceled"}:
        return "unexpected_state"
    return "unexpected_state"


async def execute_sell_close_and_reconcile(
    payload: SellClosePayload,
    *,
    ledger: AlpacaPaperLedgerService,
    submit_fn: SubmitFn = alpaca_paper_submit_order,
    get_order_fn: ReadFn = alpaca_paper_get_order,
    list_fills_fn: ReadFn = alpaca_paper_list_fills,
    list_positions_fn: ReadFn = alpaca_paper_list_positions,
    poll_attempts: int = 5,
    poll_sleep_seconds: float = 1.0,
    sleep_fn: SleepFn = asyncio.sleep,
) -> SellCloseReconcileResult:
    """Submit exactly one sell order and reconcile only its returned order id."""
    pre_submit_time = datetime.now(UTC)
    submit = await submit_fn(**payload.order_request, confirm=True)
    if submit.get("submitted") is not True:
        raise SellCloseStopError("confirm=True did not submit")
    order = _extract_order(submit)
    broker_order_id = order.get("id") or order.get("order_id")
    if not broker_order_id:
        raise SellCloseStopError("submitted order is missing id")
    if submit.get("client_order_id") != payload.client_order_id:
        raise SellCloseStopError("submitted client_order_id mismatch")
    if order.get("client_order_id") not in {None, payload.client_order_id}:
        raise SellCloseStopError("broker order client_order_id mismatch")
    await ledger.record_submit(payload.client_order_id, order, raw_response=submit)

    final_order = order
    poll_timed_out = False
    for attempt in range(max(1, poll_attempts)):
        status_payload = await get_order_fn(str(broker_order_id))
        polled_order = _extract_order(status_payload)
        if polled_order:
            final_order = polled_order
            await ledger.record_status(
                payload.client_order_id, polled_order, raw_response=status_payload
            )
        status = _status_of(final_order)
        if status in FINAL_ORDER_STATUSES:
            break
        if attempt == max(1, poll_attempts) - 1:
            poll_timed_out = True
            break
        await sleep_fn(poll_sleep_seconds)

    fills_payload = await list_fills_fn(after=pre_submit_time.isoformat(), limit=100)
    fills = _filter_fills_for_order(fills_payload.get("fills") or [], str(broker_order_id))
    positions_payload = await list_positions_fn()
    positions = positions_payload.get("positions") or []
    position = next(
        (
            item
            for item in positions
            if isinstance(item, dict)
            and _symbols_match(_symbol_of(item), payload.execution_symbol)
        ),
        None,
    )
    post_position_qty = _decimal_from_item(position, "qty_available", "available_qty", "qty", "quantity")
    await ledger.record_position_snapshot(
        payload.client_order_id,
        position,
        raw_response={
            "position": position,
            "execution_symbol": payload.execution_symbol,
            "source_client_order_id": payload.source_client_order_id,
        },
    )
    status = _status_of(final_order)
    reconcile_status = _derive_reconcile_status(
        status=status,
        fills=fills,
        post_position_qty=post_position_qty,
        requested_qty=payload.qty,
        poll_timed_out=poll_timed_out,
    )
    lifecycle_state = "open" if poll_timed_out else (status or "unexpected")
    ledger_row = await ledger.record_reconcile(
        payload.client_order_id,
        reconcile_status,
        notes=(
            f"order_status={status}; fills={len(fills)}; "
            f"post_position_qty={post_position_qty}; source_client_order_id={payload.source_client_order_id}"
        ),
        error_summary=None
        if reconcile_status in {"closed_position_matched", "reduced_position_matched"}
        else reconcile_status,
        raw_response={
            "final_order": final_order,
            "fills_count": len(fills),
            "post_position_qty": str(post_position_qty),
        },
    )
    return SellCloseReconcileResult(
        client_order_id=payload.client_order_id,
        broker_order_id=str(broker_order_id),
        order_status=status,
        lifecycle_state=lifecycle_state,
        fill_count=len(fills),
        post_position_qty=post_position_qty,
        reconcile_status=reconcile_status,
        ledger_row=ledger_row,
    )


def build_report(
    *,
    payload: SellClosePayload,
    preflight: SellClosePreflightSnapshot,
    validation: SellCloseValidationResult | None = None,
    reconcile: SellCloseReconcileResult | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    return {
        "success": True,
        "workflow": "ROB-86 guarded Alpaca Paper sell/close smoke",
        "dry_run": not execute,
        "execute_requested": execute,
        "account_mode": "alpaca_paper",
        "execution_venue": payload.execution_venue,
        "signal_venue": payload.signal_venue,
        "source_client_order_id": payload.source_client_order_id,
        "client_order_id": payload.client_order_id,
        "execution_symbol": payload.execution_symbol,
        "qty": str(payload.qty),
        "limit_price_usd": str(payload.limit_price_usd),
        "close_intent": preflight.close_intent,
        "preflight": {
            "account_status": preflight.account_status,
            "open_order_count": preflight.open_order_count,
            "conflicting_open_sell_order_count": preflight.conflicting_open_sell_order_count,
            "matching_position_count": preflight.matching_position_count,
            "matching_position_qty": str(preflight.matching_position_qty),
            "recent_fill_count": preflight.recent_fill_count,
        },
        "preview_ok": validation is not None and validation.preview.get("success") is True,
        "confirm_false_ok": validation is not None
        and validation.confirm_false.get("submitted") is False,
        "submitted": reconcile is not None,
        "reconcile_status": reconcile.reconcile_status if reconcile else None,
        "forbidden_surfaces": "no close-position/liquidate/bulk/generic/live/KIS/Upbit/watch/order-intent/scheduler/direct-DB route used",
    }


async def run_sell_close_smoke(
    *,
    source_client_order_id: str,
    symbol: str,
    qty: Decimal | int | float | str,
    limit_price_usd: Decimal | int | float | str,
    client_order_id: str | None = None,
    time_in_force: str = "gtc",
    execute: bool = False,
    source_lookup_fn: SourceLookupFn = load_source_ledger_rows,
) -> dict[str, Any]:
    payload = await build_sell_close_payload(
        source_client_order_id=source_client_order_id,
        symbol=symbol,
        qty=qty,
        limit_price_usd=limit_price_usd,
        client_order_id=client_order_id,
        time_in_force=time_in_force,
        source_lookup_fn=source_lookup_fn,
    )
    preflight = await collect_sell_close_preflight_snapshot(payload)
    validate_sell_close_preflight(preflight, payload)
    async with AsyncSessionLocal() as db:
        ledger = AlpacaPaperLedgerService(db)
        validation = await validate_sell_close_preview_and_confirm_false(
            payload, ledger=ledger
        )
        reconcile = None
        if execute:
            reconcile = await execute_sell_close_and_reconcile(payload, ledger=ledger)
    return build_report(
        payload=payload,
        preflight=preflight,
        validation=validation,
        reconcile=reconcile,
        execute=execute,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-client-order-id", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--qty", required=True)
    parser.add_argument("--limit-price", required=True, dest="limit_price_usd")
    parser.add_argument("--client-order-id")
    parser.add_argument("--time-in-force", default="gtc")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="submit exactly one Alpaca Paper sell order with confirm=True after all guards pass",
    )
    return parser


async def _main_async(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = await run_sell_close_smoke(
        source_client_order_id=args.source_client_order_id,
        symbol=args.symbol,
        qty=args.qty,
        limit_price_usd=args.limit_price_usd,
        client_order_id=args.client_order_id,
        time_in_force=args.time_in_force,
        execute=args.execute,
    )
    import json

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
