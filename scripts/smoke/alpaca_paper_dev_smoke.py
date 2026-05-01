"""Dev/operator-only Alpaca PAPER submit→cancel smoke (ROB-73).

Modes:
  Preview-only (default):
      uv run python scripts/smoke/alpaca_paper_dev_smoke.py
    Calls account/cash + alpaca_paper_submit_order(confirm=False) +
    alpaca_paper_cancel_order(order_id='dummy', confirm=False).
    No broker mutations.

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

from app.mcp_server.tooling.alpaca_paper import (
    alpaca_paper_get_account,
    alpaca_paper_get_cash,
)
from app.mcp_server.tooling.alpaca_paper_orders import (
    alpaca_paper_cancel_order,
    alpaca_paper_submit_order,
)

SMOKE_SYMBOL = "AAPL"
SMOKE_QTY = Decimal("1")
SMOKE_LIMIT_PRICE = Decimal("1.00")  # far below market — should not fill
ENV_GATE = "ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS"


def _both_gates_set(args: argparse.Namespace) -> bool:
    return bool(args.confirm_paper_side_effect) and os.environ.get(ENV_GATE) == "1"


async def _preview_only() -> int:
    lines: list[tuple[str, bool, str]] = []
    try:
        acct = await alpaca_paper_get_account()
        lines.append(("get_account", True, f"status={acct['account'].get('status', '?')}"))
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))

    try:
        cash = await alpaca_paper_get_cash()
        lines.append(("get_cash", True, f"cash_set={cash['cash'].get('cash') is not None}"))
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_cash", False, f"ERROR: {type(exc).__name__}"))

    try:
        submit_result = await alpaca_paper_submit_order(
            symbol=SMOKE_SYMBOL, side="buy", type="limit",
            qty=SMOKE_QTY, limit_price=SMOKE_LIMIT_PRICE,
        )
        lines.append((
            "submit_order(confirm=False)", submit_result["submitted"] is False,
            f"blocked_reason={submit_result.get('blocked_reason')}",
        ))
    except Exception as exc:  # noqa: BLE001
        lines.append(("submit_order(confirm=False)", False, f"ERROR: {type(exc).__name__}"))

    try:
        cancel_result = await alpaca_paper_cancel_order(order_id="dummy-no-op")
        lines.append((
            "cancel_order(confirm=False)", cancel_result["cancelled"] is False,
            f"blocked_reason={cancel_result.get('blocked_reason')}",
        ))
    except Exception as exc:  # noqa: BLE001
        lines.append(("cancel_order(confirm=False)", False, f"ERROR: {type(exc).__name__}"))

    ok = all(success for _, success, _ in lines)
    for name, success, note in lines:
        print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
    print(f"summary: {'PASS' if ok else 'FAIL'} mode=preview_only")
    return 0 if ok else 1


async def _side_effect_smoke() -> int:
    lines: list[tuple[str, bool, str]] = []
    submitted_id: str | None = None
    cancelled = False

    try:
        acct = await alpaca_paper_get_account()
        lines.append(("get_account", True, f"status={acct['account'].get('status', '?')}"))
    except Exception as exc:  # noqa: BLE001
        lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))
        for name, s, note in lines:
            print(f"  [{'OK' if s else 'FAIL'}] {name}: {note}")
        print("summary: BLOCKED mode=side_effects reason=account_unreachable")
        return 1

    try:
        submit_result = await alpaca_paper_submit_order(
            symbol=SMOKE_SYMBOL, side="buy", type="limit",
            qty=SMOKE_QTY, limit_price=SMOKE_LIMIT_PRICE,
            confirm=True,
        )
        submitted_id = submit_result["order"]["id"]
        lines.append((
            "submit_order(confirm=True)", submit_result["submitted"] is True,
            f"order_id_len={len(submitted_id)} status={submit_result['order'].get('status', '?')}",
        ))
    except Exception as exc:  # noqa: BLE001
        lines.append(("submit_order(confirm=True)", False, f"ERROR: {type(exc).__name__}"))

    if submitted_id:
        try:
            cancel_result = await alpaca_paper_cancel_order(
                order_id=submitted_id, confirm=True,
            )
            cancelled = bool(cancel_result.get("cancelled"))
            readback_status = cancel_result.get("read_back_status", "unknown")
            order_info = cancel_result.get("order") or {}
            lines.append((
                "cancel_order(confirm=True)", cancelled,
                f"read_back={readback_status} final_status={order_info.get('status', '?')}",
            ))
        except Exception as exc:  # noqa: BLE001
            lines.append(("cancel_order(confirm=True)", False, f"ERROR: {type(exc).__name__}"))

    ok = all(success for _, success, _ in lines)
    for name, success, note in lines:
        print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
    classification = "PASS" if ok and cancelled else "PARTIAL"
    print(f"summary: {classification} mode=side_effects")
    return 0 if classification == "PASS" else 1


async def _async_main(args: argparse.Namespace) -> int:
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

    if _both_gates_set(args):
        return await _side_effect_smoke()
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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
