"""ROB-286 — Scalper decision pure-function tests.

Matrix rows T18-T23. All assertions hit a pure function with no I/O.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.scalping.config import ScalperConfig
from app.services.scalping.decision import (
    Action,
    Entry,
    Exit,
    Hold,
    MarketSnapshot,
    SymbolState,
    compute_action,
)

_DEFAULT_CONFIG = ScalperConfig.default_for_testnet()


def _snapshot(
    *,
    symbol: str = "BTCUSDT",
    last_price: Decimal = Decimal("50000"),
    rsi_5m: float = 25.0,
    ema_20_5m: Decimal = Decimal("49500"),
    ema_50_5m: Decimal = Decimal("49000"),
    instrument_health: str = "healthy",
) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        last_price=last_price,
        rsi_5m=rsi_5m,
        ema_20_5m=ema_20_5m,
        ema_50_5m=ema_50_5m,
        instrument_health=instrument_health,
    )


def _state(
    *,
    open_position: bool = False,
    open_entry_client_order_id: str | None = None,
    tp_price: Decimal | None = None,
    sl_price: Decimal | None = None,
) -> SymbolState:
    return SymbolState(
        symbol="BTCUSDT",
        open_position=open_position,
        open_entry_client_order_id=open_entry_client_order_id,
        tp_price=tp_price,
        sl_price=sl_price,
    )


def test_compute_action_entry_buy() -> None:
    """T18 — Oversold + uptrend → BUY entry."""
    state = _state(open_position=False)
    snap = _snapshot(
        rsi_5m=20.0,  # oversold
        ema_20_5m=Decimal("49600"),  # short-term > long-term → uptrend
        ema_50_5m=Decimal("49000"),
        last_price=Decimal("50000"),
    )
    action: Action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Entry)
    assert action.side == "BUY"
    assert action.tp_price > snap.last_price
    assert action.sl_price < snap.last_price


def test_compute_action_tp_sell() -> None:
    """T19 — Open position + price ≥ TP → SELL exit (TP)."""
    state = _state(
        open_position=True,
        open_entry_client_order_id="entry-1",
        tp_price=Decimal("51000"),
        sl_price=Decimal("49000"),
    )
    snap = _snapshot(last_price=Decimal("51001"))
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Exit)
    assert action.reason == "take_profit"


def test_compute_action_sl_sell() -> None:
    """T20 — Open position + price ≤ SL → SELL exit (SL)."""
    state = _state(
        open_position=True,
        open_entry_client_order_id="entry-1",
        tp_price=Decimal("51000"),
        sl_price=Decimal("49000"),
    )
    snap = _snapshot(last_price=Decimal("48999"))
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Exit)
    assert action.reason == "stop_loss"


def test_compute_action_hold() -> None:
    """T21 — No signal + no open position → Hold."""
    state = _state(open_position=False)
    snap = _snapshot(
        rsi_5m=50.0,  # neutral
        ema_20_5m=Decimal("50000"),
        ema_50_5m=Decimal("50000"),
        last_price=Decimal("50000"),
    )
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Hold)


def test_compute_action_refuses_busy_symbol() -> None:
    """T22 — Symbol with open position refuses additional entry."""
    state = _state(
        open_position=True,
        open_entry_client_order_id="entry-1",
        tp_price=Decimal("51000"),
        sl_price=Decimal("49000"),
    )
    snap = _snapshot(
        rsi_5m=20.0, last_price=Decimal("49999")
    )  # oversold but neutral wrt TP/SL
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    # Not an entry — either Hold (waiting on TP/SL) or Exit, never a new Entry.
    assert not isinstance(action, Entry)


def test_compute_action_refuses_unhealthy_instrument() -> None:
    """T23 — instrument_health=manual_backfill_required refuses entry."""
    state = _state(open_position=False)
    snap = _snapshot(rsi_5m=20.0, instrument_health="manual_backfill_required")
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Hold)
    assert "manual_backfill_required" in action.reason or "unhealthy" in action.reason


def test_compute_action_refuses_degraded_instrument() -> None:
    """Degraded instrument is also refused entry."""
    state = _state(open_position=False)
    snap = _snapshot(rsi_5m=20.0, instrument_health="degraded")
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Hold)


def test_compute_action_no_signal_when_overbought() -> None:
    """RSI > 70 (overbought) → no entry."""
    state = _state(open_position=False)
    snap = _snapshot(
        rsi_5m=80.0,
        ema_20_5m=Decimal("49500"),
        ema_50_5m=Decimal("49000"),
    )
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Hold)


def test_compute_action_no_signal_when_downtrend() -> None:
    """EMA20 < EMA50 (downtrend) → no entry even if oversold."""
    state = _state(open_position=False)
    snap = _snapshot(
        rsi_5m=20.0,
        ema_20_5m=Decimal("48000"),
        ema_50_5m=Decimal("49000"),  # downtrend
    )
    action = compute_action(state=state, snapshot=snap, config=_DEFAULT_CONFIG)
    assert isinstance(action, Hold)
