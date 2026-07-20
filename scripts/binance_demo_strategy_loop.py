"""ROB-993 — Binance Demo strategy loop CLI (default-disabled, manual entry point).

Real-time 4h-bar-close-triggered signal loop wired to
``BinanceFuturesDemoExecutionClient`` (ROB-298 safety boundaries inherited
unchanged: 1x leverage, reduceOnly close, ``demo-fapi.binance.com`` only,
XRP/DOGE/SOL). Strategy-pluggable: the default plugin is ``NullStrategy``
(always no signal) — the S3 signal-engine adapter (ROB-980) is a separate,
later commit, deliberately not wired here.

Modes (mutually exclusive; default with no flag prints guidance):

  1. **default-disabled** — ``BINANCE_DEMO_STRATEGY_LOOP_ENABLED`` unset
     or false: one log line, exit 0, zero HTTP/DB.
  2. ``--readiness`` — no-secret env readiness report. No HTTP, no
     credentials required.
  3. ``--once`` — one tick: fetch 1m bars, aggregate to 4h (H1 semantics),
     evaluate the strategy, act on a signal if any (dry-run unless
     ``--confirm``).
  4. ``--loop`` — poll forever at ``--poll-interval-seconds``, only acting
     once per newly-closed 4h bar (in-memory guard — a fresh process
     re-evaluates the current bar once more; this is a known limitation,
     not a persisted schedule). Foreground, operator-managed; not a
     daemon/scheduler registration.
  5. ``--paper-signal`` — inject a canned ``Signal`` (bypassing bar-fetch
     + strategy) and run one tick. This is the ROB-993 verification path:
     "페이퍼 신호로 e2e 스모크(주문 1건 데모 왕복)" — smoke-tests the
     kill-switch -> sizing -> execution -> ledger -> correlation_id ->
     forecast_save wiring end to end. Requires ``--confirm`` for a real
     Demo round trip (default is dry-run, zero HTTP mutation).

Kill switch (ROB-993 AC ④): this file's env gate (default off) + a
concurrent-position cap of 1 + a consecutive-stop-loss cap of 2 (this
loop's own trades, current UTC day) — both risk gates are re-evaluated
from the durable ledger on every tick, never held only in memory
(survives a process restart). Per the ROB-993 adversarial review
(verify-993-2256.md, Finding 1), these caps — and the $6-10 leg notional
— are hard lane invariants, not operator-tunable CLI flags:
``orchestrator.run_tick`` fails closed via
``KillSwitchLimitsNotLocked``/``LegNotionalCapNotLocked`` before any
network/DB call if a caller ever supplies a different value. See
``app.services.brokers.binance.demo_strategy_loop.kill_switch`` /
``.sizing``.

Demo-only: the execution client and the market-data client both enforce
the Futures Demo host allowlist (``demo-fapi.binance.com``) at the
transport layer on every request; this CLI additionally reconfirms both
resolved hosts once at startup via ``assert_demo_only`` before any bar
fetch or order call (AC ⑥).

No scheduler/TaskIQ/cron registration — this CLI is the only entry point.
Running it continuously (``--loop``) is an explicit, separately-decided
operator action (e.g. inside an operator-held tmux/screen session), not
an infra default.

Exit codes:
  0 — clean run: disabled, no signal, dry-run, kill-switch gated, or a
      reconciled round trip.
  1 — operator misconfiguration (missing env, missing credentials).
  2 — runtime failure (broker/ledger anomaly raised mid-lifecycle).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("scripts.binance_demo_strategy_loop")

_DEFAULT_SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
_DEFAULT_BASE_URL = "https://demo-fapi.binance.com"


def _truthy(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _evidence(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _trace(line: str) -> None:
    print(f"[rob-993] {line}")


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "ROB-993 Binance Demo strategy loop. Default disabled — set "
            "BINANCE_DEMO_STRATEGY_LOOP_ENABLED=true to opt in. Modes "
            "(mutually exclusive): --once / --loop / --paper-signal / "
            "--readiness."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    mode.add_argument(
        "--loop",
        action="store_true",
        help=(
            "Poll forever at --poll-interval-seconds. Foreground process; "
            "operator starts/stops it manually (no scheduler registration)."
        ),
    )
    mode.add_argument(
        "--paper-signal",
        dest="paper_signal",
        action="store_true",
        help=(
            "Inject a canned Signal (bypassing bar-fetch + strategy) and "
            "run one tick — smoke-tests kill-switch/execution/ledger/"
            "forecast wiring end to end. Pass --confirm for a real Demo "
            "round trip (default dry-run)."
        ),
    )
    mode.add_argument(
        "--readiness",
        action="store_true",
        help="No-secret env readiness report. No HTTP, no credentials required.",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(_DEFAULT_SYMBOLS),
        help=(
            "Comma-separated symbol list to aggregate/evaluate "
            "(default: XRPUSDT,DOGEUSDT,SOLUSDT)."
        ),
    )
    parser.add_argument(
        "--leverage",
        type=int,
        default=1,
        help="Leverage (default: 1). Any other value is rejected before any signed POST.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        dest="poll_interval_seconds",
        type=float,
        default=300.0,
        help="--loop poll cadence in seconds (default: 300).",
    )
    parser.add_argument(
        "--paper-symbol",
        dest="paper_symbol",
        default="XRPUSDT",
        help="--paper-signal symbol (default: XRPUSDT).",
    )
    parser.add_argument(
        "--paper-side",
        dest="paper_side",
        choices=["BUY", "SELL"],
        default="BUY",
        help="--paper-signal side (default: BUY).",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Operator gate: dispatch real Demo orders. Without this, every "
            "mode is dry-run (evidence only, zero broker submits)."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default INFO)."
    )
    return parser.parse_args(argv)


def _build_paper_signal(args: argparse.Namespace, *, decision_ts: int):
    from app.services.brokers.binance.demo_strategy_loop.strategy import Signal

    return Signal(
        symbol=args.paper_symbol.upper(),
        side=args.paper_side,
        decision_ts=decision_ts,
        strategy_id="rob-993-paper-signal",
        reason="operator-injected paper signal (ROB-993 e2e smoke)",
    )


def _report_outcome(outcome: Any) -> None:
    payload: dict[str, Any] = {
        "event": "strategy_loop_tick",
        "decision_ts": outcome.decision_ts,
        "signal": (
            None
            if outcome.signal is None
            else {
                "symbol": outcome.signal.symbol,
                "side": outcome.signal.side,
                "strategy_id": outcome.signal.strategy_id,
                "reason": outcome.signal.reason,
            }
        ),
        "blocked_reason": outcome.blocked_reason,
        "round_trip": (
            None
            if outcome.round_trip is None
            else {
                "open_client_order_id": outcome.round_trip.open_client_order_id,
                "close_client_order_id": outcome.round_trip.close_client_order_id,
                "symbol": outcome.round_trip.symbol,
                "side": outcome.round_trip.side,
                "qty": str(outcome.round_trip.qty),
                "reconciled": outcome.round_trip.reconciled,
            }
        ),
        "forecast_saved": outcome.forecast_saved,
        "forecast_error": outcome.forecast_error,
    }
    _evidence(payload)
    if outcome.round_trip is not None and outcome.round_trip.reconciled:
        _trace(
            "round_trip_reconciled "
            f"open={outcome.round_trip.open_client_order_id} "
            f"close={outcome.round_trip.close_client_order_id}"
        )
    elif outcome.blocked_reason:
        _trace(f"blocked reason={outcome.blocked_reason}")


async def _run_tick(
    args: argparse.Namespace,
    *,
    signal_override: Any | None = None,
    already_processed_decision_ts: int | None = None,
) -> tuple[int, int | None]:
    """Run one tick. Returns ``(exit_code, decision_ts)``."""
    from app.services.brokers.binance.demo_strategy_loop import bars as bars_mod
    from app.services.brokers.binance.demo_strategy_loop.kill_switch import (
        LOCKED_LIMITS,
    )
    from app.services.brokers.binance.demo_strategy_loop.orchestrator import (
        assert_demo_only,
        run_tick,
    )
    from app.services.brokers.binance.demo_strategy_loop.sizing import (
        LEG_NOTIONAL_CAP_MAX_USDT,
    )
    from app.services.brokers.binance.demo_strategy_loop.strategy import NullStrategy
    from app.services.brokers.binance.futures_demo.errors import (
        BinanceFuturesDemoMissingCredentials,
    )
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )

    try:
        execution = BinanceFuturesDemoExecutionClient.from_env()
    except BinanceFuturesDemoMissingCredentials as exc:
        logger.error("strategy loop refused: %s", exc)
        return 1, None

    import httpx

    exec_base_url = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", _DEFAULT_BASE_URL)
    venue_host = httpx.URL(exec_base_url).host
    market_client = bars_mod.build_bars_client()
    assert_demo_only(venue_host, market_client.base_url.host)

    from app.core.db import AsyncSessionLocal
    from app.services.brokers.binance.demo.ledger.service import (
        BinanceDemoLedgerService,
    )

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    # ROB-993 adversarial review (verify-993-2256.md, Finding 1): leg notional
    # and kill-switch caps are hard lane invariants, not CLI-settable — there
    # is deliberately no flag to override LOCKED_LIMITS / the notional cap.
    # ``run_tick`` also asserts this itself before any network/DB call, so
    # this is belt-and-suspenders, not the only enforcement point.
    limits = LOCKED_LIMITS

    try:
        async with AsyncSessionLocal() as session:
            ledger = BinanceDemoLedgerService(session)
            outcome = await run_tick(
                strategy=NullStrategy(),
                execution=execution,
                ledger=ledger,
                session=session,
                market_client=market_client,
                venue_host=venue_host,
                symbols=symbols,
                cap_usdt=LEG_NOTIONAL_CAP_MAX_USDT,
                leverage=args.leverage,
                kill_switch_limits=limits,
                now=_now_utc(),
                confirm=args.confirm,
                signal_override=signal_override,
                already_processed_decision_ts=already_processed_decision_ts,
            )
    except Exception as exc:  # noqa: BLE001 — surfaced as an anomaly evidence line
        _evidence({"event": "strategy_loop_anomaly", "error": str(exc)})
        logger.error("strategy loop tick failed: %s", exc)
        return 2, None
    finally:
        await execution.aclose()
        await market_client.aclose()

    _report_outcome(outcome)
    return 0, outcome.decision_ts


async def _run_once(args: argparse.Namespace) -> int:
    exit_code, _ = await _run_tick(args)
    return exit_code


async def _run_paper_signal(args: argparse.Namespace) -> int:
    decision_ts = int(_now_utc().timestamp() * 1000)
    signal = _build_paper_signal(args, decision_ts=decision_ts)
    exit_code, _ = await _run_tick(args, signal_override=signal)
    return exit_code


async def _run_loop(args: argparse.Namespace) -> int:
    _trace(f"loop_start poll_interval_seconds={args.poll_interval_seconds}")
    last_decision_ts: int | None = None
    try:
        while True:
            exit_code, decision_ts = await _run_tick(
                args, already_processed_decision_ts=last_decision_ts
            )
            if exit_code == 2:
                return exit_code
            if decision_ts is not None:
                last_decision_ts = decision_ts
            await asyncio.sleep(args.poll_interval_seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):
        _trace("loop_stopped operator_interrupt")
        return 0


async def _run(args: argparse.Namespace) -> int:
    if getattr(args, "readiness", False):
        enabled = _truthy(os.environ.get("BINANCE_DEMO_STRATEGY_LOOP_ENABLED"))
        _evidence(
            {
                "event": "strategy_loop_env_readiness",
                "BINANCE_DEMO_STRATEGY_LOOP_ENABLED": enabled,
                "symbols": args.symbols,
            }
        )
        return 0 if enabled else 1

    # Hard invariant: default-disabled. Checked AFTER argparse (so --help
    # works without the env set) but BEFORE any mode dispatch / HTTP / DB.
    if not _truthy(os.environ.get("BINANCE_DEMO_STRATEGY_LOOP_ENABLED")):
        logger.info(
            "strategy loop disabled — set BINANCE_DEMO_STRATEGY_LOOP_ENABLED=true to opt in"
        )
        return 0

    if args.once:
        return await _run_once(args)
    if args.loop:
        return await _run_loop(args)
    if args.paper_signal:
        return await _run_paper_signal(args)

    logger.info(
        "strategy loop enabled but no action requested. Pass --once for a "
        "single tick, --loop to poll continuously, --paper-signal for an "
        "e2e smoke round trip, or --readiness for an env-only report."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        logger.error("strategy loop failed: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
