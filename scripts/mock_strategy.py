"""Manual ROB-1474 paper strategy. No scheduler registration or live venue."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("run", "close-due", "status"))
    result.add_argument("--venue", choices=("alpaca_paper",), required=True)
    result.add_argument(
        "--asof",
        type=date.fromisoformat,
        required=True,
        help="execution session date (New York)",
    )
    result.add_argument("--dry-run", action="store_true")
    result.add_argument(
        "--confirm",
        action="store_true",
        help="explicit permission for each writing invocation",
    )
    result.add_argument("--horizon", type=int, choices=(1, 3, 5, 10), default=5)
    result.add_argument("--top-n", type=int, default=10)
    result.add_argument("--notional", type=Decimal, default=Decimal("10000"))
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(
            os.environ.get("STOCK_SELECTION_CACHE", "research/stock_selection/.cache")
        ),
    )
    result.add_argument(
        "--asset-fixture",
        type=Path,
        help="dry-run only: local paper account_ref and assets JSON; no broker/DB",
    )
    result.add_argument(
        "--account-ref", help="optional status scope; required for close-due --dry-run"
    )
    return result


async def execute(args: argparse.Namespace) -> dict:
    from app.services.mock_strategy.recorder import MockStrategyRecorder
    from app.services.mock_strategy.us import UsPaperStrategy, plan_orders, projection
    from app.services.mock_strategy.us_signals import build_selection, session_times

    if args.top_n < 1 or not args.notional.is_finite() or args.notional <= 0:
        raise ValueError("positive top-n and finite positive notional required")
    if args.asset_fixture and (not args.dry_run or args.command != "run"):
        raise ValueError("asset fixture is run --dry-run only")
    if args.command == "run" and args.dry_run:
        from app.services.brokers.alpaca.schemas import Asset

        if args.asset_fixture is None:
            raise ValueError(
                "run --dry-run requires --asset-fixture (offline eligibility evidence)"
            )
        fixture = json.loads(args.asset_fixture.read_text())
        assets = [Asset.model_validate(row) for row in fixture["assets"]]
        signals = build_selection(
            args.cache_dir, args.asof, horizon=args.horizon, top_n=args.top_n
        )
        plan = plan_orders(
            signals,
            {a.symbol: a for a in assets},
            account_ref=fixture["account_ref"],
            notional=args.notional,
        )
        return {
            "venue": args.venue,
            "asof": args.asof.isoformat(),
            "dry_run": True,
            "strategy_id": signals.strategy_id,
            "signal_date": signals.signal_date.isoformat(),
            "orders": [projection(row) for row in plan.orders],
            "skipped": list(plan.skipped),
        }
    recorder = MockStrategyRecorder()
    if args.command == "status":
        rows = await recorder.list_orders(
            venue=args.venue, account_ref=args.account_ref
        )
        return {
            "venue": args.venue,
            "asof": args.asof.isoformat(),
            "orders": [
                {
                    **projection(row),
                    "opened_at": row.opened_at.isoformat() if row.opened_at else None,
                    "closed_at": row.closed_at.isoformat() if row.closed_at else None,
                    "realized_pnl": str(row.realized_pnl)
                    if row.realized_pnl is not None
                    else None,
                }
                for row in rows
                if row.meta.get("asof", "9999") <= args.asof.isoformat()
            ],
        }
    if args.dry_run:
        if not args.account_ref:
            raise ValueError("close-due --dry-run requires --account-ref")
        start, _, _ = session_times(args.asof)
        rows = await recorder.list_due(
            venue=args.venue, account_ref=args.account_ref, asof=start
        )
        return {
            "venue": args.venue,
            "dry_run": True,
            "orders": [projection(row) for row in rows],
            "note": "due entries only; broker terminal-entry and fresh position checks required before close",
        }
    if args.confirm is not True:
        raise ValueError("--confirm required for paper mutations")
    from app.services.mock_strategy.us_broker import UsPaperBroker

    strategy = UsPaperStrategy(recorder, UsPaperBroker())
    if args.command == "close-due":
        return await strategy.close_due(asof=args.asof, confirm=True)
    return await strategy.run(
        asof=args.asof,
        horizon=args.horizon,
        top_n=args.top_n,
        notional=args.notional,
        selection=lambda: build_selection(
            args.cache_dir, args.asof, horizon=args.horizon, top_n=args.top_n
        ),
        confirm=True,
    )


def main(argv: list[str] | None = None) -> int:
    venue_probe = argparse.ArgumentParser(add_help=False)
    venue_probe.add_argument("--venue")
    route, _ = venue_probe.parse_known_args(argv)
    if route.venue in {
        "binance_spot_demo",
        "binance_futures_demo",
        "alpaca_paper_crypto",
    }:
        from scripts.mock_strategy_crypto import main as crypto_main

        return crypto_main(argv)
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
    except Exception as exc:
        # Transport/database exceptions can include credentials or DSNs. Never
        # print their messages or chained traceback at this operator boundary.
        print(
            json.dumps(
                {"error": "mock_strategy_failed", "error_class": type(exc).__name__}
            )
        )
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return int(
        any(
            row.get("status") in {"sending", "uncertain"}
            for row in result.get("orders", [])
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
