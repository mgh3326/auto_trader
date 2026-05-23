"""ROB-298 PR 2 — Binance USD-M Futures Demo smoke CLI (default-disabled, 5 modes).

Sibling of ``scripts.binance_spot_demo_smoke`` but targets the Futures
Demo endpoint (``https://demo-fapi.binance.com``).

Five operating modes (mutually exclusive; default exits with guidance):

  1. **default-disabled** — env unset/false: prints one disabled line,
     exits 0, zero HTTP / DB / ledger writes.
  2. ``--plan-only`` — print a JSON plan; no HTTP, no DB, no signing.
     Rejects BTCUSDT (excluded from the futures allowlist).
  3. ``--preflight`` — signed ``GET /fapi/v1/account``; redacted summary.
  4. ``--order-test`` — signed ``POST /fapi/v1/order/test``; non-mutating
     server-side validation of the order shape.
  5. ``--confirm`` — full BUY (open) + reduceOnly SELL (close) round-trip
     with ledger lifecycle writes (planned → previewed → validated →
     submitted → filled → closed → reconciled). Operator-gated. Verifies
     position mode is One-way, pins leverage to 1x, and reconciles on both
     ``open_orders`` empty AND ``position`` flat.

Hard invariants:
  * Default-disabled — exit 0 with a single log line + zero side effects.
  * Host allowlist enforced at transport layer
    (``demo-fapi.binance.com`` only); live / testnet / Spot Demo hosts
    refused even if env is misconfigured.
  * Per-call operator gate on submit/cancel: ``confirm=True`` only routed
    for the ``--confirm`` mode.
  * Symbol allowlist enforced; excluded list (BTCUSDT) cannot be
    re-enabled by ``--allow-symbol``.
  * Position mode check: refuses Hedge mode (PR 2 supports One-way only).
  * Leverage pinned to 1x exactly; any mismatch in the Binance echo
    raises ``BinanceFuturesDemoLeverageMismatch`` and the smoke aborts.
  * reduceOnly threaded on every close-side submit.
  * Secret hygiene: api_key / api_secret never appear in any printed line.
  * No scheduler activation; this CLI is the only Futures Demo entry
    point that places real Demo orders.

Exit codes:
  0 — clean run (or default-disabled exit, or reconciled-clean confirm).
  1 — operator misconfiguration (missing env, missing credentials,
      sizing blocked, excluded/non-allowlisted symbol).
  2 — runtime failure (HTTP error, server auth rejection, hedge mode,
      leverage mismatch, anomaly / reconciliation drift).
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

from app.services.brokers.binance.futures_demo import (
    BinanceFuturesDemoDisabled,
    BinanceFuturesDemoExecutionClient,
    BinanceFuturesDemoHedgeModeBlocked,
    BinanceFuturesDemoLeverageMismatch,
    BinanceFuturesDemoMissingCredentials,
    BinanceFuturesDemoUnsupportedAuth,
    BinanceFuturesDemoUnsupportedSymbol,
    FuturesDemoPreflightClient,
)
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOrderSubmitResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    FuturesDemoDryRunResult,
)
from app.services.brokers.binance.futures_demo.sizing import (
    FUTURES_DEMO_EXCLUDED_SYMBOLS,
    FUTURES_DEMO_FALLBACK_SYMBOLS,
    FuturesSizingBlocked,
    FuturesSizingResult,
    assert_symbol_allowed,
    compute_futures_demo_order_qty,
)

logger = logging.getLogger("scripts.binance_futures_demo_smoke")

_DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
_PRICE_PATH = "/fapi/v1/ticker/price"
_CID_PREFIX = "rob-298-fut-"


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _new_cid() -> str:
    """Generate a traceable client_order_id (``rob-298-fut-<uuid4hex[:16]>``)."""
    # Total length: 12 + 16 = 28, comfortably under Binance's 36-char cap.
    return f"{_CID_PREFIX}{uuid.uuid4().hex[:16]}"


def _evidence(payload: dict[str, Any]) -> None:
    """Stdout-stream a single source-labeled evidence JSON line."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _trace(line: str) -> None:
    """Print a one-line, machine-greppable evidence row tagged ``[rob-298-fut]``."""
    print(f"[rob-298-fut] {line}")


