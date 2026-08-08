"""B0-X sidecar cancel runner — own resting orders only (contract §2-4, v1.3 ①).

    # Scan only. Reads the whole account, cancels nothing, dispatches zero
    # mutation HTTP. This is the default and the safe thing to run first.
    B0X_SIDECAR_ENABLED=true BINANCE_SPOT_DEMO_ENABLED=true \\
        uv run python -m scripts.run_b0x_cancel

    # Actually cancel the b0xc- orders (operator gate, per call).
    ... uv run python -m scripts.run_b0x_cancel --confirm

The read is account-wide on purpose: these Demo credentials are shared with
other demo lanes, so the only honest answer to "what is resting here?" comes
from a symbol-less query. Whatever is *not* ``b0xc-`` prefixed is printed
under NOT_MINE and left strictly alone — there is no flag that cancels it,
by design (mock/CLAUDE.md §4).

No scheduler registration exists for this script. It runs because an operator
ran it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from scripts.b0x.crypto import cancel as cancel_module
from scripts.b0x.crypto import sidecar as sidecar_lane


def _print_outcome(outcome: cancel_module.CancelOutcome) -> None:
    part = outcome.partition
    print(
        f"open_orders_total={part.total} mine={len(part.mine)} foreign={len(part.foreign)}"
    )
    for order in part.mine:
        print(
            f"  MINE     {order.symbol} {order.client_order_id} "
            f"broker_order_id={order.broker_order_id} {order.side} "
            f"qty={order.qty} status={order.status}"
        )
    for order in part.foreign:
        print(
            f"  NOT_MINE {order.symbol} {order.client_order_id} "
            f"broker_order_id={order.broker_order_id} {order.side} "
            f"qty={order.qty} status={order.status}  ← untouched"
        )
    if not outcome.confirm:
        print("DRY RUN — zero cancel HTTP dispatched (pass --confirm to act)")
    for row in outcome.cancelled:
        print(
            f"  CANCELLED {row['symbol']} {row['client_order_id']} "
            f"dispatched={row['dispatched']} status={row['status']}"
        )


async def _run(args: argparse.Namespace) -> int:
    sidecar_lane.assert_sidecar_enabled()
    client = BinanceSpotDemoExecutionClient.from_env()
    try:
        if args.scan_only:
            found = await cancel_module.scan(client)
            outcome = cancel_module.CancelOutcome(
                partition=found, cancelled=(), confirm=False
            )
        else:
            outcome = await cancel_module.cancel_own(client, confirm=args.confirm)
    finally:
        await client.aclose()

    _print_outcome(outcome)
    if args.json:
        print(json.dumps(outcome.to_json(), sort_keys=True, ensure_ascii=False))
    # Foreign orders are not an error, but they are a fact the operator must
    # see, so they get a distinct non-zero exit rather than a silent success.
    return 3 if outcome.partition.foreign else 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="dispatch the cancels for b0xc- orders. Without it, nothing is sent.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="read and partition the account book; never enter the cancel path",
    )
    parser.add_argument("--json", action="store_true", help="also emit the raw record")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.scan_only and args.confirm:
        print("--scan-only and --confirm are mutually exclusive", file=sys.stderr)
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
