"""ROB-286 — Deterministic scalping decision (pure function).

``compute_action(state, snapshot, config) → Action`` is the entire
trading logic — no LLM, no external approval, no I/O. Inputs are the
per-symbol state (open position, TP/SL prices) and a market snapshot
(price, RSI, EMAs, instrument health).

Decision rules:

* Open position present → check TP/SL trigger; otherwise Hold.
* No open position, instrument healthy, RSI ≤ rsi_oversold, EMA20 >
  EMA50 (uptrend) → Entry(BUY) with TP/SL anchored on entry price.
* Otherwise Hold.

The function MUST stay pure (testable in isolation, no async, no
asyncio) — the runner is what wires it to I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.scalping.config import ScalperConfig


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Inputs to the decision function — price + indicator values."""

    symbol: str
    last_price: Decimal
    rsi_5m: float
    ema_20_5m: Decimal
    ema_50_5m: Decimal
    instrument_health: str = (
        "healthy"  # one of healthy/degraded/rate_limited/manual_backfill_required
    )


@dataclass(frozen=True, slots=True)
class SymbolState:
    """Per-symbol state assembled from the ledger before decision time."""

    symbol: str
    open_position: bool
    open_entry_client_order_id: str | None
    tp_price: Decimal | None
    sl_price: Decimal | None


@dataclass(frozen=True, slots=True)
class Hold:
    """Action: do nothing this tick."""

    reason: str


@dataclass(frozen=True, slots=True)
class Entry:
    """Action: open a new position."""

    side: str  # "BUY" (MVP is long-only on spot)
    tp_price: Decimal
    sl_price: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class Exit:
    """Action: close an existing position."""

    reason: str  # "take_profit" | "stop_loss" | "manual"


Action = Hold | Entry | Exit


def compute_action(
    *,
    state: SymbolState,
    snapshot: MarketSnapshot,
    config: ScalperConfig,
) -> Action:
    """Decide what to do for ``state.symbol`` given the snapshot."""
    # If we have an open position, the only valid moves are Exit (TP/SL
    # trigger) or Hold (waiting on trigger). Never open a second position.
    if state.open_position:
        if state.tp_price is not None and snapshot.last_price >= state.tp_price:
            return Exit(reason="take_profit")
        if state.sl_price is not None and snapshot.last_price <= state.sl_price:
            return Exit(reason="stop_loss")
        return Hold(reason="open_position_waiting_for_trigger")

    # No open position. First gate: instrument health.
    if snapshot.instrument_health != "healthy":
        return Hold(
            reason=(
                f"instrument_health={snapshot.instrument_health} "
                "(unhealthy; refusing entry)"
            )
        )

    # Second gate: RSI must be oversold (mean-reversion).
    if snapshot.rsi_5m > config.rsi_oversold:
        return Hold(reason=f"rsi_5m={snapshot.rsi_5m} above oversold threshold")
    if snapshot.rsi_5m >= config.rsi_overbought:
        return Hold(reason=f"rsi_5m={snapshot.rsi_5m} overbought; no entry")

    # Third gate: trend must be up (EMA20 > EMA50).
    if snapshot.ema_20_5m <= snapshot.ema_50_5m:
        return Hold(reason="ema20 <= ema50 (downtrend or flat)")

    # All gates passed → compute TP/SL anchored on last_price.
    price = snapshot.last_price
    tp = price * (Decimal("1") + config.tp_pct)
    sl = price * (Decimal("1") - config.sl_pct)
    return Entry(
        side="BUY",
        tp_price=tp,
        sl_price=sl,
        reason=(
            f"rsi_5m={snapshot.rsi_5m} oversold + ema20 > ema50 uptrend "
            f"(price={price} tp={tp} sl={sl})"
        ),
    )
