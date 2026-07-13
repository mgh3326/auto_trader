#!/usr/bin/env python3
"""KIS mock scalping daemon (ROB-321 PR4b).

Wires the read-only quote WebSocket (PR2) → supervisor (PR3) → exec bridge →
monitored executor (PR4a). Default-disabled and **dry-run by default**: orders
and ledger writes happen only when BOTH gates are set.

Gates (both default off):
    KIS_MOCK_SCALPING_WS_ENABLED  — run the daemon at all (else no-op, exit 0)
    KIS_MOCK_SCALPING_WS_CONFIRM  — submit real mock orders (else preview only)

Market data is read-only; orders are mock-only (`_place_order_impl(is_mock=True,
scalping_exit=…)`). The confirm path's fill confirmation is an operator-validated
open item — see docs/runbooks/kis-mock-scalping-smoke.md.

Exit codes: 0 success/disabled · 1 unexpected · 2 subscription ACK fail · 3 connect fail.

Usage:
    KIS_MOCK_SCALPING_WS_ENABLED=true uv run python -m scripts.kis_mock_scalping_daemon \
        --symbols 005930,000660 --account-mode kis_mock --max-seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from app.core.config import settings
from app.services.brokers.kis.mock_scalping.contract import ScalpingRiskLimits
from app.services.brokers.kis.mock_scalping_exec.adapters import (
    KisMockBroker,
    KisMockLedgerWriter,
    KisMockRiskGate,
)
from app.services.brokers.kis.mock_scalping_exec.executor import MockScalpingExecutor
from app.services.brokers.kis.mock_scalping_exec.ws_bridge import WsExecutionBridge
from app.services.brokers.kis.mock_scalping_ws.market_stream import KISQuoteWebSocket
from app.services.brokers.kis.mock_scalping_ws.quote_queue import QuoteEventQueue
from app.services.brokers.kis.mock_scalping_ws.supervisor import KisScalpingSupervisor
from app.services.kis_websocket_internal.protocol import KISSubscriptionAckError

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="KIS mock scalping daemon")
    p.add_argument("--symbols", default="005930")
    p.add_argument(
        "--account-mode", choices=("kis_mock", "kis_live"), default="kis_mock"
    )
    p.add_argument("--max-seconds", type=float, default=60.0)
    p.add_argument("--max-triggers", type=int, default=None)
    return p.parse_args(argv)


async def run_daemon(args: argparse.Namespace) -> int:
    if not settings.kis_mock_scalping_ws_enabled:
        logger.info(
            "KIS_MOCK_SCALPING_WS_ENABLED is not set; scalping daemon disabled (no-op)."
        )
        return 0

    confirm = bool(settings.kis_mock_scalping_ws_confirm)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    logger.info(
        "starting KIS mock scalping daemon: account_mode=%s symbols=%s confirm=%s",
        args.account_mode,
        symbols,
        confirm,
    )

    queue = QuoteEventQueue()
    supervisor = KisScalpingSupervisor(symbols=symbols)
    limits = ScalpingRiskLimits()
    # ROB-843 P1-1: the broker owns the final pre-send freshness re-check, so it
    # needs the same limits (age/spread caps) the risk gate uses.
    broker = KisMockBroker(get_state=supervisor.market_state, limits=limits)
    ledger = KisMockLedgerWriter()
    # ROB-843: the executor owns the final pre-send risk re-check. Wire a real
    # gate (fresh live-market + durable-ledger snapshot) so a confirmed entry
    # can never bypass it.
    risk_gate = KisMockRiskGate(get_state=supervisor.market_state)
    executor = MockScalpingExecutor(
        broker=broker, ledger=ledger, risk=risk_gate, limits=limits
    )
    bridge = WsExecutionBridge(executor=executor, limits=limits, confirm=confirm)
    ws = KISQuoteWebSocket(
        symbols=symbols,
        on_tick=queue.on_tick,
        on_book=queue.on_book,
        account_mode=args.account_mode,
    )
    ws.is_running = True

    triggers = {"n": 0}

    async def _on_trigger(trigger) -> None:
        triggers["n"] += 1
        await bridge.on_trigger(trigger)

    def _stop_when() -> bool:
        return args.max_triggers is not None and triggers["n"] >= args.max_triggers

    try:
        await ws.connect_and_subscribe()
    except KISSubscriptionAckError as exc:
        logger.error("quote subscription ACK failed: %s", exc)
        return 2
    except RuntimeError as exc:
        logger.error("quote WS connect failed: %s", exc)
        return 3

    producer = asyncio.create_task(ws.listen())
    consumer = asyncio.create_task(
        supervisor.run(queue.iterator, on_trigger=_on_trigger, stop_when=_stop_when)
    )
    try:
        await asyncio.wait(
            {producer, consumer},
            timeout=args.max_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        await ws.stop()
        for task in (producer, consumer):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    logger.info(
        "scalping daemon done: account_mode=%s triggers=%s confirm=%s",
        args.account_mode,
        triggers["n"],
        confirm,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(run_daemon(args))
    except Exception:
        logger.exception("scalping daemon failed with an unexpected exception")
        return 1


if __name__ == "__main__":
    sys.exit(main())
