"""ROB-317 — operator CLI for the Binance Demo WebSocket scalping daemon.

Default-disabled. Behaviour is entirely env-gated (see WsDaemonGates):

* ``BINANCE_DEMO_SCALPING_ENABLED`` + ``BINANCE_DEMO_SCALPING_WS_ENABLED`` —
  both must be truthy for the daemon to subscribe + evaluate triggers.
* ``BINANCE_DEMO_SCALPING_WS_CONFIRM`` — only when also truthy may real Demo
  orders be placed; otherwise the executor runs in dry-run (no mutation).

With the gates off it prints a ``disabled`` summary and exits 0 without
opening a socket. With the gates on it prints a ``running`` summary, subscribes
to the read-only fstream futures streams, and routes triggers to the
confirm-gated WsExecutionBridge. Demo hosts only; no live/testnet path; no
secrets printed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import DEFAULT_ALLOWLIST
from app.services.brokers.binance.demo_scalping_ws.config import WsDaemonGates
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    FuturesMarketStream,
    FuturesWsEvent,
    build_futures_stream_url,
)
from app.services.brokers.binance.demo_scalping_ws.supervisor import (
    ScalpingDaemonSupervisor,
    TriggerEvent,
)

OnTrigger = Callable[[TriggerEvent], Awaitable[None]]


def build_summary(gates: WsDaemonGates) -> dict[str, Any]:
    """Map resolved gates to a single-line startup summary.

    Printed once before the run loop, so ``subscribed`` is the pre-run snapshot
    (always False at print time); on the active path the socket is opened right
    after this line.
    """
    if not gates.daemon_active:
        return {
            "status": "disabled",
            "base_enabled": gates.base_enabled,
            "ws_enabled": gates.ws_enabled,
            "subscribed": False,
        }
    return {
        "status": "running",
        "base_enabled": gates.base_enabled,
        "ws_enabled": gates.ws_enabled,
        "mutation_allowed": gates.mutation_allowed,
        "subscribed": False,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-317 Binance Demo WebSocket scalping daemon. Default-disabled "
            "(zero side effects). Set BINANCE_DEMO_SCALPING_ENABLED=true and "
            "BINANCE_DEMO_SCALPING_WS_ENABLED=true to activate."
        )
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


logger = logging.getLogger("rob317.demo_scalping_ws_daemon")

_STREAMS = ("aggTrade", "bookTicker", "kline_1m")


def _real_source_factory(
    symbols: list[str],
) -> Callable[[], AsyncIterator[FuturesWsEvent]]:
    url = build_futures_stream_url(symbols, streams=_STREAMS)

    async def _factory() -> AsyncIterator[FuturesWsEvent]:
        async with FuturesMarketStream(url=url) as stream:
            async for ev in stream.events():
                yield ev

    return _factory


async def run_daemon(
    *,
    symbols: list[str],
    source_factory: Callable[[], AsyncIterator[FuturesWsEvent]] | None = None,
    on_trigger: OnTrigger | None = None,
    confirm: bool = False,
    clock: Callable[[], dt.datetime] | None = None,
) -> None:
    """Run the trigger pipeline.

    Production: ``on_trigger`` defaults to the env-built WsExecutionBridge
    (confirm passed in from gates). Tests inject ``source_factory`` +
    ``on_trigger`` to stay network/DB-free.
    """
    factory = source_factory or _real_source_factory(symbols)
    sup = ScalpingDaemonSupervisor(
        symbols=symbols, **({"clock": clock} if clock else {})
    )
    aclose: Callable[[], Awaitable[None]] | None = None
    if on_trigger is None:
        from app.services.brokers.binance.demo_scalping_exec.ws_bridge import (
            build_ws_execution_bridge_from_env,
        )

        bridge, aclose = await build_ws_execution_bridge_from_env(confirm=confirm)
        on_trigger = bridge
    try:
        await sup.run_with_reconnect(factory, on_trigger=on_trigger)
    finally:
        if aclose is not None:
            await aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    gates = WsDaemonGates.from_env()
    summary = build_summary(gates)
    print(json.dumps(summary, sort_keys=True))
    if not gates.daemon_active:
        return 0
    symbols = sorted(DEFAULT_ALLOWLIST)
    logger.info(
        "WS daemon active (mutation_allowed=%s) — turning triggers into real Demo orders",
        gates.mutation_allowed,
    )
    asyncio.run(run_daemon(symbols=symbols, confirm=gates.mutation_allowed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
