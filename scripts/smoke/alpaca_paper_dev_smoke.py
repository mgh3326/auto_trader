"""Dev/operator-only Alpaca PAPER submit→cancel smoke (ROB-73/ROB-74).

Modes:
  Preview-only (default):
      uv run python scripts/smoke/alpaca_paper_dev_smoke.py
    Calls account/cash + alpaca_paper_submit_order(confirm=False) +
    alpaca_paper_cancel_order(order_id='dummy', confirm=False).
    No broker mutations.

    ROB-74 crypto preview metadata can be supplied with --asset-class crypto,
    --symbol, --notional, --limit-price, and --candidate-report. These inputs
    are echoed only as redacted summary flags.

  Side-effect mode (BOTH gates required):
      ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1 \\
          uv run python scripts/smoke/alpaca_paper_dev_smoke.py \\
          --confirm-paper-side-effect
    Submits one tiny PAPER limit order (AAPL buy 1 share @ $1.00),
    captures its id, cancels it, reads back final status, prints a
    redacted summary.

This script never prints API keys, secrets, headers, or raw broker payloads.
Either gate alone is rejected.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

from app.mcp_server.tooling.alpaca_paper import (
    alpaca_paper_get_account,
    alpaca_paper_get_cash,
)
from app.mcp_server.tooling.alpaca_paper_orders import (
    alpaca_paper_cancel_order,
    alpaca_paper_submit_order,
)

DEFAULT_EQUITY_SYMBOL = "AAPL"
DEFAULT_CRYPTO_SYMBOL = "BTC/USD"
SMOKE_QTY = Decimal("1")
SMOKE_LIMIT_PRICE = Decimal("1.00")  # far below market — should not fill
ENV_GATE = "ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS"
PREVIEW_SUBMIT_CHECK = "submit_order(confirm=False)"
PREVIEW_CANCEL_CHECK = "cancel_order(confirm=False)"
SMOKE_LINE = tuple[str, bool, str]


def _both_gates_set(args: argparse.Namespace) -> bool:
    return bool(args.confirm_paper_side_effect) and os.environ.get(ENV_GATE) == "1"


async def _preview_only() -> int:
    lines: list[SMOKE_LINE] = []
    await _append_account_check(lines)
    await _append_cash_check(lines)
    await _append_preview_submit_check(
        lines,
        {
            "symbol": DEFAULT_EQUITY_SYMBOL,
            "side": "buy",
            "type": "limit",
            "qty": SMOKE_QTY,
            "limit_price": SMOKE_LIMIT_PRICE,
        },
    )
    await _append_preview_cancel_check(lines)

    ok = all(success for _, success, _ in lines)
    for name, success, note in lines:
        print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
    print(f"summary: {'PASS' if ok else 'FAIL'} mode=preview_only")
    return 0 if ok else 1


async def _append_account_check(lines: list[SMOKE_LINE]) -> None:
    try:
        acct = await alpaca_paper_get_account()
        lines.append(
            ("get_account", True, f"status={acct['account'].get('status', '?')}")
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))


async def _append_cash_check(lines: list[SMOKE_LINE]) -> None:
    try:
        cash = await alpaca_paper_get_cash()
        lines.append(
            ("get_cash", True, f"cash_set={cash['cash'].get('cash') is not None}")
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_cash", False, f"ERROR: {type(exc).__name__}"))


async def _append_preview_submit_check(
    lines: list[SMOKE_LINE],
    payload: dict[str, object],
) -> None:
    try:
        submit_result = await alpaca_paper_submit_order(**payload)
        lines.append(
            (
                PREVIEW_SUBMIT_CHECK,
                submit_result["submitted"] is False,
                f"blocked_reason={submit_result.get('blocked_reason')}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        lines.append((PREVIEW_SUBMIT_CHECK, False, f"ERROR: {type(exc).__name__}"))


async def _append_preview_cancel_check(lines: list[SMOKE_LINE]) -> None:
    try:
        cancel_result = await alpaca_paper_cancel_order(order_id="dummy-no-op")
        lines.append(
            (
                PREVIEW_CANCEL_CHECK,
                cancel_result["cancelled"] is False,
                f"blocked_reason={cancel_result.get('blocked_reason')}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        lines.append((PREVIEW_CANCEL_CHECK, False, f"ERROR: {type(exc).__name__}"))


def _order_payload(args: argparse.Namespace) -> dict[str, object]:
    symbol = args.symbol or (
        DEFAULT_CRYPTO_SYMBOL if args.asset_class == "crypto" else DEFAULT_EQUITY_SYMBOL
    )
    payload: dict[str, object] = {
        "symbol": symbol,
        "side": args.side,
        "type": args.order_type,
        "asset_class": args.asset_class,
    }
    if args.notional is not None:
        payload["notional"] = args.notional
    else:
        payload["qty"] = args.qty
    if args.limit_price is not None:
        payload["limit_price"] = args.limit_price
    elif args.asset_class != "crypto":
        payload["limit_price"] = SMOKE_LIMIT_PRICE
    return payload


async def _preview_only_for_args(args: argparse.Namespace) -> int:
    lines: list[SMOKE_LINE] = []
    await _append_account_check(lines)
    await _append_cash_check(lines)
    await _append_preview_submit_check(lines, _order_payload(args))
    await _append_preview_cancel_check(lines)

    ok = all(success for _, success, _ in lines)
    for name, success, note in lines:
        print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
    if args.asset_class == "crypto":
        print(
            "summary: "
            f"{'PASS' if ok else 'FAIL'} mode=preview_only asset_class=crypto "
            f"candidate_report_attached={args.candidate_report is not None}"
        )
    else:
        print(f"summary: {'PASS' if ok else 'FAIL'} mode=preview_only")
    return 0 if ok else 1


async def _side_effect_smoke(args: argparse.Namespace) -> int:
    lines: list[SMOKE_LINE] = []
    if not await _side_effect_account_ready(lines):
        _print_lines(lines)
        print("summary: BLOCKED mode=side_effects reason=account_unreachable")
        return 1

    submitted_id = await _side_effect_submit(lines, args)
    cancelled = await _side_effect_cancel(lines, submitted_id)

    ok = all(success for _, success, _ in lines)
    _print_lines(lines)
    classification = "PASS" if ok and cancelled else "PARTIAL"
    print(f"summary: {classification} mode=side_effects")
    return 0 if classification == "PASS" else 1


async def _side_effect_account_ready(lines: list[SMOKE_LINE]) -> bool:
    try:
        acct = await alpaca_paper_get_account()
        lines.append(
            ("get_account", True, f"status={acct['account'].get('status', '?')}")
        )
        return True
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))
        return False


async def _side_effect_submit(
    lines: list[SMOKE_LINE], args: argparse.Namespace
) -> str | None:
    try:
        submit_result = await alpaca_paper_submit_order(
            **_order_payload(args),
            confirm=True,
        )
        submitted_id = submit_result["order"]["id"]
        lines.append(
            (
                "submit_order(confirm=True)",
                submit_result["submitted"] is True,
                f"order_id_len={len(submitted_id)} status={submit_result['order'].get('status', '?')}",
            )
        )
        return submitted_id
    except Exception as exc:  # noqa: BLE001
        lines.append(
            ("submit_order(confirm=True)", False, f"ERROR: {type(exc).__name__}")
        )
        return None


async def _side_effect_cancel(
    lines: list[SMOKE_LINE], submitted_id: str | None
) -> bool:
    if not submitted_id:
        return False
    try:
        cancel_result = await alpaca_paper_cancel_order(
            order_id=submitted_id,
            confirm=True,
        )
        cancelled = bool(cancel_result.get("cancelled"))
        readback_status = cancel_result.get("read_back_status", "unknown")
        order_info = cancel_result.get("order") or {}
        lines.append(
            (
                "cancel_order(confirm=True)",
                cancelled,
                f"read_back={readback_status} final_status={order_info.get('status', '?')}",
            )
        )
        return cancelled
    except Exception as exc:  # noqa: BLE001
        lines.append(
            ("cancel_order(confirm=True)", False, f"ERROR: {type(exc).__name__}")
        )
        return False


def _print_lines(lines: list[SMOKE_LINE]) -> None:
    for name, success, note in lines:
        print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")


async def _async_main(args: argparse.Namespace) -> int:
    if args.candidate_report and not Path(args.candidate_report).is_file():
        print(
            "BLOCKED: --candidate-report path does not exist or is not a file.",
            file=sys.stderr,
        )
        return 2

    if args.confirm_paper_side_effect and os.environ.get(ENV_GATE) != "1":
        print(
            f"BLOCKED: --confirm-paper-side-effect requires {ENV_GATE}=1; "
            "either gate alone is rejected.",
            file=sys.stderr,
        )
        return 2
    if not args.confirm_paper_side_effect and os.environ.get(ENV_GATE) == "1":
        print(
            f"BLOCKED: {ENV_GATE}=1 requires --confirm-paper-side-effect; "
            "either gate alone is rejected.",
            file=sys.stderr,
        )
        return 2

    if (
        args.confirm_paper_side_effect
        and args.asset_class == "crypto"
        and args.limit_price is None
    ):
        print(
            "BLOCKED: crypto side-effect smoke requires explicit --limit-price.",
            file=sys.stderr,
        )
        return 2

    if _both_gates_set(args):
        return await _side_effect_smoke(args)
    if args.asset_class == "crypto" or args.symbol or args.candidate_report:
        return await _preview_only_for_args(args)
    return await _preview_only()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dev-owned Alpaca PAPER submit/cancel smoke runner",
    )
    parser.add_argument(
        "--confirm-paper-side-effect",
        action="store_true",
        help=f"Required (with {ENV_GATE}=1) to enable broker mutations",
    )
    parser.add_argument(
        "--asset-class",
        choices=("us_equity", "crypto"),
        default="us_equity",
        help="Paper Alpaca asset class to validate; crypto is ROB-74 preview/MVP only",
    )
    parser.add_argument("--symbol", help="Order symbol, e.g. AAPL or BTC/USD")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument("--order-type", choices=("limit", "market"), default="limit")
    parser.add_argument("--qty", type=Decimal, default=SMOKE_QTY)
    parser.add_argument("--notional", type=Decimal)
    parser.add_argument("--limit-price", type=Decimal)
    parser.add_argument(
        "--candidate-report",
        help=(
            "Path to a candidate report; existence is validated, but contents are "
            "never read, parsed, logged, or converted into order parameters"
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