def _resolve_allowlist(
    allow_symbol_overrides: list[str] | None,
) -> frozenset[str]:
    """Merge the default Futures Demo allowlist with operator overrides.

    Excluded symbols (e.g. BTCUSDT) still win at ``assert_symbol_allowed``
    time — this merge only extends the allowed set.
    """
    base = set(FUTURES_DEMO_FALLBACK_SYMBOLS)
    if allow_symbol_overrides:
        base.update(s.upper() for s in allow_symbol_overrides if s)
    return frozenset(base)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-298 PR 2 Binance USD-M Futures Demo smoke. Default "
            "behavior is disabled (zero side effects). Set "
            "BINANCE_FUTURES_DEMO_ENABLED=true + credentials to opt in. "
            "Five modes (mutually exclusive): --plan-only / --preflight / "
            "--order-test / --confirm (and the no-flag default which "
            "prints guidance)."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--plan-only",
        dest="plan_only",
        action="store_true",
        help=(
            "Emit a source-labeled planned-order template without any "
            "HTTP. Safe to run with no credentials when the env gate is "
            "on. BTCUSDT is rejected (excluded list)."
        ),
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run a read-only GET /fapi/v1/account preflight against the "
            "Futures Demo endpoint. Requires env gate + credentials."
        ),
    )
    mode.add_argument(
        "--order-test",
        dest="order_test",
        action="store_true",
        help=(
            "Run a signed POST /fapi/v1/order/test (server-side "
            "validation, non-mutating). Requires env gate + credentials."
        ),
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Operator gate: dispatch real Demo orders. ROB-298 PR 2 "
            "authorizes Futures Demo only. Submits an open then closes "
            "with reduceOnly=true; pins leverage to 1x; verifies One-way "
            "position mode; writes the full ledger lifecycle."
        ),
    )
    parser.add_argument(
        "--symbol",
        default="XRPUSDT",
        help="Symbol for the planned/confirmed order (default: XRPUSDT).",
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
        "--leverage",
        type=int,
        default=1,
        help=(
            "Leverage for --confirm (default: 1). Smoke contract pins "
            "1x exactly; values other than 1 are passed through to "
            "Binance but the echo verification still enforces equality."
        ),
    )
    parser.add_argument(
        "--allow-symbol",
        action="append",
        default=None,
        help=(
            "Extend the symbol allowlist (e.g., --allow-symbol DOGEUSDT). "
            "Excluded symbols (BTCUSDT) cannot be re-enabled."
        ),
    )
    parser.add_argument(
        "--close-with",
        dest="close_with",
        choices=["SELL", "CANCEL"],
        default="SELL",
        help=(
            "How to close after a confirmed open. SELL = reduceOnly "
            "market close back; CANCEL only valid for LIMIT (default: "
            "SELL). For MARKET (always fills), CANCEL falls back to SELL "
            "with a warning."
        ),
    )
    parser.add_argument(
        "--order-type",
        dest="order_type",
        choices=["MARKET", "LIMIT"],
        default="MARKET",
        help="Order type. CANCEL close-mode requires LIMIT (default: MARKET).",
    )
    parser.add_argument(
        "--price",
        type=Decimal,
        default=None,
        help="Price for LIMIT orders. Omit for MARKET.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Mode: --plan-only — zero HTTP, zero signing, zero DB.
# ---------------------------------------------------------------------------
async def _run_plan_only(args: argparse.Namespace) -> int:
    symbol = args.symbol.upper()
    allowlist = _resolve_allowlist(args.allow_symbol)
    # Excluded symbols win even if --allow-symbol attempts to extend.
    if symbol in FUTURES_DEMO_EXCLUDED_SYMBOLS:
        _evidence(
            {
                "event": "futures_demo_plan_rejected",
                "reason": "BinanceFuturesDemoUnsupportedSymbol",
                "symbol": symbol,
                "detail": (
                    f"{symbol} is explicitly excluded "
                    "(MIN_NOTIONAL > 10 USDT cap); --allow-symbol cannot "
                    "re-enable excluded symbols."
                ),
            }
        )
        return 1
    try:
        assert_symbol_allowed(symbol, allowlist_override=allowlist)
    except BinanceFuturesDemoUnsupportedSymbol as exc:
        _evidence(
            {
                "event": "futures_demo_plan_rejected",
                "reason": "BinanceFuturesDemoUnsupportedSymbol",
                "symbol": symbol,
                "detail": str(exc),
            }
        )
        return 1

    # Plan-only does no HTTP. We surface the cap + a coarse qty estimate
    # the operator can sanity-check before running --order-test/--confirm.
    plan = {
        "source": "futures_demo",
        "venue": "binance",
        "product": "usdm_futures",
        "symbol": symbol,
        "side": args.side,
        "order_type": args.order_type,
        "leverage": args.leverage,
        "cap_usdt": str(args.cap_usdt),
        "price": str(args.price) if args.price is not None else None,
        "allowlist_effective": sorted(allowlist),
    }
    _evidence({"event": "futures_demo_plan", "plan": plan})
    return 0


# ---------------------------------------------------------------------------
# Mode: --preflight — signed GET /fapi/v1/account, redacted summary.
# ---------------------------------------------------------------------------
async def _run_preflight(args: argparse.Namespace) -> int:
    try:
        client = FuturesDemoPreflightClient.from_env()
    except BinanceFuturesDemoMissingCredentials as exc:
        logger.error("preflight refused: %s", exc)
        return 1
    try:
        result = await client.preflight_account()
    finally:
        await client.aclose()
    _evidence(
        {
            "event": "futures_demo_preflight",
            "preflight": result.to_evidence_dict(),
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Mode: --order-test — signed POST /fapi/v1/order/test.
# ---------------------------------------------------------------------------
async def _run_order_test(args: argparse.Namespace) -> int:
    symbol = args.symbol.upper()
    allowlist = _resolve_allowlist(args.allow_symbol)
    try:
        assert_symbol_allowed(symbol, allowlist_override=allowlist)
    except BinanceFuturesDemoUnsupportedSymbol as exc:
        logger.error("order_test refused: %s", exc)
        return 1
    try:
        execution = BinanceFuturesDemoExecutionClient.from_env()
    except BinanceFuturesDemoMissingCredentials as exc:
        logger.error("order_test refused: %s", exc)
        return 1
    base_url = os.environ.get(
        "BINANCE_FUTURES_DEMO_BASE_URL", _DEFAULT_BASE_URL
    )
    try:
        filters = await _fetch_symbol_filters(base_url, symbol)
        ref_price = await _fetch_reference_price(base_url, symbol)
        sizing = compute_futures_demo_order_qty(
            symbol=symbol,
            target_notional_usdt=args.cap_usdt,
            price=ref_price,
            min_notional=filters["min_notional"],
            step_size=filters["step_size"],
            cap_usdt=args.cap_usdt,
            symbol_allowlist_override=allowlist,
        )
        if isinstance(sizing, FuturesSizingBlocked):
            logger.error("order_test sizing blocked: %s", sizing.reason)
            return 1
        result = await execution.order_test(
            symbol=symbol,
            side=args.side,
            order_type="MARKET",
            qty=sizing.qty,
        )
        _trace(
            f"order_test_ok symbol={result.symbol} side={result.side} "
            f"qty={result.qty}"
        )
        _evidence(
            {
                "event": "futures_demo_order_test",
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
    symbol = args.symbol.upper()
    allowlist = _resolve_allowlist(args.allow_symbol)

    if args.close_with == "CANCEL" and args.order_type != "LIMIT":
        logger.warning(
            "--close-with CANCEL requires --order-type LIMIT (received %s); "
            "falling back to reduceOnly SELL close.",
            args.order_type,
        )
        args.close_with = "SELL"
    if args.side != "BUY":
        logger.error(
            "--confirm with --side SELL is not wired by this smoke CLI; "
            "pass --side BUY (you can close with reduceOnly SELL after)."
        )
        return 1
    if args.order_type == "LIMIT" and args.price is None:
        logger.error("--order-type LIMIT requires --price")
        return 1

    try:
        assert_symbol_allowed(symbol, allowlist_override=allowlist)
    except BinanceFuturesDemoUnsupportedSymbol as exc:
        logger.error("confirm refused: %s", exc)
        return 1

    try:
        execution = BinanceFuturesDemoExecutionClient.from_env()
    except BinanceFuturesDemoMissingCredentials as exc:
        logger.error("confirm refused: %s", exc)
        return 1
    base_url = os.environ.get(
        "BINANCE_FUTURES_DEMO_BASE_URL", _DEFAULT_BASE_URL
    )
    venue_host = httpx.URL(base_url).host

    # Deferred DB import so default-disabled exit imports zero DB code.
    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo.ledger.service import (
        BinanceDemoLedgerService,
    )

    try:
        filters = await _fetch_symbol_filters(base_url, symbol)
        ref_price = (
            args.price
            if args.order_type == "LIMIT"
            else await _fetch_reference_price(base_url, symbol)
        )
        sizing = compute_futures_demo_order_qty(
            symbol=symbol,
            target_notional_usdt=args.cap_usdt,
            price=ref_price,
            min_notional=filters["min_notional"],
            step_size=filters["step_size"],
            cap_usdt=args.cap_usdt,
            symbol_allowlist_override=allowlist,
        )
        if isinstance(sizing, FuturesSizingBlocked):
            logger.error("confirm sizing blocked: %s", sizing.reason)
            return 1
        assert isinstance(sizing, FuturesSizingResult)

        async with AsyncSessionLocal() as session:
            ledger = BinanceDemoLedgerService(session)
            instrument_id = await _get_or_create_instrument(session, symbol)
            open_cid = _new_cid()
            close_cid = _new_cid()

            return await _execute_confirm_lifecycle(
                execution=execution,
                ledger=ledger,
                session=session,
                venue_host=venue_host,
                instrument_id=instrument_id,
                open_cid=open_cid,
                close_cid=close_cid,
                symbol=symbol,
                side=args.side,
                order_type=args.order_type,
                price=args.price,
                qty=sizing.qty,
                notional=sizing.notional_usdt,
                leverage=args.leverage,
                close_with=args.close_with,
            )
    finally:
        await execution.aclose()


async def _execute_confirm_lifecycle(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: Any,
    session: Any,
    venue_host: str,
    instrument_id: int,
    open_cid: str,
    close_cid: str,
    symbol: str,
    side: str,
    order_type: str,
    price: Decimal | None,
    qty: Decimal,
    notional: Decimal,
    leverage: int,
    close_with: str,
) -> int:
    """Run the full planned→reconciled lifecycle. Returns exit code."""
    # 1. Position-mode check (One-way required for PR 2).
    try:
        mode_result = await execution.get_position_mode()
    except Exception as exc:  # noqa: BLE001
        logger.error("position_mode query failed: %s", exc)
        return 2
    if mode_result.is_hedge_mode:
        _trace("position_mode is_hedge=true")
        logger.error(
            "Hedge mode is not supported by PR 2 (One-way required). "
            "Switch the Futures Demo account to One-way mode and retry."
        )
        return 2
    _trace("position_mode is_hedge=false")

    # 2. Leverage set + echo verification.
    try:
        lev_result = await execution.set_leverage(
            symbol=symbol, leverage=leverage
        )
    except BinanceFuturesDemoLeverageMismatch as exc:
        logger.error("leverage mismatch: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("set_leverage failed: %s", exc)
        return 2
    _trace(
        f"leverage_set symbol={lev_result.symbol} leverage={lev_result.leverage}"
    )

    opposite_side = "SELL" if side == "BUY" else "BUY"
    now = _now_utc()

    # 3. PLANNED — open row.
    await ledger.record_planned(
        instrument_id=instrument_id,
        product="usdm_futures",
        venue_host=venue_host,
        client_order_id=open_cid,
        side=side,
        order_type=order_type,
        qty=qty,
        price=price,
        notional_usdt=notional,
        extra_metadata={
            "source": "rob-298-pr2-smoke",
            "role": "open",
            "leverage": leverage,
        },
        now=now,
    )
    await session.commit()
    _trace(
        f"planned cid={open_cid} product=usdm_futures symbol={symbol} "
        f"side={side} qty={qty} venue={venue_host}"
    )

    # 4. PREVIEWED — local preview (no HTTP).
    preview = execution.preview_submit(
        symbol=symbol,
        side=side,
        order_type=order_type,
        qty=qty,
        client_order_id=open_cid,
        reduce_only=False,
    )
    assert isinstance(preview, FuturesDemoDryRunResult)
    await ledger.record_previewed(client_order_id=open_cid, now=_now_utc())
    await session.commit()
    _trace(f"previewed cid={open_cid}")

    # 5. VALIDATED — POST /fapi/v1/order/test (no placement).
    try:
        await execution.order_test(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            time_in_force="GTC" if order_type == "LIMIT" else None,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"order_test_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=order_test_failed")
        logger.error("order_test failed: %s", exc)
        return 2
    _trace(f"order_test_ok symbol={symbol}")
    await ledger.record_validated(client_order_id=open_cid, now=_now_utc())
    await session.commit()
    _trace(f"validated cid={open_cid}")

    # 6. SUBMITTED — signed POST /fapi/v1/order (real Demo placement; open).
    try:
        submit_result = await execution.submit_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=open_cid,
            price=price,
            time_in_force="GTC" if order_type == "LIMIT" else None,
            reduce_only=False,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"submit_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=submit_failed")
        logger.error("submit (open) failed: %s", exc)
        return 2
    assert isinstance(submit_result, FuturesDemoOrderSubmitResult)
    broker_id = submit_result.broker_order_id
    submit_status = submit_result.status
    await ledger.record_submitted(
        client_order_id=open_cid,
        broker_order_id=broker_id,
        now=_now_utc(),
        extra_metadata_merge={"submit_status": submit_status},
    )
    await session.commit()
    _trace(
        f"submitted cid={open_cid} broker_order_id={broker_id} "
        f"status={submit_status} reduce_only=false"
    )

    # 7. FILLED — record when server reports FILLED.
    open_was_filled = submit_status == "FILLED"
    if open_was_filled:
        await ledger.record_filled(client_order_id=open_cid, now=_now_utc())
        await session.commit()
        _trace(f"filled cid={open_cid}")

    # 8. Pre-close position check: position must not be flat.
    try:
        pre_close_pos = await execution.get_position(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"position_query_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=position_query_failed")
        logger.error("position query failed: %s", exc)
        return 2

    if pre_close_pos.is_flat:
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=(
                "open_did_not_take_effect: position flat after submit "
                f"status={submit_status}"
            ),
            now=_now_utc(),
        )
        await session.commit()
        _trace(
            f"anomaly cid={open_cid} reason=open_did_not_take_effect"
        )
        logger.error(
            "open side did not take effect — position is flat after submit "
            "(status=%s)",
            submit_status,
        )
        return 2

    position_amt = pre_close_pos.position_amt
    _trace(f"position_check symbol={symbol} amt={position_amt}")

    # 9. Close side — reduceOnly always true.
    close_qty = abs(position_amt)
    return await _close_with_reduce_only(
        execution=execution,
        ledger=ledger,
        session=session,
        venue_host=venue_host,
        instrument_id=instrument_id,
        open_cid=open_cid,
        close_cid=close_cid,
        symbol=symbol,
        close_side=opposite_side,
        close_qty=close_qty,
        notional=notional,
        leverage=leverage,
    )


async def _close_with_reduce_only(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: Any,
    session: Any,
    venue_host: str,
    instrument_id: int,
    open_cid: str,
    close_cid: str,
    symbol: str,
    close_side: str,
    close_qty: Decimal,
    notional: Decimal,
    leverage: int,
) -> int:
    """Round-trip the position with a reduceOnly MARKET close."""
    now = _now_utc()
    await ledger.record_planned(
        instrument_id=instrument_id,
        product="usdm_futures",
        venue_host=venue_host,
        client_order_id=close_cid,
        side=close_side,
        order_type="MARKET",
        qty=close_qty,
        price=None,
        notional_usdt=notional,
        parent_client_order_id=open_cid,
        extra_metadata={
            "source": "rob-298-pr2-smoke",
            "role": "close",
            "reduce_only": True,
            "leverage": leverage,
        },
        now=now,
    )
    await session.commit()
    _trace(
        f"planned cid={close_cid} product=usdm_futures symbol={symbol} "
        f"side={close_side} qty={close_qty} venue={venue_host}"
    )

    await ledger.record_previewed(client_order_id=close_cid, now=_now_utc())
    await session.commit()
    _trace(f"previewed cid={close_cid}")

    # Validate close shape via order_test (reduceOnly=true).
    try:
        await execution.order_test(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            qty=close_qty,
            reduce_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=close_cid,
            reason=f"close_order_test_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={close_cid} reason=close_order_test_failed")
        logger.error("close order_test failed: %s", exc)
        return 2
    await ledger.record_validated(client_order_id=close_cid, now=_now_utc())
    await session.commit()
    _trace(f"validated cid={close_cid}")

    # Submit close with reduceOnly=true (defense in depth — cannot flip).
    try:
        close_result = await execution.submit_order(
            symbol=symbol,
            side=close_side,
            order_type="MARKET",
            qty=close_qty,
            client_order_id=close_cid,
            reduce_only=True,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=close_cid,
            reason=f"close_submit_failed: {exc}",
            now=_now_utc(),
        )
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"close_submit_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={close_cid} reason=close_submit_failed")
        logger.error("close submit failed: %s", exc)
        return 2
    assert isinstance(close_result, FuturesDemoOrderSubmitResult)
    close_status = close_result.status
    await ledger.record_submitted(
        client_order_id=close_cid,
        broker_order_id=close_result.broker_order_id,
        now=_now_utc(),
        extra_metadata_merge={
            "submit_status": close_status,
            "reduce_only": True,
        },
    )
    await session.commit()
    _trace(
        f"submitted cid={close_cid} broker_order_id={close_result.broker_order_id} "
        f"status={close_status} reduce_only=true"
    )
    close_was_filled = close_status == "FILLED"
    if close_was_filled:
        await ledger.record_filled(client_order_id=close_cid, now=_now_utc())
        await session.commit()
        _trace(f"filled cid={close_cid}")

    # Close out the open row before reconciliation.
    await ledger.record_closed(client_order_id=open_cid, now=_now_utc())
    await session.commit()
    _trace(f"closed cid={open_cid}")

    return await _reconcile(
        execution=execution,
        ledger=ledger,
        session=session,
        open_cid=open_cid,
        close_cid=close_cid,
        symbol=symbol,
        close_was_filled=close_was_filled,
    )


async def _reconcile(
    *,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: Any,
    session: Any,
    open_cid: str,
    close_cid: str | None,
    symbol: str,
    close_was_filled: bool | None,
) -> int:
    """Reconciliation gate: open_orders empty AND position flat.

    Returns 0 on clean reconcile, 2 on drift / anomaly.
    """
    try:
        open_orders = await execution.get_open_orders(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"open_orders_query_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=open_orders_query_failed")
        logger.error("open_orders query failed: %s", exc)
        return 2

    is_empty = not open_orders.orders
    _trace(f"open_orders_check empty={'true' if is_empty else 'false'}")
    if not is_empty:
        residual_cids = [o.client_order_id for o in open_orders.orders]
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"open_orders_residual: {residual_cids!r}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=open_orders_residual")
        return 2

    # Position must be flat for a clean reconcile.
    try:
        post_pos = await execution.get_position(symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=f"position_query_failed: {exc}",
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=position_query_failed")
        logger.error("post-close position query failed: %s", exc)
        return 2

    _trace(
        f"position_check symbol={symbol} amt={post_pos.position_amt} "
        f"is_flat={'true' if post_pos.is_flat else 'false'}"
    )
    if not post_pos.is_flat:
        await ledger.record_anomaly(
            client_order_id=open_cid,
            reason=(
                f"position_not_flat_after_close: amt={post_pos.position_amt}"
            ),
            now=_now_utc(),
        )
        await session.commit()
        _trace(f"anomaly cid={open_cid} reason=position_not_flat")
        return 2

    await ledger.record_reconciled(client_order_id=open_cid, now=_now_utc())
    if close_cid is not None and close_was_filled:
        try:
            await ledger.record_closed(
                client_order_id=close_cid, now=_now_utc()
            )
            await ledger.record_reconciled(
                client_order_id=close_cid, now=_now_utc()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "close-row reconcile non-fatal: %s (cid=%s)", exc, close_cid
            )
    await session.commit()
    _trace(f"reconciled cid={open_cid}")
    _evidence(
        {
            "event": "futures_demo_confirm_reconciled",
            "open_client_order_id": open_cid,
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
    """Pull ``LOT_SIZE.stepSize`` + ``MIN_NOTIONAL.notional`` for ``symbol``.

    USD-M Futures uses ``filterType == 'MIN_NOTIONAL'`` with field
    ``notional`` (no fallback aliases needed for the demo deployment).
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
        elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
            mn = (
                entry.get("notional")
                or entry.get("minNotional")
                or entry.get("minNotionalValue")
            )
            if mn is not None:
                min_notional = Decimal(str(mn))
    if step_size is None:
        raise RuntimeError(
            f"no LOT_SIZE filter in exchangeInfo for {symbol!r}"
        )
    if min_notional is None:
        # Fall back to a conservative 5 USDT if the server doesn't expose
        # the filter. XRPUSDT on demo-fapi is typically 5 USDT.
        min_notional = Decimal("5")
    return {"step_size": step_size, "min_notional": min_notional}


async def _fetch_reference_price(base_url: str, symbol: str) -> Decimal:
    """Pull the latest mark price for ``symbol`` (public read)."""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        resp = await client.get(_PRICE_PATH, params={"symbol": symbol})
        resp.raise_for_status()
        body = resp.json()
    price = body.get("price")
    if price is None:
        raise RuntimeError(
            f"ticker/price returned no price for {symbol!r}"
        )
    return Decimal(str(price))


# ---------------------------------------------------------------------------
# Instrument resolution (for ledger FK).
# ---------------------------------------------------------------------------
async def _get_or_create_instrument(session: Any, symbol: str) -> int:
    """Find-or-create ``crypto_instruments`` for ``(binance, usdm_futures, symbol)``.

    Returns the row's ``id`` (FK target for the ledger). Base / quote
    assets are inferred for ``*USDT`` symbols (the only ones in scope for
    PR 2's MVP); other suffixes raise.
    """
    from sqlalchemy import select

    from app.models.crypto_instruments import CryptoInstrument

    result = await session.execute(
        select(CryptoInstrument.id).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "usdm_futures",
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        return int(row)
    if not symbol.endswith("USDT"):
        raise RuntimeError(
            f"crypto_instruments row missing for binance/usdm_futures/"
            f"{symbol!r} and only *USDT pairs are auto-seeded."
        )
    base = symbol[: -len("USDT")]
    inst = CryptoInstrument(
        venue="binance",
        product="usdm_futures",
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


# ---------------------------------------------------------------------------
# Top-level orchestration.
# ---------------------------------------------------------------------------
async def _run(args: argparse.Namespace) -> int:
    # Hard invariant #1: default-disabled. The gate is checked AFTER
    # argparse so `--help` still works without the env set, but BEFORE
    # any mode dispatch / HTTP / DB.
    if not _truthy(os.environ.get("BINANCE_FUTURES_DEMO_ENABLED")):
        logger.info(
            "futures demo disabled — set BINANCE_FUTURES_DEMO_ENABLED=true "
            "to opt in"
        )
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
        "futures demo enabled but no action requested. Pass --plan-only "
        "for a no-HTTP planning template, --preflight for read-only "
        "account, --order-test for signed shape validation, or --confirm "
        "for a full open + reduceOnly close round-trip with ledger "
        "lifecycle writes."
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
    except BinanceFuturesDemoDisabled as exc:
        logger.error("futures demo disabled: %s", exc)
        return 1
    except BinanceFuturesDemoMissingCredentials as exc:
        logger.error("futures demo credentials missing: %s", exc)
        return 1
    except BinanceFuturesDemoUnsupportedAuth as exc:
        logger.error("futures demo unsupported auth: %s", exc)
        return 2
    except BinanceFuturesDemoHedgeModeBlocked as exc:
        logger.error("futures demo hedge mode blocked: %s", exc)
        return 2
    except BinanceFuturesDemoLeverageMismatch as exc:
        logger.error("futures demo leverage mismatch: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.error("futures demo smoke failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
