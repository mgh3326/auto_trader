#!/usr/bin/env python3
"""KIS mock scalping quote WebSocket smoke (ROB-321 PR2).

Read-only: connects the quote/orderbook WebSocket, prints a bounded number of
parsed ticks/orderbook snapshots, and exits. Never places orders, never mutates,
never publishes. Default-disabled — requires KIS_MOCK_SCALPING_WS_ENABLED=true.

This smoke also RESOLVES the open question "does the KIS mock (:31000) WS serve
real-time quotes, or must quotes come from the live (:21000) WS?": run it once
with --account-mode kis_mock and once with kis_live and record which yields
ticks in docs/runbooks/kis-mock-scalping-ws-smoke.md.

Exit codes:
    0  - success (or disabled no-op)
    1  - unexpected exception
    2  - subscription ACK failure
    3  - connection not established
    4  - no quote events received within the window

Usage:
    KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_ws_smoke \
        --account-mode kis_mock --symbols 005930,000660 --max-events 5 --max-seconds 30
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from app.core.config import settings
from app.services.brokers.kis.mock_scalping_ws.market_stream import KISQuoteWebSocket
from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KIS quote WS read-only smoke")
    parser.add_argument(
        "--account-mode",
        choices=("kis_mock", "kis_live"),
        default="kis_mock",
        help="Quote WS environment (default: kis_mock).",
    )
    parser.add_argument(
        "--symbols",
        default="005930",
        help="Comma-separated KR stock codes (default: 005930).",
    )
    parser.add_argument("--max-events", type=int, default=5)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    return parser.parse_args(argv)


async def run_smoke(args: argparse.Namespace) -> int:
    if not settings.kis_mock_scalping_ws_enabled:
        logger.info(
            "KIS_MOCK_SCALPING_WS_ENABLED is not set; quote WS smoke is disabled (no-op)."
        )
        return 0

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    counters = {"ticks": 0, "books": 0}
    done = asyncio.Event()

    def _maybe_done() -> None:
        if counters["ticks"] + counters["books"] >= args.max_events:
            done.set()

    def _on_tick(tick: QuoteTick) -> None:
        counters["ticks"] += 1
        logger.info("tick %s last=%s ts=%s", tick.symbol, tick.last_price, tick.ts)
        _maybe_done()

    def _on_book(book: OrderBookSnapshot) -> None:
        counters["books"] += 1
        logger.info(
            "book %s bid=%s ask=%s bid_qty=%s ask_qty=%s",
            book.symbol,
            book.bid,
            book.ask,
            book.bid_qty,
            book.ask_qty,
        )
        _maybe_done()

    client = KISQuoteWebSocket(
        symbols=symbols,
        on_tick=_on_tick,
        on_book=_on_book,
        account_mode=args.account_mode,
    )
    client.is_running = True

    try:
        await client.connect_and_subscribe()
    except KISSubscriptionAckError as exc:
        logger.error("Quote subscription ACK failed: %s", exc)
        return 2
    except RuntimeError as exc:
        logger.error("Quote WS connection failed: %s", exc)
        return 3

    listen_task = asyncio.create_task(client.listen())
    try:
        await asyncio.wait_for(done.wait(), timeout=args.max_seconds)
    except TimeoutError:
        logger.info("max-seconds reached without hitting max-events")
    finally:
        await client.stop()
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task

    total = counters["ticks"] + counters["books"]
    logger.info(
        "quote WS smoke done: account_mode=%s ticks=%s books=%s",
        args.account_mode,
        counters["ticks"],
        counters["books"],
    )
    if total == 0:
        logger.error(
            "No quote events received on account_mode=%s — this host may not serve "
            "real-time quotes; retry with the other --account-mode.",
            args.account_mode,
        )
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(run_smoke(args))
    except Exception:
        logger.exception("Quote WS smoke failed with an unexpected exception")
        return 1


if __name__ == "__main__":
    sys.exit(main())
