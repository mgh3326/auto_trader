"""Guarded Alpaca Paper filled-buy and reconcile smoke (ROB-85).

Default mode is dry-run: preflight reads, preview, and confirm=False validation
only. Execute mode is intentionally narrow and submits exactly one Alpaca Paper
crypto buy-limit order after the same-process guards pass.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.mcp_server.tooling.alpaca_paper import (
    alpaca_paper_get_account,
    alpaca_paper_get_order,
    alpaca_paper_list_fills,
    alpaca_paper_list_orders,
    alpaca_paper_list_positions,
)
from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_submit_order
from app.mcp_server.tooling.alpaca_paper_preview import alpaca_paper_preview_order
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import (
    AlpacaPaperLedgerService,
    ApprovalProvenance,
)
from app.services.crypto_execution_mapping import (
    CryptoExecutionMappingError,
    map_upbit_to_alpaca_paper,
)

ROB85_MAX_NOTIONAL_USD = Decimal("10")
FINAL_ORDER_STATUSES = frozenset(
    {"filled", "partially_filled", "canceled", "rejected", "expired"}
)
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
    }
)


class SmokeStopError(RuntimeError):
    """Raised when a ROB-85 guard fails closed before broker mutation."""


@dataclass(frozen=True)
class SmokePayload:
    signal_symbol: str
    signal_venue: str
    execution_symbol: str
    execution_venue: str
    asset_class: str
    notional: Decimal
    limit_price_usd: Decimal
    time_in_force: str
    client_order_id: str
    order_request: dict[str, Any]
    provenance: ApprovalProvenance


@dataclass(frozen=True)
class PreflightSnapshot:
    account_status: str | None
    buying_power: Decimal
    open_order_count: int
    execution_symbol_open_order_count: int
    position_count: int
    execution_symbol_position_count: int
    recent_fill_count: int


@dataclass(frozen=True)
class ValidationResult:
    preview: dict[str, Any]
    confirm_false: dict[str, Any]
    ledger_row: Any | None = None


@dataclass(frozen=True)
class ReconcileResult:
    client_order_id: str
    broker_order_id: str | None
    order_status: str | None
    lifecycle_state: str
    fill_count: int
    position_present: bool
    position_qty: str | None
    reconcile_status: str
    ledger_row: Any | None = None


PreviewFn = Callable[..., Awaitable[dict[str, Any]]]
SubmitFn = Callable[..., Awaitable[dict[str, Any]]]
ReadFn = Callable[..., Awaitable[dict[str, Any]]]
SleepFn = Callable[[float], Awaitable[None]]


def _parse_decimal(value: Decimal | int | float | str, *, field_name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SmokeStopError(f"{field_name} must be a decimal value") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SmokeStopError(f"{field_name} must be > 0")
    return parsed


def _utc_client_order_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    return f"rob85-fill-{stamp}"


def _compact_order_request(order_request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in order_request.items()
    }


def _extract_order(payload: dict[str, Any]) -> dict[str, Any]:
    order = payload.get("order")
    return order if isinstance(order, dict) else {}


def _symbol_of(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or item.get("asset") or "").upper()


def _symbols_match(broker_symbol: str, execution_symbol: str) -> bool:
    """Match Alpaca crypto symbols that may omit the slash (BTCUSD == BTC/USD)."""
    normalized_broker = broker_symbol.upper()
    normalized_execution = execution_symbol.upper()
    return normalized_broker == normalized_execution or normalized_broker.replace(
        "/", ""
    ) == normalized_execution.replace("/", "")


def _status_of(order: dict[str, Any]) -> str | None:
    status = order.get("status")
    return str(status).lower() if status is not None else None


def _qty_of(position: dict[str, Any] | None) -> str | None:
    if position is None:
        return None
    raw = position.get("qty") or position.get("quantity")
    return str(raw) if raw is not None else None


def _safe_account_status(account_payload: dict[str, Any]) -> str | None:
    account = account_payload.get("account")
    if not isinstance(account, dict):
        return None
    status = account.get("status")
    return str(status).lower() if status is not None else None


def _safe_buying_power(cash_payload: dict[str, Any] | None) -> Decimal:
    cash = (cash_payload or {}).get("cash")
    if not isinstance(cash, dict):
        return Decimal("0")
    raw = cash.get("buying_power") or cash.get("cash") or "0"
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return Decimal("0")


def build_smoke_payload(
    *,
    signal_symbol: str,
    notional: Decimal | int | float | str,
    limit_price_usd: Decimal | int | float | str | None,
    time_in_force: str = "gtc",
    client_order_id: str | None = None,
    now: datetime | None = None,
) -> SmokePayload:
    """Build a bounded crypto buy-limit payload with separated provenance."""
    if limit_price_usd is None:
        raise SmokeStopError("limit_price_usd is required for ROB-85 fill smoke")

    try:
        mapping = map_upbit_to_alpaca_paper(signal_symbol)
    except CryptoExecutionMappingError as exc:
        raise SmokeStopError(str(exc)) from exc
    parsed_notional = _parse_decimal(notional, field_name="notional")
    parsed_limit = _parse_decimal(limit_price_usd, field_name="limit_price_usd")
    if parsed_notional > ROB85_MAX_NOTIONAL_USD:
        raise SmokeStopError(f"notional exceeds ROB-85 max ({ROB85_MAX_NOTIONAL_USD})")
    normalized_tif = (time_in_force or "").strip().lower()
    if normalized_tif not in {"gtc", "ioc"}:
        raise SmokeStopError("time_in_force must be gtc or ioc for crypto")
    coid = (client_order_id or _utc_client_order_id(now)).strip()
    if not coid:
        raise SmokeStopError("client_order_id must not be blank")
    if len(coid) > 48:
        raise SmokeStopError("client_order_id must be <= 48 characters")

    order_request: dict[str, Any] = {
        "symbol": mapping.execution_symbol,
        "side": "buy",
        "type": "limit",
        "notional": parsed_notional,
        "time_in_force": normalized_tif,
        "limit_price": parsed_limit,
        "client_order_id": coid,
        "asset_class": "crypto",
    }
    provenance = ApprovalProvenance(
        signal_symbol=mapping.signal_symbol,
        signal_venue=mapping.signal_venue,
        execution_asset_class=mapping.asset_class,
        workflow_stage="crypto_always_open",
        purpose="paper_plumbing_smoke",
    )
    return SmokePayload(
        signal_symbol=mapping.signal_symbol,
        signal_venue=mapping.signal_venue,
        execution_symbol=mapping.execution_symbol,
        execution_venue=mapping.execution_venue,
        asset_class=mapping.asset_class,
        notional=parsed_notional,
        limit_price_usd=parsed_limit,
        time_in_force=normalized_tif,
        client_order_id=coid,
        order_request=order_request,
        provenance=provenance,
    )


async def collect_preflight_snapshot(
    payload: SmokePayload,
    *,
    get_account_fn: ReadFn = alpaca_paper_get_account,
    get_cash_fn: ReadFn | None = None,
    list_orders_fn: Callable[..., Awaitable[dict[str, Any]]] = alpaca_paper_list_orders,
    list_positions_fn: ReadFn = alpaca_paper_list_positions,
    list_fills_fn: Callable[..., Awaitable[dict[str, Any]]] = alpaca_paper_list_fills,
) -> PreflightSnapshot:
    """Read account/order/position/fill state and summarize without raw ids."""
    if get_cash_fn is None:
        from app.mcp_server.tooling.alpaca_paper import alpaca_paper_get_cash

        get_cash_fn = alpaca_paper_get_cash

    account_payload = await get_account_fn()
    cash_payload = await get_cash_fn()
    orders_payload = await list_orders_fn(status="open", limit=50)
    positions_payload = await list_positions_fn()
    fills_payload = await list_fills_fn(limit=20)

    orders = orders_payload.get("orders") or []
    positions = positions_payload.get("positions") or []
    fills = fills_payload.get("fills") or []
    matching_open_orders = [
        order
        for order in orders
        if isinstance(order, dict)
        and _symbols_match(_symbol_of(order), payload.execution_symbol)
    ]
    matching_positions = [
        position
        for position in positions
        if isinstance(position, dict)
        and _symbols_match(_symbol_of(position), payload.execution_symbol)
    ]
    return PreflightSnapshot(
        account_status=_safe_account_status(account_payload),
        buying_power=_safe_buying_power(cash_payload),
        open_order_count=len(orders),
        execution_symbol_open_order_count=len(matching_open_orders),
        position_count=len(positions),
        execution_symbol_position_count=len(matching_positions),
        recent_fill_count=len(fills),
    )


def validate_preflight(snapshot: PreflightSnapshot, payload: SmokePayload) -> None:
    """Fail closed on attribution or buying-power ambiguity."""
    if snapshot.account_status and snapshot.account_status not in {"active", "ok"}:
        raise SmokeStopError(f"account status is not active: {snapshot.account_status}")
    if snapshot.buying_power < payload.notional:
        raise SmokeStopError("buying_power is below requested notional")
    if snapshot.execution_symbol_open_order_count > 0:
        raise SmokeStopError("open order exists for execution symbol")
    if snapshot.execution_symbol_position_count > 0:
        raise SmokeStopError("existing execution-symbol position blocks ROB-85")


async def validate_preview_and_confirm_false(
    payload: SmokePayload,
    *,
    preview_fn: PreviewFn = alpaca_paper_preview_order,
    submit_fn: SubmitFn = alpaca_paper_submit_order,
    ledger: AlpacaPaperLedgerService | None = None,
) -> ValidationResult:
    """Run mandatory no-mutation preview and confirm=False validation."""
    preview = await preview_fn(**payload.order_request)
    if preview.get("success") is not True or preview.get("preview") is not True:
        raise SmokeStopError("preview did not succeed")
    if preview.get("submitted") is True:
        raise SmokeStopError("preview unexpectedly submitted")
    if preview.get("would_exceed_buying_power") is True:
        raise SmokeStopError("preview would exceed buying power")

    confirm_false = await submit_fn(**payload.order_request, confirm=False)
    if confirm_false.get("submitted") is not False:
        raise SmokeStopError("confirm=False validation unexpectedly submitted")
    if confirm_false.get("blocked_reason") != "confirmation_required":
        raise SmokeStopError("confirm=False did not block with confirmation_required")
    if confirm_false.get("client_order_id") != payload.client_order_id:
        raise SmokeStopError("confirm=False client_order_id mismatch")

    ledger_row = None
    if ledger is not None:
        ledger_row = await ledger.record_preview(
            client_order_id=payload.client_order_id,
            execution_symbol=payload.execution_symbol,
            execution_venue=payload.execution_venue,
            instrument_type=InstrumentType.crypto,
            side="buy",
            order_type="limit",
            time_in_force=payload.time_in_force,
            requested_notional=payload.notional,
            requested_price=payload.limit_price_usd,
            currency="USD",
            preview_payload=_compact_order_request(payload.order_request),
            validation_summary={
                "preview_success": True,
                "confirm_false_blocked_reason": "confirmation_required",
                "would_exceed_buying_power": preview.get("would_exceed_buying_power"),
            },
            provenance=payload.provenance,
            raw_response={"preview": preview, "confirm_false": confirm_false},
        )

    return ValidationResult(
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
    position: dict[str, Any] | None,
    poll_timed_out: bool,
) -> str:
    if poll_timed_out:
        return "open_after_poll_timeout"
    if status == "filled" and fills and position is not None:
        return "filled_position_matched"
    if status == "partially_filled" and position is not None:
        return "partial_fill_position_matched"
    if status in {"rejected", "expired", "canceled"}:
        return "unexpected_state"
    return "unexpected_state"


async def execute_and_reconcile(
    payload: SmokePayload,
    *,
    ledger: AlpacaPaperLedgerService,
    submit_fn: SubmitFn = alpaca_paper_submit_order,
    get_order_fn: Callable[..., Awaitable[dict[str, Any]]] = alpaca_paper_get_order,
    list_fills_fn: Callable[..., Awaitable[dict[str, Any]]] = alpaca_paper_list_fills,
    list_positions_fn: ReadFn = alpaca_paper_list_positions,
    poll_attempts: int = 5,
    poll_sleep_seconds: float = 1.0,
    sleep_fn: SleepFn = asyncio.sleep,
) -> ReconcileResult:
    """Submit exactly one order and reconcile only its returned order id."""
    pre_submit_time = datetime.now(UTC)
    submit = await submit_fn(**payload.order_request, confirm=True)
    if submit.get("submitted") is not True:
        raise SmokeStopError("confirm=True did not submit")
    order = _extract_order(submit)
    broker_order_id = order.get("id") or order.get("order_id")
    if not broker_order_id:
        raise SmokeStopError("submitted order is missing id")
    if submit.get("client_order_id") != payload.client_order_id:
        raise SmokeStopError("submitted client_order_id mismatch")
    if order.get("client_order_id") not in {None, payload.client_order_id}:
        raise SmokeStopError("broker order client_order_id mismatch")

    await ledger.record_submit(payload.client_order_id, order, raw_response=submit)

    final_order = order
    poll_timed_out = False
    attempts = max(1, poll_attempts)
    for attempt in range(attempts):
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
        if attempt == attempts - 1:
            poll_timed_out = True
            break
        await sleep_fn(poll_sleep_seconds)

    fills_payload = await list_fills_fn(after=pre_submit_time.isoformat(), limit=100)
    fills = _filter_fills_for_order(
        fills_payload.get("fills") or [], str(broker_order_id)
    )
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
    position_snapshot_response = {
        "position": position,
        "execution_symbol": payload.execution_symbol,
    }
    await ledger.record_position_snapshot(
        payload.client_order_id, position, raw_response=position_snapshot_response
    )

    status = _status_of(final_order)
    reconcile_status = _derive_reconcile_status(
        status=status,
        fills=fills,
        position=position,
        poll_timed_out=poll_timed_out,
    )
    lifecycle_state = "open" if poll_timed_out else (status or "unexpected")
    ledger_row = await ledger.record_reconcile(
        payload.client_order_id,
        reconcile_status,
        notes=(
            f"order_status={status}; fills={len(fills)}; "
            f"position_present={position is not None}"
        ),
        error_summary=None
        if reconcile_status.endswith("matched")
        else reconcile_status,
        raw_response={
            "final_order": final_order,
            "fills_count": len(fills),
            "position_present": position is not None,
        },
    )
    return ReconcileResult(
        client_order_id=payload.client_order_id,
        broker_order_id=str(broker_order_id),
        order_status=status,
        lifecycle_state=lifecycle_state,
        fill_count=len(fills),
        position_present=position is not None,
        position_qty=_qty_of(position),
        reconcile_status=reconcile_status,
        ledger_row=ledger_row,
    )


def build_report(
    *,
    payload: SmokePayload,
    preflight: PreflightSnapshot,
    validation: ValidationResult | None = None,
    reconcile: ReconcileResult | None = None,
    execute: bool = False,
) -> str:
    """Return an audit summary that excludes account ids and raw payloads."""
    lines = [
        "ROB-85 Alpaca Paper fill/reconcile smoke summary",
        f"mode={'execute' if execute else 'dry-run'}",
        f"signal={payload.signal_venue}:{payload.signal_symbol}",
        f"execution={payload.execution_venue}:{payload.execution_symbol}",
        f"client_order_id={payload.client_order_id}",
        f"notional_usd={payload.notional}",
        f"limit_price_usd={payload.limit_price_usd}",
        (
            "preflight="
            f"account_status={preflight.account_status or 'unknown'}, "
            f"buying_power_sufficient={preflight.buying_power >= payload.notional}, "
            f"open_orders={preflight.open_order_count}, "
            "execution_symbol_open_orders="
            f"{preflight.execution_symbol_open_order_count}, "
            f"positions={preflight.position_count}, "
            "execution_symbol_positions="
            f"{preflight.execution_symbol_position_count}, "
            f"recent_fills={preflight.recent_fill_count}"
        ),
    ]
    if validation is not None:
        lines.append(
            "validation=preview_success=True, confirm_false=confirmation_required"
        )
    if reconcile is not None:
        lines.append(
            "reconcile="
            f"order_status={reconcile.order_status}, "
            f"fill_count={reconcile.fill_count}, "
            f"position_present={reconcile.position_present}, "
            f"position_qty={reconcile.position_qty}, "
            f"reconcile_status={reconcile.reconcile_status}"
        )
    lines.append(
        "side_effects=no live/generic/KIS/Upbit/bulk/watch/order-intent/"
        "scheduler/direct-SQL side effects"
    )
    lines.append("secrets=not printed; account identifiers omitted")
    lines.append("sell_close=out_of_scope")
    return "\n".join(lines)


async def run_smoke(
    *,
    payload: SmokePayload,
    execute: bool = False,
    record_preview: bool = False,
    ledger: AlpacaPaperLedgerService | None = None,
) -> tuple[str, ReconcileResult | None]:
    preflight = await collect_preflight_snapshot(payload)
    validate_preflight(preflight, payload)
    if execute or record_preview:
        if ledger is None:
            raise SmokeStopError("ledger is required for record-preview/execute mode")
    validation = await validate_preview_and_confirm_false(
        payload, ledger=ledger if (execute or record_preview) else None
    )
    reconcile = None
    if execute:
        if ledger is None:
            raise SmokeStopError("ledger is required for execute mode")
        reconcile = await execute_and_reconcile(payload, ledger=ledger)
    return (
        build_report(
            payload=payload,
            preflight=preflight,
            validation=validation,
            reconcile=reconcile,
            execute=execute,
        ),
        reconcile,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-symbol", default="KRW-BTC")
    parser.add_argument("--notional", default="10")
    parser.add_argument("--limit-price-usd", required=True)
    parser.add_argument("--time-in-force", default="gtc", choices=("gtc", "ioc"))
    parser.add_argument("--client-order-id")
    parser.add_argument("--record-preview", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


async def _main_async(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = build_smoke_payload(
        signal_symbol=args.signal_symbol,
        notional=args.notional,
        limit_price_usd=args.limit_price_usd,
        time_in_force=args.time_in_force,
        client_order_id=args.client_order_id,
    )
    ledger = None
    if args.execute or args.record_preview:
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            ledger = AlpacaPaperLedgerService(session)
            report, _ = await run_smoke(
                payload=payload,
                execute=args.execute,
                record_preview=args.record_preview,
                ledger=ledger,
            )
            print(report)
            return 0
    report, _ = await run_smoke(payload=payload)
    print(report)
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_main_async()))
    except SmokeStopError as exc:
        print(f"ROB-85 smoke stopped: {exc}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
