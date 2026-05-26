# scripts/kiwoom_mock_smoke.py
"""Operator-safe Kiwoom mock-investment order smoke (ROB-319).

Default-disabled. KRX-only. Mock host only (enforced in KiwoomMockClient's
host allowlist). Never prints secret values — only presence/missing of the
required env keys.

Each broker mutation requires an explicit ``--confirm``. The buy-limit price is
operator-approved via ``--price`` and floored to the KRX tick (no new quote
engine — reference an existing KIS quote out of band to pick a conservative,
non-marketable price). Cancel is wired (ROB-319), so ``full`` mode always
attempts to cancel any order it opened (finally-block) and reconciles, rather
than stranding a real mock order.

Usage:
    uv run python -m scripts.kiwoom_mock_smoke --mode preflight
    uv run python -m scripts.kiwoom_mock_smoke --mode preview \
        --symbol 005930 --price 50000 --quantity 1
    # Real mock lifecycle (submit -> history -> modify? -> cancel -> reconcile):
    uv run python -m scripts.kiwoom_mock_smoke --mode full \
        --symbol 005930 --price 50000 --quantity 1 \
        --new-price 49900 --new-quantity 1 --confirm

See docs/runbooks/kiwoom-mock-smoke.md for the full procedure and safety notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.core.config import validate_kiwoom_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.mcp_server.tooling import orders_kiwoom_variants as kvar

KRX = "KRX"


class SmokeRejected(RuntimeError):
    """Raised when operator inputs violate a smoke safety boundary."""


class _Recorder:
    """Minimal MCP shim that captures registered tool callables."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):  # noqa: ARG002
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def ensure_krx(exchange: str) -> str:
    value = (exchange or KRX).strip().upper()
    if value != KRX:
        raise SmokeRejected(f"Kiwoom mock smoke is KRX-only; got {exchange!r}")
    return value


def tick_aligned_price(price: int) -> int:
    """Floor an operator-approved price to the KRX tick (buy-side rounding)."""

    tick = get_tick_size_kr(price)
    return (int(price) // tick) * tick


def extract_order_id(payload: dict[str, Any]) -> str | None:
    for key in ("ord_no", "order_no"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _tools() -> dict[str, Any]:
    recorder = _Recorder()
    kvar.register(recorder)
    return recorder.tools


def _emit(payload: dict[str, Any]) -> None:
    # Mock-only payloads; no secrets ever flow through these responses.
    print(json.dumps(payload, ensure_ascii=False, default=str))


async def run_preflight() -> dict[str, Any]:
    missing = validate_kiwoom_mock_config()
    return {
        "step": "preflight",
        "ok": not missing,
        "missing_env_keys": missing,  # names only, never values
    }


async def run_preview(symbol: str, price: int, quantity: int) -> dict[str, Any]:
    tools = _tools()
    return await tools["kiwoom_mock_preview_order"](
        symbol=symbol, side="buy", quantity=quantity, price=price
    )


async def run_full(args: argparse.Namespace) -> int:
    """Submit -> history -> modify? -> cancel -> reconcile, fail-closed.

    Cancels in a finally block so a real mock order is never left open.
    """

    tools = _tools()
    price = tick_aligned_price(args.price)
    _emit({"step": "price_tick_aligned", "requested": args.price, "used": price})

    preview = await run_preview(args.symbol, price, args.quantity)
    _emit({"step": "preview", **preview})

    dry = await tools["kiwoom_mock_place_order"](
        symbol=args.symbol,
        side="buy",
        quantity=args.quantity,
        price=price,
        dry_run=True,
    )
    _emit({"step": "place_dry_run", **dry})

    if not args.confirm:
        _emit({"step": "stop", "reason": "no --confirm; stopped after dry-run"})
        return 0

    placed = await tools["kiwoom_mock_place_order"](
        symbol=args.symbol,
        side="buy",
        quantity=args.quantity,
        price=price,
        dry_run=False,
        confirm=True,
    )
    _emit({"step": "place_confirmed", **placed})
    if not placed.get("success"):
        _emit({"step": "abort", "reason": "place_order did not succeed"})
        return 2

    order_id = extract_order_id(placed)
    if not order_id:
        # Order may exist but we could not parse its id — reconcile and surface
        # loudly rather than pretend success.
        history = await tools["kiwoom_mock_get_order_history"]()
        _emit({"step": "reconcile_no_order_id", **history})
        _emit(
            {
                "step": "anomaly",
                "reason": "place succeeded but order id unparsed; check history "
                "and cancel manually if an order is open",
            }
        )
        return 2

    exit_code = 0
    try:
        history = await tools["kiwoom_mock_get_order_history"]()
        _emit({"step": "history_after_place", **history})

        if args.new_price is not None and args.new_quantity is not None:
            modified = await tools["kiwoom_mock_modify_order"](
                order_id=order_id,
                symbol=args.symbol,
                new_price=tick_aligned_price(args.new_price),
                new_quantity=args.new_quantity,
                dry_run=False,
                confirm=True,
            )
            _emit({"step": "modify_confirmed", **modified})
            # A modify may reissue the order number — track it for cancel.
            new_id = extract_order_id(modified)
            if modified.get("success") and new_id:
                order_id = new_id
        else:
            _emit({"step": "modify_skipped", "reason": "no --new-price/--new-quantity"})
    finally:
        cancelled = await tools["kiwoom_mock_cancel_order"](
            order_id=order_id,
            symbol=args.symbol,
            cancel_quantity=args.quantity,
            dry_run=False,
            confirm=True,
        )
        _emit({"step": "cancel_confirmed", "order_id": order_id, **cancelled})
        if not cancelled.get("success"):
            exit_code = 2
            _emit(
                {
                    "step": "cleanup_required",
                    "order_id": order_id,
                    "reason": "cancel did not succeed; verify and cancel manually",
                }
            )

    final_history = await tools["kiwoom_mock_get_order_history"]()
    _emit({"step": "final_reconcile_history", **final_history})
    positions = await tools["kiwoom_mock_get_positions"]()
    _emit({"step": "final_reconcile_positions", **positions})
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiwoom mock order smoke (ROB-319)")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["preflight", "preview", "full"],
    )
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--price", type=int, default=None)
    parser.add_argument("--quantity", type=int, default=None)
    parser.add_argument("--new-price", type=int, default=None)
    parser.add_argument("--new-quantity", type=int, default=None)
    parser.add_argument(
        "--exchange", default=KRX, help="KRX only; any other value is rejected."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for any real broker mutation (full mode submit/modify/cancel).",
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    ensure_krx(args.exchange)

    if args.mode == "preflight":
        _emit(await run_preflight())
        return 0

    if not (args.symbol and args.price and args.quantity):
        raise SmokeRejected("symbol, price, quantity are required for this mode")

    if args.mode == "preview":
        _emit(
            {
                "step": "price_tick_aligned",
                "requested": args.price,
                "used": tick_aligned_price(args.price),
            }
        )
        _emit(
            {
                "step": "preview",
                **await run_preview(
                    args.symbol, tick_aligned_price(args.price), args.quantity
                ),
            }
        )
        return 0

    return await run_full(args)


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
