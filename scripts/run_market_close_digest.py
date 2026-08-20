"""ROB-1297 market-close digest CLI.

Default is dry-run (print the card, no send). ``--send`` delivers through
TradeNotifier only.

    uv run python -m scripts.run_market_close_digest --market us --session-date 2026-08-19
    uv run python -m scripts.run_market_close_digest --market kr --send
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
)
from app.services.market_close_digest.service import run_market_close_digest

AC1_EXPECTED = {
    "market": "us",
    "session_date": "2026-08-19",
    "sell_count": 4,
    "buy_count": 1,
    "oversell_blocked": 2,
}


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("us", "kr", "crypto"), required=True)
    parser.add_argument("--session-date", type=_parse_date, default=None)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Deliver through TradeNotifier (default: dry-run, no send).",
    )
    return parser.parse_args(argv)


def _result_payload(result: Any) -> dict[str, Any]:
    snapshot = result.snapshot
    payload: dict[str, Any] = {
        "market": result.market,
        "session_date": result.session_date.isoformat(),
        "status": result.status,
        "sent": result.sent,
        "mutation_count": result.mutation_count,
        "message": result.message,
    }
    if snapshot is not None:
        payload["fill_count"] = snapshot.fill_count
        payload["buy_count"] = snapshot.buy_count
        payload["sell_count"] = snapshot.sell_count
        payload["oversell_blocked"] = len(snapshot.oversell_blocked)
        payload["auto_approve_count"] = snapshot.auto_approve_count
        payload["card_count"] = snapshot.card_count
        payload["flags"] = list(snapshot.flags)
    if (
        result.market == AC1_EXPECTED["market"]
        and result.session_date.isoformat() == AC1_EXPECTED["session_date"]
        and snapshot is not None
        and result.status not in {"skipped_holiday", "aborted_mutation"}
    ):
        payload["ac1_expected"] = AC1_EXPECTED
        payload["ac1_delta"] = {
            "sell_count": snapshot.sell_count - int(AC1_EXPECTED["sell_count"]),
            "buy_count": snapshot.buy_count - int(AC1_EXPECTED["buy_count"]),
            "oversell_blocked": len(snapshot.oversell_blocked)
            - int(AC1_EXPECTED["oversell_blocked"]),
        }
    return payload


async def _run(args: argparse.Namespace) -> int:
    notifier = None
    if args.send:
        configure_trade_notifier_from_settings(log_context="market-close-digest")
        notifier = get_trade_notifier()
    async with AsyncSessionLocal() as session:
        result = await run_market_close_digest(
            market=args.market,  # type: ignore[arg-type]
            session_date=args.session_date,
            now=datetime.now().astimezone() if args.session_date is None else None,
            session=session,
            send=bool(args.send),
            notifier=notifier,
        )
    print(
        json.dumps(_result_payload(result), ensure_ascii=False, indent=2, default=str)
    )
    if result.status == "aborted_mutation":
        return 2
    if result.status == "send_failed":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
