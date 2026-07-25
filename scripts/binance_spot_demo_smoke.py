"""ROB-298 — Binance Spot Demo Mode smoke CLI (default-disabled, 5 modes).

Parallel to ``scripts.binance_testnet_scalper_smoke`` but targets the
Spot Demo endpoint (``https://demo-api.binance.com``).

Five operating modes (mutually exclusive; default exits with guidance):

  1. **default-disabled** — env unset/false: prints one disabled line,
     exits 0, zero HTTP / DB / ledger writes.
  2. ``--plan-only`` — print a JSON plan; no HTTP, no DB, no signing.
  3. ``--preflight`` — signed ``GET /api/v3/account``; redacted summary.
  4. ``--order-test`` — signed ``POST /api/v3/order/test``; non-mutating
     server-side validation of the order shape.
  5. ``--confirm`` — full BUY + close (SELL/CANCEL) round-trip with
     ledger lifecycle writes (planned → previewed → validated →
     submitted → filled → closed → reconciled). Operator-gated.

Hard invariants:
  * Default-disabled — exit 0 with a single log line + zero side effects.
  * Host allowlist enforced at transport layer (``demo-api.binance.com``
    only); testnet / live hosts refused even if env is misconfigured.
  * Per-call operator gate on submit/cancel: ``confirm=True`` only
    routed for the ``--confirm`` mode.
  * Secret hygiene: api_key / api_secret never appear in any printed
    line; only fingerprints and redacted broker payloads are emitted.
  * No scheduler activation; this CLI is the only Spot Demo entry point
    that places real Demo orders.

Exit codes:
  0 — clean run (or default-disabled exit, or reconciled-clean confirm).
  1 — operator misconfiguration (missing env, missing credentials,
      sizing blocked, conflicting close option).
  2 — runtime failure (HTTP error, server auth rejection, anomaly /
      reconciliation drift).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
import uuid
from decimal import Decimal
from typing import Any

import httpx

from app.services.brokers.binance.spot_demo import (
    BinanceSpotDemoDisabled,
    BinanceSpotDemoExecutionClient,
    BinanceSpotDemoMissingCredentials,
    BinanceSpotDemoUnsupportedAuth,
    SpotDemoPreflightClient,
    plan_spot_demo_order,
)
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoOrderSubmitResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    SpotDemoDryRunResult,
)
from app.services.brokers.binance.spot_demo.sizing import (
    CloseQtyDust,
    CloseQtyResult,
    SizingBlocked,
    SizingResult,
    classify_close_residual,
    compute_close_qty,
    compute_demo_order_qty,
)

logger = logging.getLogger("scripts.binance_spot_demo_smoke")

_DEFAULT_BASE_URL = "https://demo-api.binance.com"
_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
_PRICE_PATH = "/api/v3/ticker/price"
_CID_PREFIX = "rob298-"
_GLOBAL_OPEN_ROOT_CAP = 1


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _new_cid() -> str:
    """Generate a traceable client_order_id (``rob298-<uuid4hex[:24]>``)."""
    # Total length 7 + 24 = 31, comfortably under Binance's 36-char cap.
    return f"{_CID_PREFIX}{uuid.uuid4().hex[:24]}"


def _evidence(payload: dict[str, Any]) -> None:
    """Stdout-stream a single source-labeled evidence JSON line."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _trace(line: str) -> None:
    """Print a one-line, machine-greppable evidence row tagged ``[rob-298]``."""
    print(f"[rob-298] {line}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-298 Binance Spot Demo smoke. Default behavior is "
            "disabled (zero side effects). Set BINANCE_SPOT_DEMO_ENABLED=true "
            "+ credentials to opt in. Five modes (mutually exclusive): "
            "--plan-only / --preflight / --order-test / --confirm (and the "
            "no-flag default which prints guidance)."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--plan-only",
        dest="plan_only",
        action="store_true",
        help=(
            "Emit a source-labeled planned-order template without any "
            "HTTP. Safe to run with no credentials when the env gate is on."
        ),
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run a read-only GET /api/v3/account preflight against the "
            "Spot Demo endpoint. Requires env gate + credentials."
        ),
    )
    mode.add_argument(
        "--order-test",
        dest="order_test",
        action="store_true",
        help=(
            "Run a signed POST /api/v3/order/test (server-side validation, "
            "non-mutating). Requires env gate + credentials."
        ),
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Operator gate: dispatch real Demo orders. ROB-298 authorizes "
            "Demo only. Submits a BUY then closes per --close-with; writes "
            "the full ledger lifecycle."
        ),
    )
    parser.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Symbol for the planned/confirmed order (default: BTCUSDT).",
    )
    parser.add_argument(
        "--side",
        choices=["BUY", "SELL"],
        default="BUY",
        help="Initial side for --confirm (default: BUY).",
    )
    parser.add_argument(
        "--cap-usdt",
        dest="cap_usdt",
        type=Decimal,
        default=Decimal("10"),
        help="Per-order notional cap in USDT (default: 10).",
    )
    parser.add_argument(
        "--close-with",
        dest="close_with",
        choices=["SELL", "CANCEL"],
        default="SELL",
        help=(
            "How to close the position after a confirmed BUY. SELL = market "
            "sell back; CANCEL only valid for LIMIT (default: SELL)."
        ),
    )
    parser.add_argument(
        "--order-type",
        dest="order_type",
        choices=["MARKET", "LIMIT"],
        default="MARKET",
        help="Order type. CANCEL close-mode requires LIMIT (default: MARKET).",
    )
    # Plan-only-specific knobs (kept for backwards compat with existing
    # plan_spot_demo_order tests/runbook).
    parser.add_argument(
        "--quantity",
        type=Decimal,
        default=Decimal("0.0001"),
        help="Quantity for the plan-only template (default: 0.0001).",
    )
    parser.add_argument(
        "--price",
        type=Decimal,
        default=None,
        help="Price for LIMIT orders. Omit for MARKET.",
    )
    parser.add_argument(
        "--max-notional-usdt",
        dest="max_notional_usdt",
        type=Decimal,
        default=None,
        help=(
            "Override the per-order notional cap for --plan-only. Default "
            "reads BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT (default 10)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    # Kept for backwards compat — older invocations pass this as a no-op.
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def _resolve_notional_cap(arg_value: Decimal | None) -> Decimal:
    if arg_value is not None:
        return arg_value
    raw = os.environ.get("BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT", "10")
    try:
        return Decimal(raw)
    except Exception:
        logger.warning(
            "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT=%r is not a valid Decimal; "
            "falling back to 10",
            raw,
        )
        return Decimal("10")


# ---------------------------------------------------------------------------
# Mode: --plan-only — zero HTTP, zero signing, zero DB.
# ---------------------------------------------------------------------------
async def _run_plan_only(args: argparse.Namespace) -> int:
    cap = _resolve_notional_cap(args.max_notional_usdt)
    plan = plan_spot_demo_order(
        symbol=args.symbol,
        side=args.side,
        order_type=args.order_type,
        quantity=args.quantity,
        price=args.price,
        notional_cap_usdt=cap,
    )
    _evidence({"event": "spot_demo_plan", "plan": plan.to_evidence_dict()})
    return 0


# ---------------------------------------------------------------------------
# Mode: --preflight — signed GET /api/v3/account, redacted summary.
# ---------------------------------------------------------------------------
async def _run_preflight(args: argparse.Namespace) -> int:
    try:
        client = SpotDemoPreflightClient.from_env()
    except BinanceSpotDemoMissingCredentials as exc:
        logger.error("preflight refused: %s", exc)
        return 1
    try:
        result = await client.preflight_account()
    finally:
        await client.aclose()
    _evidence({"event": "spot_demo_preflight", "preflight": result.to_evidence_dict()})
    return 0


# ---------------------------------------------------------------------------
# Mode: --order-test — signed POST /api/v3/order/test.
# ---------------------------------------------------------------------------
async def _run_order_test(args: argparse.Namespace) -> int:
    try:
        execution = BinanceSpotDemoExecutionClient.from_env()
    except BinanceSpotDemoMissingCredentials as exc:
        logger.error("order_test refused: %s", exc)
        return 1
    base_url = os.environ.get("BINANCE_SPOT_DEMO_BASE_URL", _DEFAULT_BASE_URL)
    try:
        # Look up live filters + a reference price so qty respects step + cap.
        filters = await _fetch_symbol_filters(base_url, args.symbol)
        ref_price = await _fetch_reference_price(base_url, args.symbol)
        sizing = compute_demo_order_qty(
            target_notional_usdt=args.cap_usdt,
            price=ref_price,
            min_notional=filters["min_notional"],
            step_size=filters["step_size"],
            cap_usdt=args.cap_usdt,
        )
        if isinstance(sizing, SizingBlocked):
            logger.error("order_test sizing blocked: %s", sizing.reason)
            return 1
        # MARKET for --order-test; price is omitted regardless of --order-type.
        result = await execution.order_test(
            symbol=args.symbol,
            side=args.side,
            order_type="MARKET",
            qty=sizing.qty,
        )
        _trace(
            f"order_test_ok symbol={result.symbol} side={result.side} qty={result.qty}"
        )
        _evidence(
            {
                "event": "spot_demo_order_test",
                "symbol": result.symbol,
                "side": result.side,
                "order_type": result.order_type,
                "qty": str(result.qty),
                "reference_price": str(ref_price),
                "min_notional": str(filters["min_notional"]),
                "step_size": str(filters["step_size"]),
            }
        )
        return 0
    finally:
        await execution.aclose()


# ---------------------------------------------------------------------------
# Mode: --confirm — full lifecycle.
# ---------------------------------------------------------------------------
async def _run_confirm(args: argparse.Namespace) -> int:
    # Validate option compatibility first.
    if args.close_with == "CANCEL" and args.order_type != "LIMIT":
        logger.error(
            "--close-with CANCEL requires --order-type LIMIT (received %s)",
            args.order_type,
        )
        return 1
    if args.side != "BUY":
        # The plan calls for BUY + close; SELL-initiated flows aren't wired.
        logger.error(
            "--confirm with --side SELL is not wired by this smoke CLI; pass "
            "--side BUY (you can close with SELL/CANCEL after)."
        )
        return 1
    if args.order_type == "LIMIT" and args.price is None:
        logger.error("--order-type LIMIT requires --price")
        return 1

    try:
        execution = BinanceSpotDemoExecutionClient.from_env()
    except BinanceSpotDemoMissingCredentials as exc:
        logger.error("confirm refused: %s", exc)
        return 1
    base_url = os.environ.get("BINANCE_SPOT_DEMO_BASE_URL", _DEFAULT_BASE_URL)
    venue_host = httpx.URL(base_url).host

    # Deferred DB import so default-disabled exit imports zero DB code.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo.ledger.service import (
        BinanceDemoLedgerService,
    )

    report = {
        "deployed_sha": _deployed_sha(),
        "env_enabled": True,
        "env_credentials_present": True,
        "blockers": [],
    }

    try:
        filters = await _fetch_symbol_filters(base_url, args.symbol)
        ref_price = (
            args.price
            if args.order_type == "LIMIT"
            else await _fetch_reference_price(base_url, args.symbol)
        )
        sizing = compute_demo_order_qty(
            target_notional_usdt=args.cap_usdt,
            price=ref_price,
            min_notional=filters["min_notional"],
            step_size=filters["step_size"],
            cap_usdt=args.cap_usdt,
        )
        if isinstance(sizing, SizingBlocked):
            logger.error("confirm sizing blocked: %s", sizing.reason)
            report["blockers"].append(sizing.reason)
            return 1
        assert isinstance(sizing, SizingResult)

        report["buy_qty"] = str(sizing.qty)

        async with AsyncSessionLocal() as session:
            ledger = BinanceDemoLedgerService(session)
            instrument_id = await ledger.resolve_or_create_instrument(
                venue="binance",
                product="spot",
                venue_symbol=args.symbol,
                base_asset=args.symbol.removesuffix("USDT"),
                quote_asset="USDT",
            )
            buy_cid = _new_cid()
            close_cid = _new_cid()

            return await _execute_confirm_lifecycle(
                execution=execution,
                ledger=ledger,
                session=session,
                venue_host=venue_host,
                instrument_id=instrument_id,
                buy_cid=buy_cid,
                close_cid=close_cid,
                symbol=args.symbol,
                order_type=args.order_type,
                price=args.price,
                qty=sizing.qty,
                notional=sizing.notional_usdt,
                close_with=args.close_with,
                step_size=filters["step_size"],
                min_notional=filters["min_notional"],
                ref_price=ref_price,
                report=report,
            )
    finally:
        _evidence(build_spot_smoke_report(report))
        await execution.aclose()


async def _execute_confirm_lifecycle(
    *,
    execution: BinanceSpotDemoExecutionClient,
    ledger: Any,
    session: Any,
    venue_host: str,
    instrument_id: int,
    buy_cid: str,
    close_cid: str,
    symbol: str,
    order_type: str,
    price: Decimal | None,
    qty: Decimal,
    notional: Decimal,
    close_with: str,
    step_size: Decimal,
    min_notional: Decimal,
    ref_price: Decimal,
    report: dict[str, Any],
) -> int:
    """Run the full planned→reconciled lifecycle. Returns exit code."""
    now = _now_utc()

    metadata = {"source": "rob-298-smoke", "role": "open"}
    credential_fingerprint = getattr(execution, "credential_fingerprint", None)
    if isinstance(credential_fingerprint, str) and credential_fingerprint:
        metadata["credential_fingerprint"] = credential_fingerprint

    # 1. PLANNED — atomically claim the global/per-instrument root slot before
    # validation or broker order submission. The independent transaction makes
    # the claim durable across processes and crashes.
    reservation = await ledger.reserve_root_planned(
        instrument_id=instrument_id,
        product="spot",
        venue_host=venue_host,
        client_order_id=buy_cid,
        side="BUY",
        order_type=order_type,
        qty=qty,
        price=price,
        notional_usdt=notional,
        extra_metadata=metadata,
        global_open_root_cap=_GLOBAL_OPEN_ROOT_CAP,
        now=now,
    )
    if reservation.status != "reserved":
        reason = reservation.reason or "exposure_slot_taken"
        report["blockers"].append(f"exposure_slot_taken:{reason}")
        _trace(f"reservation_blocked cid={buy_cid} reason={reason}")
        logger.error("root reservation blocked before broker order: %s", reason)
        return 1
    _trace(
        f"planned cid={buy_cid} product=spot symbol={symbol} side=BUY "
        f"qty={qty} venue={venue_host}"
    )

    # 2. PREVIEWED — local plan preview (no HTTP).
    preview = execution.preview_submit(
        symbol=symbol,
        side="BUY",
        order_type=order_type,
        qty=qty,
        client_order_id=buy_cid,
    )
    assert isinstance(preview, SpotDemoDryRunResult)
    await ledger.record_previewed(client_order_id=buy_cid, now=_now_utc())
    await session.commit()
    _trace(f"previewed cid={buy_cid}")

    # 3. VALIDATED — POST /api/v3/order/test (no placement).
    try:
        await execution.order_test(
            symbol=symbol,
            side="BUY",
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force="GTC" if order_type == "LIMIT" else None,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"order_test_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=order_test_failed")
        logger.error("order_test failed: %s", exc)
        return 2
    await ledger.record_validated(client_order_id=buy_cid, now=_now_utc())
    await session.commit()
    _trace(f"order_test_ok symbol={symbol}")
    _trace(f"validated cid={buy_cid}")

    # 4. SUBMITTED — signed POST /api/v3/order (real Demo placement).
    try:
        submit_result = await execution.submit_order(
            symbol=symbol,
            side="BUY",
            order_type=order_type,
            qty=qty,
            client_order_id=buy_cid,
            price=price,
            time_in_force="GTC" if order_type == "LIMIT" else None,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        report["buy_status"] = "FAILED"
        report["blockers"].append(f"submit_failed: {exc}")
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"submit_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=submit_failed")
        logger.error("submit failed: %s", exc)
        return 2
    assert isinstance(submit_result, SpotDemoOrderSubmitResult)
    broker_id = submit_result.broker_order_id
    submit_status = submit_result.status
    await ledger.record_submitted(
        client_order_id=buy_cid,
        broker_order_id=broker_id,
        now=_now_utc(),
        extra_metadata_merge={"submit_status": submit_status},
    )
    await session.commit()
    _trace(
        f"submitted cid={buy_cid} broker_order_id={broker_id} status={submit_status}"
    )
    report["buy_status"] = submit_status

    # 5. FILLED — if the server reports FILLED on response, record it.
    # MARKET responses normally fill immediately on Spot Demo; LIMIT may
    # come back NEW. For LIMIT+CANCEL close, we skip FILLED and head
    # straight to cancel below.
    buy_was_filled = submit_status == "FILLED"
    if buy_was_filled:
        await ledger.record_filled(client_order_id=buy_cid, now=_now_utc())
        await session.commit()
        _trace(f"filled cid={buy_cid}")

    # 6. CLOSE — SELL market or CANCEL the LIMIT.
    if close_with == "SELL":
        if not buy_was_filled:
            logger.warning(
                "close_with=SELL but BUY not FILLED (status=%s); proceeding "
                "with SELL but reconciliation may show drift",
                submit_status,
            )
        return await _close_with_sell(
            execution=execution,
            ledger=ledger,
            session=session,
            venue_host=venue_host,
            instrument_id=instrument_id,
            buy_cid=buy_cid,
            close_cid=close_cid,
            symbol=symbol,
            qty=qty,
            notional=notional,
            step_size=step_size,
            min_notional=min_notional,
            ref_price=ref_price,
            report=report,
        )
    # close_with == "CANCEL"
    return await _close_with_cancel(
        execution=execution,
        ledger=ledger,
        session=session,
        buy_cid=buy_cid,
        symbol=symbol,
        step_size=step_size,
        min_notional=min_notional,
        ref_price=ref_price,
        report=report,
    )


async def _close_with_sell(
    *,
    execution: BinanceSpotDemoExecutionClient,
    ledger: Any,
    session: Any,
    venue_host: str,
    instrument_id: int,
    buy_cid: str,
    close_cid: str,
    symbol: str,
    qty: Decimal,
    notional: Decimal,
    step_size: Decimal,
    min_notional: Decimal,
    ref_price: Decimal,
    report: dict[str, Any],
) -> int:
    """Round-trip the position with a MARKET SELL."""
    base_asset = symbol.removesuffix("USDT")
    balance = await execution.get_asset_balance(asset=base_asset)
    close_sizing = compute_close_qty(
        free_balance=balance.free,
        price=ref_price,
        min_notional=min_notional,
        step_size=step_size,
    )
    if isinstance(close_sizing, CloseQtyDust):
        # Nothing sellable at min-notional; the BUY left only dust. Confirm a
        # clean book, then reconcile with a dust note (NOT anomaly).
        report["close_qty"] = "0"
        report["residual_dust_amount"] = str(close_sizing.free)
        report["residual_dust_notional"] = str(close_sizing.notional_usdt)
        await ledger.record_closed(client_order_id=buy_cid, now=_now_utc())
        await session.commit()
        _trace(
            f"close skipped dust base={base_asset} free={close_sizing.free} "
            f"notional={close_sizing.notional_usdt} reason={close_sizing.reason}"
        )
        return await _reconcile(
            execution=execution,
            ledger=ledger,
            session=session,
            buy_cid=buy_cid,
            close_cid=None,
            symbol=symbol,
            sell_was_filled=None,
            dust_note=close_sizing.reason,
            report=report,
            step_size=step_size,
            min_notional=min_notional,
            ref_price=ref_price,
        )
    assert isinstance(close_sizing, CloseQtyResult)
    close_qty = close_sizing.qty
    report["close_qty"] = str(close_qty)

    now = _now_utc()
    await ledger.record_planned(
        instrument_id=instrument_id,
        product="spot",
        venue_host=venue_host,
        client_order_id=close_cid,
        side="SELL",
        order_type="MARKET",
        qty=close_qty,
        price=None,
        notional_usdt=close_sizing.notional_usdt,
        parent_client_order_id=buy_cid,
        extra_metadata={"source": "rob-298-smoke", "role": "close"},
        now=now,
    )
    await session.commit()
    _trace(
        f"planned cid={close_cid} product=spot symbol={symbol} side=SELL "
        f"qty={close_qty} venue={venue_host}"
    )
    await ledger.record_previewed(client_order_id=close_cid, now=_now_utc())
    await ledger.record_validated(client_order_id=close_cid, now=_now_utc())
    await session.commit()
    _trace(f"previewed cid={close_cid}")
    _trace(f"validated cid={close_cid}")

    try:
        sell_result = await execution.submit_order(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            qty=close_qty,
            client_order_id=close_cid,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        report["close_status"] = "FAILED"
        report["blockers"].append(f"sell_submit_failed: {exc}")
        await ledger.record_anomaly(
            client_order_id=close_cid,
            reason=f"sell_submit_failed: {exc}",
            now=_now_utc(),
        )
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"close_sell_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={close_cid} reason=sell_submit_failed")
        logger.error("close sell failed: %s", exc)
        return 2
    assert isinstance(sell_result, SpotDemoOrderSubmitResult)
    sell_status = sell_result.status
    report["close_status"] = sell_status
    await ledger.record_submitted(
        client_order_id=close_cid,
        broker_order_id=sell_result.broker_order_id,
        now=_now_utc(),
        extra_metadata_merge={"submit_status": sell_status},
    )
    await session.commit()
    _trace(
        f"submitted cid={close_cid} broker_order_id={sell_result.broker_order_id} "
        f"status={sell_status}"
    )
    if sell_status == "FILLED":
        await ledger.record_filled(client_order_id=close_cid, now=_now_utc())
        await session.commit()
        _trace(f"filled cid={close_cid}")

    # Close the BUY (round-trip complete).
    await ledger.record_closed(client_order_id=buy_cid, now=_now_utc())
    await session.commit()
    _trace(f"closed cid={buy_cid}")

    return await _reconcile(
        execution=execution,
        ledger=ledger,
        session=session,
        buy_cid=buy_cid,
        close_cid=close_cid,
        symbol=symbol,
        sell_was_filled=sell_status == "FILLED",
        report=report,
        step_size=step_size,
        min_notional=min_notional,
        ref_price=ref_price,
    )


async def _close_with_cancel(
    *,
    execution: BinanceSpotDemoExecutionClient,
    ledger: Any,
    session: Any,
    buy_cid: str,
    symbol: str,
    step_size: Decimal,
    min_notional: Decimal,
    ref_price: Decimal,
    report: dict[str, Any],
) -> int:
    """Cancel an unfilled LIMIT BUY."""
    try:
        cancel_result = await execution.cancel_order(
            symbol=symbol, client_order_id=buy_cid, confirm=True
        )
    except Exception as exc:  # noqa: BLE001
        report["close_status"] = "FAILED"
        report["blockers"].append(f"cancel_failed: {exc}")
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"cancel_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=cancel_failed")
        logger.error("cancel failed: %s", exc)
        return 2
    await ledger.record_cancelled(client_order_id=buy_cid, now=_now_utc())
    await session.commit()
    cancel_status = getattr(cancel_result, "status", "CANCELED")
    report["close_status"] = str(cancel_status)
    _trace(f"cancelled cid={buy_cid} broker_status={cancel_status}")

    return await _reconcile(
        execution=execution,
        ledger=ledger,
        session=session,
        buy_cid=buy_cid,
        close_cid=None,
        symbol=symbol,
        sell_was_filled=None,
        report=report,
        step_size=step_size,
        min_notional=min_notional,
        ref_price=ref_price,
    )


