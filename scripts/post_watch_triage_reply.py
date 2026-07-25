"""Send watch-alert Claude triage replies through TradeNotifier.

This CLI is operator-host safe: it sends notifications only. It does not
mutate broker, order, watch, or session-context state.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.trade_notifier.runtime import (
    configure_trade_notifier_from_settings,
    shutdown_trade_notifier,
)


def build_message(
    *,
    symbol: str,
    market: str,
    event_uuid: str,
    triage_text: str,
) -> str:
    body = triage_text.strip()
    return "\n".join(
        [
            f"[watch triage] {symbol}",
            f"market: {market}",
            f"event: {event_uuid}",
            "",
            body,
        ]
    ).strip()


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


async def send_reply(
    *,
    symbol: str,
    market: str,
    event_uuid: str,
    triage_text: str,
) -> bool:
    configured = configure_trade_notifier_from_settings(
        log_context="Watch triage notifier"
    )
    if not configured:
        return False

    message = build_message(
        symbol=symbol,
        market=market,
        event_uuid=event_uuid,
        triage_text=triage_text,
    )
    try:
        return await get_trade_notifier().notify_agent_message(
            message,
            correlation_id=event_uuid,
            market_type=market,
            mirror_telegram=True,
        )
    finally:
        await shutdown_trade_notifier(log_context="Watch triage notifier")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror a watch-alert Claude triage reply to Discord and Telegram."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--event-uuid", required=True)
    parser.add_argument(
        "--text-file",
        default="-",
        help="File containing Claude result text; '-' reads stdin.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    triage_text = _read_text(args.text_file)
    ok = asyncio.run(
        send_reply(
            symbol=args.symbol,
            market=args.market,
            event_uuid=args.event_uuid,
            triage_text=triage_text,
        )
    )
    if ok:
        print("sent")
        return 0
    print("not sent", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
