#!/usr/bin/env python3
"""KIS mock holdings/cash-delta fill-confirmation smoke (ROB-341).

Two modes, both KIS **mock** only (host VTTC8434R / mockapi):

* ``--preflight`` — READ-ONLY. Reads the mock domestic balance snapshot and
  prints the per-symbol holdings + cash. Places NO order. Run this first.
* ``--confirm``   — operator-gated bounded round trip: one small marketable
  limit BUY, holdings/cash-delta fill confirmation, then a cleanup SELL to
  flatten back to baseline. Prints the ROB-341 evidence packet as JSON.

Same-day fill confirmation is the baseline-vs-post **holdings delta** (primary)
plus the **cash delta** (fill-price source). daily-ccld is NOT used as the gate
(it can return empty rows for same-day mock fills); it is read only as a
non-gating, post-settlement diagnostic and surfaced in the evidence packet.

Safety: KIS mock only (no live), limit orders only (no market), no scheduler,
no persistent confirm flag. Default-disabled — requires
KIS_MOCK_SCALPING_WS_ENABLED=true plus KIS mock config. ``--confirm`` must be
passed explicitly; without it the round trip never runs. Prints only missing
env var NAMES, never secret values. Always attempts cleanup in a finally block.

Exit codes:
    0  - success (preflight printed, or confirmed round trip cleaned up)
    1  - unexpected exception
    2  - order/inquiry error, or fill could not be confirmed in the poll window
    3  - anomaly: residual position/pending order could not be cleaned up
    4  - disabled or KIS mock not configured (env/config no-op)

The cleanup SELL goes through the mock scalping-exit bypass, so ``--confirm``
also requires KIS_MOCK_SCALPING_ENABLED=true and an allowed cleanup exit reason
(default ``stop_loss`` — no smoke-only reason is added to the validator). Both
gates are preflighted BEFORE any BUY, so a missing/invalid gate stops the run
with no position acquired (exit 4). Set the gate flags ephemerally only.

Usage:
    KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_holdings_delta_smoke \
        --preflight --symbol 005930
    KIS_MOCK_SCALPING_ENABLED=true KIS_MOCK_SCALPING_WS_ENABLED=true \
        uv run python -m scripts.kis_mock_holdings_delta_smoke \
        --confirm --symbol 005930 --notional-krw 10000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from decimal import ROUND_DOWN, Decimal

logger = logging.getLogger(__name__)

# Cleanup SELL reuses an existing allowed ScalpingExitContext reason. We do NOT
# add a smoke-only synthetic reason to the validator surface (ROB-358), so no
# synthetic reason ever lands in the ledger.exit_reason column.
_DEFAULT_CLEANUP_EXIT_REASON = "stop_loss"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KIS mock holdings/cash-delta fill-confirmation smoke"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="read-only: print holdings + cash for --symbol, place no order",
    )
    mode.add_argument(
        "--confirm",
        action="store_true",
        help="operator-gated bounded round trip (one small limit buy + cleanup sell)",
    )
    parser.add_argument("--symbol", required=True, help="KR stock code, e.g. 005930")
    parser.add_argument("--market", default="J", help="KIS market div code (default J)")
    parser.add_argument(
        "--notional-krw",
        type=int,
        default=10000,
        help="max buy notional in KRW (default 10000)",
    )
    parser.add_argument("--max-poll", type=int, default=10)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--cleanup-reason",
        default=_DEFAULT_CLEANUP_EXIT_REASON,
        help=(
            "scalping exit reason for the cleanup SELL "
            f"(default {_DEFAULT_CLEANUP_EXIT_REASON!r}; must be an allowed "
            "ScalpingExitContext reason — no smoke-only reason is introduced)"
        ),
    )
    return parser.parse_args(argv)


def _gate_or_exit() -> object | None:
    """Lazy import + env gate. Returns the settings object, or None if disabled."""
    from app.core.config import settings

    if not settings.kis_mock_scalping_ws_enabled:
        logger.info("KIS_MOCK_SCALPING_WS_ENABLED is not set; smoke disabled (no-op).")
        return None
    missing = [
        name
        for name, value in (
            ("KIS_MOCK_APP_KEY", settings.kis_mock_app_key),
            ("KIS_MOCK_APP_SECRET", settings.kis_mock_app_secret),
            ("KIS_MOCK_ACCOUNT_NO", settings.kis_mock_account_no),
        )
        if not value
    ]
    if missing:
        logger.error("KIS mock not configured. Missing (names only): %s", missing)
        return None
    return settings


def _confirm_cleanup_preflight_error(
    settings_obj: object, cleanup_reason: str
) -> str | None:
    """Fail-fast check that the cleanup SELL is submittable, run BEFORE any BUY.

    Returns an error string when a required cleanup gate is missing/invalid,
    else None. ``KIS_MOCK_SCALPING_ENABLED`` gates the mock scalping-exit sell
    bypass; the cleanup reason must be an allowed ``ScalpingExitContext`` reason
    (we reuse ``stop_loss`` — no smoke-only synthetic reason is introduced).
    """
    if not getattr(settings_obj, "kis_mock_scalping_enabled", False):
        return (
            "KIS_MOCK_SCALPING_ENABLED=true is required for the cleanup SELL; "
            "set it ephemerally for this run only (no persistent flag)"
        )
    from app.mcp_server.tooling.order_validation import _SCALPING_EXIT_REASONS

    if cleanup_reason not in _SCALPING_EXIT_REASONS:
        return (
            f"invalid cleanup exit reason {cleanup_reason!r}; must be one of "
            f"{sorted(_SCALPING_EXIT_REASONS)}"
        )
    return None


def _best_ask_bid(orderbook: dict) -> tuple[Decimal | None, Decimal | None]:
    def _dec(key: str) -> Decimal | None:
        raw = orderbook.get(key)
        try:
            v = Decimal(str(raw).replace(",", "").strip())
        except Exception:  # noqa: BLE001
            return None
        return v if v > 0 else None

    return _dec("askp1"), _dec("bidp1")


async def run_preflight(args: argparse.Namespace) -> int:
    if _gate_or_exit() is None:
        return 4
    from app.mcp_server.tooling.order_execution import _create_kis_client
    from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker

    client = _create_kis_client(is_mock=True)
    broker = KisMockBroker(get_state=lambda s: None)
    broker._mock_client = client  # reuse the same mock-host client
    try:
        holdings_qty, cash = await broker._read_snapshot(args.symbol)
    except Exception as exc:  # noqa: BLE001 - read-only preflight, classify the fault
        logger.error("balance snapshot read failed: %s", str(exc)[:300])
        return 2
    logger.info(
        json.dumps(
            {
                "mode": "preflight",
                "symbol": args.symbol,
                "holdings_qty": str(holdings_qty),
                "cash_dnca_tot_amt": (str(cash) if cash is not None else None),
            }
        )
    )
    return 0


async def _await_fill(broker, submit_result, *, max_poll: int, interval: float):
    for _ in range(max_poll):
        fill = await broker.confirm_fill(submit_result)
        if fill is not None:
            return fill
        await asyncio.sleep(interval)
    return None


async def run_confirm(args: argparse.Namespace) -> int:
    settings_obj = _gate_or_exit()
    if settings_obj is None:
        return 4
    # Fail-fast BEFORE any BUY: if the cleanup SELL cannot be safely executed,
    # never acquire a position we cannot flatten (ROB-358).
    preflight_error = _confirm_cleanup_preflight_error(
        settings_obj, args.cleanup_reason
    )
    if preflight_error is not None:
        logger.error("cleanup preflight failed; no order placed: %s", preflight_error)
        logger.info(
            json.dumps(
                {
                    "mode": "confirm",
                    "symbol": args.symbol,
                    "preflight": "cleanup_gate_failed",
                    "error": preflight_error,
                }
            )
        )
        return 4
    import uuid

    from app.mcp_server.tooling.order_execution import _create_kis_client
    from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker

    client = _create_kis_client(is_mock=True)
    broker = KisMockBroker(get_state=lambda s: None)
    broker._mock_client = client
    cid = f"rob341-smoke-{uuid.uuid4().hex[:12]}"
    evidence: dict[str, object] = {"mode": "confirm", "symbol": args.symbol, "cid": cid}

    base_qty, base_cash = await broker._read_snapshot(args.symbol)
    evidence["baseline_holdings_qty"] = str(base_qty)
    evidence["baseline_cash"] = str(base_cash) if base_cash is not None else None

    orderbook = await client.inquire_orderbook(args.symbol, args.market)
    ask, bid = _best_ask_bid(orderbook)
    if ask is None or bid is None:
        logger.error("no valid ask/bid in orderbook; cannot place a marketable limit")
        evidence["error"] = "no_quote"
        logger.info(json.dumps(evidence))
        return 2
    qty = (Decimal(args.notional_krw) / ask).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if qty <= 0:
        logger.error("notional %s too small for ask %s", args.notional_krw, ask)
        evidence["error"] = "size_zero"
        logger.info(json.dumps(evidence))
        return 2
    evidence["buy_limit_price"] = str(ask)
    evidence["quantity"] = str(qty)

    entry_fill = None
    try:
        buy = await broker.submit_buy(
            symbol=args.symbol,
            price=ask,
            quantity=qty,
            correlation_id=cid,
            confirm=True,
        )
        evidence["buy_order_id"] = buy.get("odno") or buy.get("order_no")
        entry_fill = await _await_fill(
            broker, buy, max_poll=args.max_poll, interval=args.poll_interval
        )
        evidence["confirmation_signal"] = "holdings_delta"
        if entry_fill is None:
            # STOP condition: holdings did not reflect the fill within the window.
            # Do NOT early-return — fall through to the finally so cleanup can
            # detect+flatten a fill that landed just after the poll window and
            # set the authoritative exit code (residual that can't be flattened
            # -> 3 outranks fill-unconfirmed -> 2).
            evidence["entry_filled"] = False
            evidence["note"] = (
                "entry fill UNCONFIRMED within poll window — holdings did not "
                "reflect a same-day mock fill (ROB-341 STOP condition)"
            )
        else:
            evidence["entry_filled"] = True
            evidence["entry_fill_price"] = str(entry_fill.price)
            evidence["entry_fill_qty"] = str(entry_fill.quantity)
    finally:
        result = await _cleanup_and_verify(
            broker, client, args, cid, base_qty, evidence, entry_fill
        )
        evidence["exit_code"] = result
        logger.info(json.dumps(evidence))
    return result


async def _cleanup_and_verify(
    broker, client, args, cid, base_qty, evidence, entry_fill
) -> int:
    """Flatten any position acquired by this smoke back to baseline. Returns the
    process exit code (0 clean, 2 fill-unconfirmed, 3 anomaly/residual)."""
    try:
        cur_qty, _ = await broker._read_snapshot(args.symbol)
    except Exception as exc:  # noqa: BLE001
        evidence["cleanup_error"] = str(exc)[:200]
        return 3
    delta = cur_qty - base_qty
    if delta < 0:
        # Holdings dropped BELOW baseline before we sold (over-flatten / external
        # mutation). A non-zero negative delta is never a clean exit (ROB-358).
        evidence["final_position_delta_vs_baseline"] = str(delta)
        evidence["cleanup"] = "below_baseline_anomaly"
        evidence["cleanup_error"] = (
            f"holdings {cur_qty} below baseline {base_qty} before cleanup SELL"
        )
        return 3
    if delta == 0:
        evidence["cleanup"] = "nothing_to_flatten"
        evidence["final_position_delta_vs_baseline"] = str(delta)
        # If the entry never filled, propagate the fill-unconfirmed code.
        return 0 if entry_fill is not None else 2

    orderbook = await client.inquire_orderbook(args.symbol, args.market)
    _, bid = _best_ask_bid(orderbook)
    if bid is None:
        evidence["cleanup"] = "no_bid_for_exit"
        return 3
    try:
        sell = await broker.submit_exit_sell(
            symbol=args.symbol,
            price=bid,
            quantity=delta,
            exit_reason=args.cleanup_reason,
            strategy_id="kis-mock-v1",
            correlation_id=cid,
            confirm=True,
        )
    except Exception as exc:  # noqa: BLE001 - submit rejection is a cleanup anomaly, not unexpected
        # A rejected cleanup SELL leaves a residual position; classify it as an
        # explicit anomaly (exit 3) instead of leaking out as exit 1 (ROB-358).
        evidence["cleanup_sell_order_id"] = None
        evidence["cleanup_error"] = str(exc)[:200]
        evidence["cleanup"] = "SELL_submit_rejected"
        evidence["final_position_delta_vs_baseline"] = str(cur_qty - base_qty)
        return 3
    order_id = sell.get("odno") or sell.get("order_no")
    evidence["cleanup_sell_order_id"] = order_id
    if not order_id:
        # Missing odno/order_no -> explicit failure with reason; residual stays.
        evidence["cleanup_error"] = "cleanup SELL response missing odno/order_no"
        evidence["cleanup"] = "SELL_no_order_id"
        evidence["final_position_delta_vs_baseline"] = str(cur_qty - base_qty)
        return 3
    exit_fill = await _await_fill(
        broker, sell, max_poll=args.max_poll, interval=args.poll_interval
    )
    final_qty, final_cash = await broker._read_snapshot(args.symbol)
    final_delta = final_qty - base_qty
    evidence["post_holdings_qty"] = str(final_qty)
    evidence["post_cash"] = str(final_cash) if final_cash is not None else None
    evidence["final_position_delta_vs_baseline"] = str(final_delta)
    if exit_fill is None or final_delta > 0:
        evidence["cleanup"] = "UNCONFIRMED_residual_position"
        return 3
    if final_delta < 0:
        # Over-flatten: sold past baseline. A clean exit requires delta == 0.
        evidence["cleanup"] = "over_flattened_anomaly"
        evidence["cleanup_error"] = (
            f"final holdings {final_qty} below baseline {base_qty} after cleanup SELL"
        )
        return 3
    # Clean exit only when holdings are back to EXACTLY the baseline.
    evidence["cleanup"] = "flattened"
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.preflight:
        return await run_preflight(args)
    return await run_confirm(args)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("unexpected error in holdings-delta smoke")
        return 1


if __name__ == "__main__":
    sys.exit(main())
