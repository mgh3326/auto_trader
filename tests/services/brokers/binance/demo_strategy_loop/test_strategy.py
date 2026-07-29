"""ROB-993 — plugin strategy interface."""

from __future__ import annotations

from app.services.brokers.binance.demo_strategy_loop.strategy import (
    LastBarDirectionStrategy,
    NullStrategy,
    Signal,
    StrategyPlugin,
)
from research.nautilus_scalping.rob974_features import Bar4h


def test_null_strategy_always_returns_none() -> None:
    strategy = NullStrategy()
    assert strategy.strategy_id == "null"
    assert strategy.evaluate({}, decision_ts=1_700_000_000_000) is None
    assert strategy.evaluate({"XRPUSDT": ()}, decision_ts=1_700_000_000_000) is None


def test_signal_is_frozen_dataclass() -> None:
    signal = Signal(
        symbol="xrpusdt",
        side="BUY",
        decision_ts=1_700_000_000_000,
        strategy_id="test",
        reason="unit test",
    )
    assert signal.symbol == "xrpusdt"
    assert signal.sl_price is None
    assert signal.confidence is None
    try:
        signal.side = "SELL"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("Signal must be frozen")


# ---------------------------------------------------------------------------
# ROB-1145 — LastBarDirectionStrategy (first signal-emitting plugin).
# ---------------------------------------------------------------------------

_FOUR_HOUR_MS = 4 * 60 * 60 * 1000


def _bar(*, ts: int, open_: float, close: float) -> Bar4h:
    return Bar4h(
        ts=ts,
        close_ts=ts + _FOUR_HOUR_MS,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1.0,
        is_segment_start=False,
    )


def _aligned_ts() -> int:
    return 1_785_000_000_000 // _FOUR_HOUR_MS * _FOUR_HOUR_MS


def test_last_bar_direction_up_bar_emits_buy() -> None:
    ts = _aligned_ts()
    strategy = LastBarDirectionStrategy()
    assert strategy.strategy_id == "last-bar-direction"
    signal = strategy.evaluate(
        {"XRPUSDT": (_bar(ts=ts, open_=0.50, close=0.55),)},
        decision_ts=ts + _FOUR_HOUR_MS,
    )
    assert signal is not None
    assert signal.symbol == "XRPUSDT"
    assert signal.side == "BUY"
    assert signal.decision_ts == ts + _FOUR_HOUR_MS
    assert signal.strategy_id == "last-bar-direction"
    assert signal.sl_price is None and signal.tp_price is None


def test_last_bar_direction_down_bar_emits_sell() -> None:
    ts = _aligned_ts()
    signal = LastBarDirectionStrategy().evaluate(
        {"XRPUSDT": (_bar(ts=ts, open_=0.55, close=0.50),)},
        decision_ts=ts + _FOUR_HOUR_MS,
    )
    assert signal is not None
    assert signal.side == "SELL"


def test_last_bar_direction_flat_bar_emits_no_signal() -> None:
    ts = _aligned_ts()
    assert (
        LastBarDirectionStrategy().evaluate(
            {"XRPUSDT": (_bar(ts=ts, open_=0.50, close=0.50),)},
            decision_ts=ts + _FOUR_HOUR_MS,
        )
        is None
    )


def test_last_bar_direction_is_deterministic() -> None:
    """Same input -> same signal, twice (no state, no randomness)."""
    ts = _aligned_ts()
    bars = {"XRPUSDT": (_bar(ts=ts, open_=0.50, close=0.55),)}
    strategy = LastBarDirectionStrategy()
    first = strategy.evaluate(bars, decision_ts=ts + _FOUR_HOUR_MS)
    second = strategy.evaluate(bars, decision_ts=ts + _FOUR_HOUR_MS)
    assert first == second


def test_last_bar_direction_prefers_xrp_over_other_symbols() -> None:
    ts = _aligned_ts()
    signal = LastBarDirectionStrategy().evaluate(
        {
            "SOLUSDT": (_bar(ts=ts, open_=100.0, close=90.0),),
            "DOGEUSDT": (_bar(ts=ts, open_=0.10, close=0.09),),
            "XRPUSDT": (_bar(ts=ts, open_=0.50, close=0.55),),
        },
        decision_ts=ts + _FOUR_HOUR_MS,
    )
    assert signal is not None
    assert signal.symbol == "XRPUSDT"
    assert signal.side == "BUY"


def test_last_bar_direction_falls_through_priority_when_primary_absent() -> None:
    ts = _aligned_ts()
    signal = LastBarDirectionStrategy().evaluate(
        {
            "SOLUSDT": (_bar(ts=ts, open_=100.0, close=90.0),),
            "DOGEUSDT": (_bar(ts=ts, open_=0.10, close=0.11),),
        },
        decision_ts=ts + _FOUR_HOUR_MS,
    )
    assert signal is not None
    assert signal.symbol == "DOGEUSDT"
    assert signal.side == "BUY"


def test_last_bar_direction_ignores_stale_bar() -> None:
    """A bar whose close_ts != decision_ts is never used (H1: absence is
    NO_SIGNAL — never reach back for a stale bar)."""
    ts = _aligned_ts()
    assert (
        LastBarDirectionStrategy().evaluate(
            {"XRPUSDT": (_bar(ts=ts - _FOUR_HOUR_MS, open_=0.50, close=0.55),)},
            decision_ts=ts + _FOUR_HOUR_MS,
        )
        is None
    )


def test_last_bar_direction_empty_inputs_emit_no_signal() -> None:
    ts = _aligned_ts()
    strategy = LastBarDirectionStrategy()
    assert strategy.evaluate({}, decision_ts=ts + _FOUR_HOUR_MS) is None
    assert strategy.evaluate({"XRPUSDT": ()}, decision_ts=ts + _FOUR_HOUR_MS) is None


def test_last_bar_direction_never_targets_excluded_or_unlisted_symbol() -> None:
    """Even if a caller hands the plugin a priority list containing BTCUSDT
    (excluded: MIN_NOTIONAL 50 > 10 USDT cap) or an unlisted symbol, the
    plugin refuses to target it."""
    ts = _aligned_ts()
    strategy = LastBarDirectionStrategy(
        symbol_priority=("BTCUSDT", "ETHUSDT", "XRPUSDT")
    )
    signal = strategy.evaluate(
        {
            "BTCUSDT": (_bar(ts=ts, open_=60000.0, close=61000.0),),
            "ETHUSDT": (_bar(ts=ts, open_=3000.0, close=3100.0),),
            "XRPUSDT": (_bar(ts=ts, open_=0.55, close=0.50),),
        },
        decision_ts=ts + _FOUR_HOUR_MS,
    )
    assert signal is not None
    assert signal.symbol == "XRPUSDT"
    assert signal.side == "SELL"


def test_last_bar_direction_satisfies_strategy_plugin_protocol() -> None:
    def _accepts(plugin: StrategyPlugin) -> str:
        return plugin.strategy_id

    assert _accepts(LastBarDirectionStrategy()) == "last-bar-direction"
    assert _accepts(NullStrategy()) == "null"