async def _reconcile(
    *,
    execution: BinanceSpotDemoExecutionClient,
    ledger: Any,
    session: Any,
    buy_cid: str,
    close_cid: str | None,
    symbol: str,
    sell_was_filled: bool | None,
    dust_note: str | None = None,
    report: dict[str, Any],
    step_size: Decimal,
    min_notional: Decimal,
    ref_price: Decimal,
) -> int:
    """Verify ``get_open_orders`` is empty and mark BUY reconciled.

    Returns 0 on a clean reconcile, 2 on drift / anomaly.
    """
    try:
        open_orders = await execution.get_open_orders(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        report["reconciliation_status"] = "anomaly"
        report["blockers"].append(f"open_orders_query_failed: {exc}")
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"open_orders_query_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=open_orders_query_failed")
        logger.error("open_orders query failed: %s", exc)
        return 2

    is_empty = not open_orders.orders
    report["open_orders_count"] = len(open_orders.orders)
    _trace(f"open_orders_check empty={'true' if is_empty else 'false'}")
    if not is_empty:
        residual_cids = [o.client_order_id for o in open_orders.orders]
        hint = (
            "Open orders remain after close. Cancel residual open orders, "
            "then re-run --confirm or remediate manually."
        )
        report["reconciliation_status"] = "anomaly"
        report["blockers"].append(f"open_orders_residual: {residual_cids!r}")
        report["remediation_hint"] = hint
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"open_orders_residual: {residual_cids!r}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=open_orders_residual")
        return 2

    base_asset = symbol.removesuffix("USDT")
    balance = await execution.get_asset_balance(asset=base_asset)
    outcome = classify_close_residual(
        free_after=balance.free,
        price=ref_price,
        min_notional=min_notional,
        step_size=step_size,
        open_orders_empty=is_empty,
    )
    if outcome.kind == "anomaly":
        report["reconciliation_status"] = "anomaly"
        report["blockers"].append(outcome.remediation_hint or "residual_after_close")
        report["remediation_hint"] = outcome.remediation_hint
        await ledger.record_anomaly(
            client_order_id=buy_cid,
            reason=f"residual_after_close: {outcome.remediation_hint}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={buy_cid} reason=residual_after_close")
        return 2

    # dust or clean: reconcile with a note recording residual size.
    report["reconciliation_status"] = "dust" if balance.free > 0 else "reconciled"
    report["residual_dust_amount"] = str(balance.free)
    report["residual_dust_notional"] = str(balance.free * ref_price)

    await ledger.record_reconciled(
        client_order_id=buy_cid,
        now=_now_utc(),
        extra_metadata_merge={
            "residual_dust": {
                "asset": base_asset,
                "free": str(balance.free),
                "notional_usdt": str(balance.free * ref_price),
                "note": dust_note or "post-close residual within dust threshold",
            }
        }
        if balance.free > 0
        else None,
    )
    if close_cid is not None and sell_was_filled:
        # Best-effort: also reconcile the close row when its lifecycle
        # reached ``filled``. (Close rows that never reached filled are
        # left in their existing state.)
        try:
            await ledger.record_closed(client_order_id=close_cid, now=_now_utc())
            await ledger.record_reconciled(client_order_id=close_cid, now=_now_utc())
        except Exception as exc:  # noqa: BLE001
            logger.warning("close-row reconcile non-fatal: %s (cid=%s)", exc, close_cid)
    await session.commit()
    _trace(f"reconciled cid={buy_cid}")
    _evidence(
        {
            "event": "spot_demo_confirm_reconciled",
            "buy_client_order_id": buy_cid,
            "close_client_order_id": close_cid,
            "symbol": symbol,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Public-read helpers — used by --order-test and --confirm to pull live
# exchangeInfo filters + a reference price. No signing needed.
# ---------------------------------------------------------------------------
async def _fetch_symbol_filters(base_url: str, symbol: str) -> dict[str, Decimal]:
    """Pull ``LOT_SIZE.stepSize`` + ``NOTIONAL.minNotional`` for ``symbol``.

    Single HTTP GET against the configured Spot Demo base URL; transport
    is a fresh httpx client (no shared secret needed for a public read).
    Modern Binance Spot uses ``filterType == 'NOTIONAL'``; legacy
    deployments use ``'MIN_NOTIONAL'``. Both are accepted.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        resp = await client.get(_EXCHANGE_INFO_PATH, params={"symbol": symbol})
        resp.raise_for_status()
        body = resp.json()
    symbols = body.get("symbols") or []
    if not symbols:
        raise RuntimeError(f"exchangeInfo returned no symbols for {symbol!r}")
    filters = symbols[0].get("filters") or []
    step_size: Decimal | None = None
    min_notional: Decimal | None = None
    for entry in filters:
        ftype = entry.get("filterType")
        if ftype == "LOT_SIZE":
            step_size = Decimal(str(entry.get("stepSize", "0")))
        elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
            mn = entry.get("minNotional") or entry.get("minNotionalValue")
            if mn is not None:
                min_notional = Decimal(str(mn))
    if step_size is None:
        raise RuntimeError(f"no LOT_SIZE filter in exchangeInfo for {symbol!r}")
    if min_notional is None:
        # Fall back to a conservative 5 USDT if the server doesn't expose
        # the filter (some test deployments omit it).
        min_notional = Decimal("5")
    return {"step_size": step_size, "min_notional": min_notional}


async def _fetch_reference_price(base_url: str, symbol: str) -> Decimal:
    """Pull the latest ticker price for ``symbol`` (public read)."""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        resp = await client.get(_PRICE_PATH, params={"symbol": symbol})
        resp.raise_for_status()
        body = resp.json()
    price = body.get("price")
    if price is None:
        raise RuntimeError(f"ticker/price returned no price for {symbol!r}")
    return Decimal(str(price))


# ---------------------------------------------------------------------------
# Instrument resolution (for ledger FK).
# ---------------------------------------------------------------------------
async def _get_or_create_instrument(session: Any, symbol: str) -> int:
    """Find-or-create ``crypto_instruments`` row for ``(binance, spot, symbol)``.

    Returns the row's ``id`` (FK target for the ledger). Base / quote
    assets are inferred for ``*USDT`` symbols (the only ones in scope for
    PR 1's MVP); other suffixes raise.
    """
    from sqlalchemy import select

    from app.models.crypto_instruments import CryptoInstrument

    result = await session.execute(
        select(CryptoInstrument.id).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return int(row)
    # Infer base/quote — MVP scope is *USDT.
    if not symbol.endswith("USDT"):
        raise RuntimeError(
            f"crypto_instruments row missing for binance/spot/{symbol!r} and "
            "I only know how to seed *USDT pairs. Run "
            "scripts.binance_testnet_seed_instruments first."
        )
    base = symbol[: -len("USDT")]
    inst = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=symbol,
        base_asset=base,
        quote_asset="USDT",
        status="active",
    )
    session.add(inst)
    await session.flush()
    await session.refresh(inst)
    return int(inst.id)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _deployed_sha() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEPLOYED_SHA", "unknown")


def build_spot_smoke_report(report: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape the accumulated fields into the final evidence event.

    Contains no secrets — only the operator-facing run summary."""
    dust_amount = report.get("residual_dust_amount")
    residual = (
        {
            "amount": dust_amount,
            "notional_usdt": report.get("residual_dust_notional"),
        }
        if dust_amount is not None
        else None
    )
    return {
        "event": "spot_demo_smoke_report",
        "deployed_sha": report.get("deployed_sha", "unknown"),
        "env_enabled": report.get("env_enabled"),
        "env_credentials_present": report.get("env_credentials_present"),
        "buy_qty": report.get("buy_qty"),
        "buy_status": report.get("buy_status"),
        "close_qty": report.get("close_qty"),
        "close_status": report.get("close_status"),
        "open_orders_count": report.get("open_orders_count"),
        "residual_dust": residual,
        "reconciliation_status": report.get("reconciliation_status"),
        "blockers": list(report.get("blockers", [])),
        "remediation_hint": report.get("remediation_hint"),
    }


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------
async def _run(args: argparse.Namespace) -> int:
    # Hard invariant #1: default-disabled. The gate is checked AFTER
    # argparse so `--help` still works without the env set, but BEFORE
    # any mode dispatch / HTTP / DB.
    if not _truthy(os.environ.get("BINANCE_SPOT_DEMO_ENABLED")):
        logger.info("spot demo disabled — set BINANCE_SPOT_DEMO_ENABLED=true to opt in")
        return 0

    if args.plan_only:
        return await _run_plan_only(args)
    if args.preflight:
        return await _run_preflight(args)
    if args.order_test:
        return await _run_order_test(args)
    if args.confirm:
        return await _run_confirm(args)

    # No mode flag → enabled-no-action guidance.
    logger.info(
        "spot demo enabled but no action requested. Pass --plan-only for a "
        "no-HTTP planning template, --preflight for read-only account, "
        "--order-test for signed shape validation, or --confirm for a full "
        "BUY + close round-trip with ledger lifecycle writes."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except BinanceSpotDemoDisabled as exc:
        logger.error("spot demo disabled: %s", exc)
        return 1
    except BinanceSpotDemoMissingCredentials as exc:
        logger.error("spot demo credentials missing: %s", exc)
        return 1
    except BinanceSpotDemoUnsupportedAuth as exc:
        logger.error("spot demo unsupported auth: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("spot demo smoke failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
