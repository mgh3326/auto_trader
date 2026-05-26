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

Bounded operator mode (for first-live validation): ``--max-runtime-sec`` and
``--max-triggers`` / ``--exit-after-first-trigger`` make the daemon exit cleanly
after a small, observable run. Real Demo orders need BOTH
``BINANCE_DEMO_SCALPING_WS_CONFIRM=true`` AND the explicit ``--confirm`` flag,
and confirmed runs must carry a trigger bound (fail-closed otherwise).
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
    parser.add_argument(
        "--max-runtime-sec",
        type=float,
        default=None,
        help="Wall-clock cap: exit cleanly after this many seconds.",
    )
    parser.add_argument(
        "--max-triggers",
        type=int,
        default=None,
        help="Exit cleanly after N triggers (bounded operator mode).",
    )
    parser.add_argument(
        "--exit-after-first-trigger",
        action="store_true",
        help="Shorthand for --max-triggers 1.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Required (in addition to BINANCE_DEMO_SCALPING_WS_CONFIRM=true) to "
            "place real Demo orders. Confirmed runs also require a trigger bound."
        ),
    )
    return parser.parse_args(argv)


def resolve_confirm(gates: WsDaemonGates, *, confirm_flag: bool) -> bool:
    """Whether real Demo order mutation is permitted.

    Requires BOTH the env gate (all three env vars, surfaced as
    ``gates.mutation_allowed``) AND the explicit ``--confirm`` flag. Either one
    missing → dry-run (fail-closed). The flag alone never enables mutation.
    """
    return gates.mutation_allowed and confirm_flag


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
    max_triggers: int | None = None,
    max_runtime_sec: float | None = None,
) -> int:
    """Run the trigger pipeline; return the number of triggers processed.

    Bounded operator mode: ``max_triggers`` stops cleanly after N triggers
    (count survives reconnects); ``max_runtime_sec`` is a wall-clock cap that
    cancels even a stream blocked with no events. Both default off (unbounded).

    Production: ``on_trigger`` defaults to the env-built WsExecutionBridge
    (confirm passed in). Tests inject ``source_factory`` + ``on_trigger`` to
    stay network/DB-free.
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

    triggers = 0
    sink = on_trigger

    async def _counting(trigger: TriggerEvent) -> None:
        nonlocal triggers
        await sink(trigger)
        triggers += 1

    def _stop_when() -> bool:
        return max_triggers is not None and triggers >= max_triggers

    try:
        runner = sup.run_with_reconnect(
            factory, on_trigger=_counting, stop_when=_stop_when
        )
        if max_runtime_sec is not None:
            try:
                await asyncio.wait_for(runner, timeout=max_runtime_sec)
            except TimeoutError:
                logger.info(
                    "daemon exiting: --max-runtime-sec=%.3f reached (triggers=%d)",
                    max_runtime_sec,
                    triggers,
                )
        else:
            await runner
    finally:
        if aclose is not None:
            await aclose()
    return triggers


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=args.log_level)
    gates = WsDaemonGates.from_env()
    summary = build_summary(gates)
    print(json.dumps(summary, sort_keys=True))
    if not gates.daemon_active:
        return 0
    max_triggers = 1 if args.exit_after_first_trigger else args.max_triggers
    confirm = resolve_confirm(gates, confirm_flag=args.confirm)
    if confirm and max_triggers is None:
        logger.error(
            "confirmed mode requires a trigger bound: pass --max-triggers N or "
            "--exit-after-first-trigger (fail-closed; no order placed)"
        )
        return 2
    symbols = sorted(DEFAULT_ALLOWLIST)
    logger.info(
        "WS daemon active: confirm=%s max_triggers=%s max_runtime_sec=%s",
        confirm,
        max_triggers,
        args.max_runtime_sec,
    )
    asyncio.run(
        run_daemon(
            symbols=symbols,
            confirm=confirm,
            max_triggers=max_triggers,
            max_runtime_sec=args.max_runtime_sec,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
