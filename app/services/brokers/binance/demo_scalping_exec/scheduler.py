"""ROB-307 PR4 — default-OFF Demo scalping tick orchestration.

One tick runs the deterministic signal per allowlisted symbol and places a
bounded-monitor entry (``execute_monitored``) on each signal — open →
bounded TP/SL poll → MARKET-close → flat, all in-run. There is no held
position to reconcile across ticks. The executor's own live-ledger risk
re-check enforces capacity (one open lifecycle per product+symbol,
global/daily caps), so the tick never needs its own capacity bookkeeping.

Kill switch: ``enabled=False`` → no-op. Failure-only alerting: per-item
errors are collected (the tick never crashes mid-loop) and logged at
ERROR; a clean tick logs nothing alarming. No schedule is registered here
— this is a manually-invokable scaffold (see the TaskIQ task), and
activation is a separate operator gate.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    Product,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    data_age_seconds,
    spread_bps,
)
from app.services.brokers.binance.demo_scalping.order_intent import build_order_intent
from app.services.brokers.binance.demo_scalping.signal import (
    SignalConfig,
    evaluate_signal,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TickSummary:
    status: str  # "disabled" | "ran"
    # (product, symbol, status, reason_codes) — reason_codes surfaces the
    # executor's blocked/dry_run/reconciled/anomaly reason vocabulary
    # (ROB-907) so Prefect logs carry enough signal to diagnose all-blocked
    # runs without a DB read.
    entered: list[tuple[str, str, str, list[str]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "entered_count": len(self.entered),
            "error_count": len(self.errors),
            "entered": [list(e) for e in self.entered],
            "errors": list(self.errors),
        }


async def run_scalping_tick(
    *,
    executors: Mapping[str, Any],
    market_data: Any,
    symbols: Sequence[str],
    products: Sequence[Product],
    now: dt.datetime,
    limits: ScalpingRiskLimits | None = None,
    confirm: bool = False,
    enabled: bool = True,
    signal_config_for: Callable[[str], SignalConfig] | None = None,
    interval: str = "1m",
    limit: int = 50,
    monitor_kwargs: dict[str, Any] | None = None,
) -> TickSummary:
    """Run one scalping tick: signal → bounded-monitor entry per symbol.

    ``enabled=False`` is the kill switch (no-op). Entries always exit flat
    in-run (``execute_monitored``), so there is no held-position reconcile
    phase. Capacity (per-symbol / global / daily caps) is enforced by the
    executor's live-ledger risk re-check.
    """
    if not enabled:
        logger.info("demo scalping scheduler disabled — no-op tick")
        return TickSummary(status="disabled")

    limits = limits or ScalpingRiskLimits()
    entered: list[tuple[str, str, str, list[str]]] = []
    errors: list[str] = []
    monitor_kwargs = monitor_kwargs or {}

    for product in products:
        executor = executors.get(product)
        if executor is None:
            errors.append(f"enter {product}: no executor")
            continue
        for symbol in symbols:
            try:
                candles = await market_data.fetch_klines(
                    product, symbol, interval=interval, limit=limit
                )
                config = (
                    signal_config_for(product)
                    if signal_config_for is not None
                    else SignalConfig(allow_short=product == "usdm_futures")
                )
                signal = evaluate_signal(candles, config)
                if not signal.has_entry:
                    continue
                intent = build_order_intent(
                    signal,
                    product=product,
                    symbol=symbol,
                    limits=limits,
                    source_candle_close_time_ms=candles[-1].close_time_ms,
                    evaluated_at_ms=int(now.timestamp() * 1000),
                )
                if intent is None:
                    continue
                # ROB-315 0c / D4: feed the real spread + data-age snapshot into
                # the executor preflight so SPREAD_TOO_WIDE / STALE_DATA gates
                # fire (and entry spread@fill is captured). bookTicker reuse —
                # one extra read per entered signal, none on the no-signal path.
                book = await market_data.fetch_book_ticker(product, symbol)
                market = MarketConditions(
                    spread_bps=spread_bps(book),
                    data_age_seconds=data_age_seconds(
                        candles[-1], now_ms=int(now.timestamp() * 1000)
                    ),
                    spot_free_base_qty=Decimal("0"),
                )
                result = await executor.execute_monitored(
                    intent, confirm=confirm, market=market, **monitor_kwargs
                )
                entered.append(
                    (product, symbol, result.status, sorted(result.reason_codes))
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"enter {product}/{symbol}: {exc}")

    if errors:
        # Failure-only signal — a clean tick stays quiet.
        logger.error(
            "demo scalping tick completed with %d error(s): %s", len(errors), errors
        )
    return TickSummary(status="ran", entered=entered, errors=errors)
