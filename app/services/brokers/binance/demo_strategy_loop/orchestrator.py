"""ROB-993 — Binance Demo strategy loop orchestrator.

Ties together, in order: 1m bar fetch -> H1 4h aggregation -> plugin
strategy evaluation -> kill switch -> sizing -> execution round trip ->
ledger -> correlation_id -> forecast_save. No scheduler/TaskIQ
registration — ``scripts/binance_demo_strategy_loop.py`` is the only
entry point, run manually by an operator (activation is a separate,
later decision per the ROB-993 ticket).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoUnsupportedSymbol,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.futures_demo.host_allowlist import (
    assert_futures_demo_host,
)

from . import bars as bars_mod
from .correlation import strategy_loop_correlation_id
from .execution import RoundTripBlocked, RoundTripResult, execute_signal_round_trip
from .kill_switch import (
    StrategyLoopKillSwitchLimits,
    assert_kill_switch_limits_locked,
    build_kill_switch_snapshot,
    evaluate_kill_switch,
)
from .sizing import (
    FuturesSizingBlocked,
    assert_leg_notional_cap_locked,
    assert_symbol_allowed,
    compute_futures_demo_order_qty,
    fetch_reference_price,
    fetch_symbol_filters,
    quantize_qty,
)
from .strategy import Signal, StrategyPlugin

logger = logging.getLogger(__name__)

STRATEGY_LOOP_TAG = "rob-993-strategy-loop"
_CID_PREFIX = "rob-993-"


@dataclass(frozen=True)
class TickOutcome:
    decision_ts: int | None
    signal: Signal | None
    round_trip: RoundTripResult | None
    blocked_reason: str | None = None
    forecast_saved: bool = False
    forecast_error: str | None = None


def _new_cid() -> str:
    return f"{_CID_PREFIX}{uuid.uuid4().hex[:16]}"


def assert_demo_only(*hosts: str) -> None:
    """Explicit live-host fail-closed reconfirm (ROB-993 AC ⑥).

    The execution client and the market-data client already enforce the
    Futures Demo host allowlist at the transport layer on every request;
    this is a belt-and-suspenders check run once at loop start so a
    misconfigured base URL is refused before any bar fetch or order call.
    """
    for host in hosts:
        assert_futures_demo_host(host)


async def collect_4h_bars(
    market_client: httpx.AsyncClient,
    symbols: tuple[str, ...],
    *,
    minute_limit: int = 500,
) -> dict[str, tuple[bars_mod.Bar4h, ...]]:
    """Fetch 1m bars per symbol and aggregate to complete-only 4h bars.

    Reuses ``research.nautilus_scalping.rob974_features.build_complete_4h``
    (H1) directly — a symbol missing minutes for a bucket simply has no
    ``Bar4h`` for that bucket (NO_SIGNAL == absence, never forward-filled).
    """
    bars_by_symbol: dict[str, tuple[bars_mod.Bar4h, ...]] = {}
    for symbol in symbols:
        minute_bars = await bars_mod.fetch_1m_minute_bars(
            market_client, symbol, limit=minute_limit
        )
        bars_by_symbol[symbol] = bars_mod.build_complete_4h(minute_bars)
    return bars_by_symbol


def _to_forecast_symbol(symbol: str) -> str:
    """Render a Binance USDT-quoted symbol as the ``<QUOTE>-<BASE>`` form
    ``forecast_service._normalize_symbol`` expects for ``instrument_type=
    "crypto"`` (mirrors Upbit market codes, e.g. ``KRW-BTC``). Without the
    dash, ``_normalize_symbol`` treats a bare symbol as a base asset and
    silently prepends ``KRW-`` (Upbit default), which would mis-tag a
    Binance pair as ``KRW-XRPUSDT`` — caught live during the ROB-993 e2e
    smoke. ``USDT`` is already a recognized quote currency in that module.
    """
    base = symbol.upper().removesuffix("USDT")
    return f"USDT-{base}" if base else symbol.upper()


async def _save_forecast(
    *,
    signal: Signal,
    correlation_id: str,
    round_trip: RoundTripResult,
    now: dt.datetime,
) -> tuple[bool, str | None]:
    """Best-effort learning-loop spine write. Never fails the (already
    executed) trade — a forecast_save error is reported, not raised."""
    from app.core.db import AsyncSessionLocal
    from app.services.trade_journal.forecast_service import (
        ForecastValidationError,
        save_forecast,
    )

    try:
        async with AsyncSessionLocal() as db:
            await save_forecast(
                db,
                created_by=STRATEGY_LOOP_TAG,
                symbol=_to_forecast_symbol(signal.symbol),
                instrument_type="crypto",
                forecast_target={
                    # This PR's round trip opens then immediately closes
                    # (infra-proof, not a held position) — there is no
                    # forward-looking price claim to resolve later, so this
                    # is written as a placeholder ("no_resolvable_forecast"
                    # kind, matching the existing ROB-816 closed_no_claim
                    # convention) rather than a "price_target" kind. A real
                    # strategy that holds to TP/SL (S3 adapter) should use
                    # "price_target" with the actual entry/TP/SL levels.
                    "kind": "no_resolvable_forecast",
                    "side": signal.side,
                    "strategy_id": signal.strategy_id,
                    "reason": signal.reason,
                    "decision_ts": signal.decision_ts,
                    "open_client_order_id": round_trip.open_client_order_id,
                    "close_client_order_id": round_trip.close_client_order_id,
                },
                probability=signal.confidence if signal.confidence is not None else 0.5,
                review_date=now.date().isoformat(),
                correlation_id=correlation_id,
            )
            await db.commit()
        return True, None
    except ForecastValidationError as exc:
        logger.warning("strategy_loop forecast_save validation error: %s", exc)
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — evidence-only, must not mask trade success
        logger.exception("strategy_loop forecast_save failed")
        return False, str(exc)


async def run_tick(
    *,
    strategy: StrategyPlugin,
    execution: BinanceFuturesDemoExecutionClient,
    ledger: BinanceDemoLedgerService,
    session: AsyncSession,
    market_client: httpx.AsyncClient,
    venue_host: str,
    symbols: tuple[str, ...],
    cap_usdt: Decimal,
    leverage: int,
    kill_switch_limits: StrategyLoopKillSwitchLimits,
    now: dt.datetime,
    confirm: bool,
    signal_override: Signal | None = None,
    already_processed_decision_ts: int | None = None,
) -> TickOutcome:
    """Run one strategy-loop tick.

    ``signal_override`` bypasses bar-fetch + strategy evaluation with a
    caller-supplied :class:`Signal` — this is the ``--paper-signal`` smoke
    mode, which exercises the kill-switch/sizing/execution/ledger/forecast
    wiring end to end without waiting for a real 4h bar close.

    ``already_processed_decision_ts`` lets a continuous ``--loop`` caller
    skip re-evaluating a 4h bar it has already acted on (or declined) —
    the loop polls faster than the 4h cadence, so without this a strategy
    could otherwise fire on the same bar close every poll interval.

    Raises ``LegNotionalCapNotLocked`` / ``KillSwitchLimitsNotLocked``
    (ROB-993 adversarial review Finding 1) before any network/DB call if
    ``cap_usdt`` or ``kill_switch_limits`` deviate from this lane's hard
    safety invariant — they are not operator-tunable dials.
    """
    assert_leg_notional_cap_locked(cap_usdt)
    assert_kill_switch_limits_locked(kill_switch_limits)

    if signal_override is not None:
        decision_ts = signal_override.decision_ts
        signal = signal_override
    else:
        bars_by_symbol = await collect_4h_bars(market_client, symbols)
        # ROB-993 adversarial review (verify-993-2256.md, Finding 5): the
        # strategy must only be invoked when EVERY symbol in the universe
        # has a complete 4h bar ending at the exact same close_ts. Picking
        # the freshest symbol's close_ts (a bare ``max()``) would silently
        # hand the plugin a snapshot where a lagging symbol's bar is stale
        # or absent — the opposite of H1's synchronized-plane semantics
        # (``compute_common_features`` requires the same intersection).
        latest_close_ts_per_symbol = {
            symbol: bars[-1].close_ts for symbol, bars in bars_by_symbol.items() if bars
        }
        if len(latest_close_ts_per_symbol) < len(symbols):
            return TickOutcome(
                decision_ts=max(latest_close_ts_per_symbol.values(), default=None),
                signal=None,
                round_trip=None,
                blocked_reason="no_complete_4h_bar",
            )
        synchronized_close_ts = set(latest_close_ts_per_symbol.values())
        if len(synchronized_close_ts) != 1:
            return TickOutcome(
                decision_ts=max(synchronized_close_ts),
                signal=None,
                round_trip=None,
                blocked_reason="missing_complete_4h_bar",
            )
        decision_ts = synchronized_close_ts.pop()
        if decision_ts == already_processed_decision_ts:
            return TickOutcome(
                decision_ts=decision_ts,
                signal=None,
                round_trip=None,
                blocked_reason="already_processed",
            )
        signal = strategy.evaluate(bars_by_symbol, decision_ts=decision_ts)

    if signal is None:
        return TickOutcome(
            decision_ts=decision_ts,
            signal=None,
            round_trip=None,
            blocked_reason="no_signal",
        )

    symbol = signal.symbol.upper()
    try:
        assert_symbol_allowed(symbol)
    except BinanceFuturesDemoUnsupportedSymbol as exc:
        return TickOutcome(
            decision_ts=decision_ts,
            signal=signal,
            round_trip=None,
            blocked_reason=f"symbol_rejected:{exc}",
        )

    kill_switch_snapshot = await build_kill_switch_snapshot(
        ledger, strategy_loop_tag=STRATEGY_LOOP_TAG, now=now
    )
    decision = evaluate_kill_switch(
        snapshot=kill_switch_snapshot, limits=kill_switch_limits
    )
    if not decision.allowed:
        return TickOutcome(
            decision_ts=decision_ts,
            signal=signal,
            round_trip=None,
            blocked_reason=f"kill_switch:{','.join(decision.reason_codes)}",
        )

    filters = await fetch_symbol_filters(market_client, symbol)
    ref_price = await fetch_reference_price(market_client, symbol)
    sizing = compute_futures_demo_order_qty(
        symbol=symbol,
        target_notional_usdt=cap_usdt,
        price=ref_price,
        min_notional=filters["min_notional"],
        step_size=filters["step_size"],
        cap_usdt=cap_usdt,
    )
    if isinstance(sizing, FuturesSizingBlocked):
        return TickOutcome(
            decision_ts=decision_ts,
            signal=signal,
            round_trip=None,
            blocked_reason=f"sizing_blocked:{sizing.reason}",
        )
    qty = quantize_qty(
        sizing.qty,
        step_size=filters["step_size"],
        quantity_precision=filters["quantity_precision"],
    )

    if not confirm:
        return TickOutcome(
            decision_ts=decision_ts,
            signal=signal,
            round_trip=None,
            blocked_reason="dry_run",
        )

    instrument_id = await ledger.resolve_or_create_instrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
    )
    correlation_id = strategy_loop_correlation_id(
        strategy_loop_tag=STRATEGY_LOOP_TAG,
        symbol=symbol,
        side=signal.side,
        decision_ts=decision_ts,
    )

    try:
        round_trip = await execute_signal_round_trip(
            execution=execution,
            ledger=ledger,
            session=session,
            signal=signal,
            instrument_id=instrument_id,
            venue_host=venue_host,
            qty=qty,
            notional_usdt=sizing.notional_usdt,
            leverage=leverage,
            strategy_loop_tag=STRATEGY_LOOP_TAG,
            correlation_id=correlation_id,
            open_client_order_id=_new_cid(),
            close_client_order_id=_new_cid(),
            close_step_size=filters["step_size"],
            close_quantity_precision=filters["quantity_precision"],
        )
    except RoundTripBlocked as exc:
        return TickOutcome(
            decision_ts=decision_ts,
            signal=signal,
            round_trip=None,
            blocked_reason=f"kill_switch:exposure_slot_taken:{exc.reason}",
        )

    forecast_saved, forecast_error = await _save_forecast(
        signal=signal, correlation_id=correlation_id, round_trip=round_trip, now=now
    )
    return TickOutcome(
        decision_ts=decision_ts,
        signal=signal,
        round_trip=round_trip,
        forecast_saved=forecast_saved,
        forecast_error=forecast_error,
    )
