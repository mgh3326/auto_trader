"""ROB-1474 manual crypto paper/demo CLI; invoked by scripts.mock_strategy."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.services.mock_strategy.crypto_brokers import VENUES, build_broker
from app.services.mock_strategy.crypto_orders import MarketRules, positive
from app.services.mock_strategy.crypto_runner import close_due, run_lists
from app.services.mock_strategy.crypto_signals import HORIZONS, daily_lists
from app.services.mock_strategy.recorder import MockStrategyRecorder
from research.stock_selection.dataset import decision_at


class FixtureMarket:
    """Read-only planning input. Never usable for submission or fill evidence."""

    account_ref = "fixture-only"

    def __init__(self, path: Path):
        self.markets = json.loads(path.read_text())

    async def rules(self, symbol: str) -> MarketRules:
        item = self.markets.get(symbol)
        if item is None:
            raise ValueError("fixture symbol unavailable")
        return MarketRules(
            positive(item["step"]),
            positive(item["min_qty"]),
            positive(item["min_notional"]),
        )

    async def price(self, symbol: str) -> Decimal:
        return positive(self.markets[symbol]["price"])

    async def submit(self, *args, **kwargs):
        raise ValueError("fixtures cannot submit")

    async def evidence(self, *args, **kwargs):
        raise ValueError("fixtures cannot supply fill evidence")

    async def closeable(self, *args, **kwargs):
        raise ValueError("fixtures cannot authorize closes")

    async def aclose(self):
        pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("run", "close-due", "status"))
    result.add_argument("--venue", required=True, choices=VENUES)
    result.add_argument("--asof", required=True, type=date.fromisoformat)
    inputs = result.add_mutually_exclusive_group()
    inputs.add_argument("--panel-root", type=Path)
    inputs.add_argument("--cache-dir", type=Path, help="mock_data_refresh root")
    result.add_argument("--horizon", type=int, choices=HORIZONS, default=3)
    result.add_argument("--notional", type=positive, default=Decimal("1000"))
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--confirm", action="store_true")
    result.add_argument("--market-fixture", type=Path)
    result.add_argument(
        "--account-ref", help="optional status scope; required for close-due --dry-run"
    )
    return result


async def execute(args: argparse.Namespace) -> dict:
    if args.market_fixture and (not args.dry_run or args.command != "run"):
        raise ValueError("market fixtures require run --dry-run")
    if args.command == "close-due" and args.dry_run:
        if not args.account_ref:
            raise ValueError("close-due --dry-run requires --account-ref")
        rows = await MockStrategyRecorder().list_due(
            venue=args.venue,
            asof=decision_at("crypto", args.asof - timedelta(days=1)).to_pydatetime(),
            account_ref=args.account_ref,
        )
        return {
            "dry_run": True,
            "orders": [
                {
                    "client_order_id": r.client_order_id,
                    "symbol": r.symbol,
                    "status": r.status,
                }
                for r in rows
                if r.strategy_id.startswith("rob1474.crypto.")
            ],
            "note": "due entries only; exact broker fill and remaining-quantity checks required before close",
        }
    if args.command == "status":
        rows = await MockStrategyRecorder().list_orders(
            venue=args.venue, account_ref=args.account_ref
        )
        return {
            "venue": args.venue,
            "orders": [
                {
                    "client_order_id": r.client_order_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "status": r.status,
                    "realized_pnl": str(r.realized_pnl)
                    if r.realized_pnl is not None
                    else None,
                }
                for r in rows
                if r.strategy_id.startswith("rob1474.crypto.")
            ],
        }
    payload = None
    if args.command == "run":
        if args.panel_root is None and args.cache_dir is None:
            raise ValueError("run requires --panel-root or --cache-dir")
        if args.dry_run and args.market_fixture is None:
            raise ValueError("run --dry-run requires --market-fixture")
        payload = daily_lists(
            args.panel_root, args.asof, horizon=args.horizon, cache_root=args.cache_dir
        )
    # Offline fixture dry runs create neither a recorder nor a broker transport.
    broker = (
        FixtureMarket(args.market_fixture)
        if args.market_fixture
        else await build_broker(args.venue, confirm=args.confirm)
    )
    try:
        if args.dry_run and args.command == "run":
            return await run_lists(
                payload,
                venue=args.venue,
                asof=args.asof,
                broker=broker,
                horizon=args.horizon,
                notional=args.notional,
                dry_run=True,
            )
        recorder = MockStrategyRecorder()
        async with recorder.account_lock(
            venue=args.venue, account_ref=broker.account_ref
        ):
            if args.command == "run":
                return await run_lists(
                    payload,
                    venue=args.venue,
                    asof=args.asof,
                    broker=broker,
                    recorder=recorder,
                    horizon=args.horizon,
                    notional=args.notional,
                    dry_run=args.dry_run,
                    confirm=args.confirm,
                )
            return await close_due(
                venue=args.venue,
                asof=decision_at(
                    "crypto", args.asof - timedelta(days=1)
                ).to_pydatetime(),
                broker=broker,
                recorder=recorder,
                dry_run=args.dry_run,
                confirm=args.confirm,
            )
    finally:
        await broker.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
    except Exception as exc:  # noqa: BLE001 - never print transport/credential-bearing errors
        print(json.dumps({"success": False, "error_class": type(exc).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return int(
        any(
            row.get("status")
            in {
                "dust_remaining",
                "close_pending",
                "close_partial",
                "uncertain",
                "entry_not_sent",
                "entry_fill_pending",
            }
            for row in result.get("orders", [])
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
