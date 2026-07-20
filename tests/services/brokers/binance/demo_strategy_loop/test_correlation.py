"""ROB-993 — correlation-id spine determinism."""

from __future__ import annotations

from app.services.brokers.binance.demo_strategy_loop.correlation import (
    strategy_loop_correlation_id,
)


def test_correlation_id_deterministic() -> None:
    kwargs = {
        "strategy_loop_tag": "rob-993-strategy-loop",
        "symbol": "XRPUSDT",
        "side": "BUY",
        "decision_ts": 1_700_000_000_000,
    }
    assert strategy_loop_correlation_id(**kwargs) == strategy_loop_correlation_id(
        **kwargs
    )


def test_correlation_id_varies_with_inputs() -> None:
    base = strategy_loop_correlation_id(
        strategy_loop_tag="rob-993-strategy-loop",
        symbol="XRPUSDT",
        side="BUY",
        decision_ts=1_700_000_000_000,
    )
    different_symbol = strategy_loop_correlation_id(
        strategy_loop_tag="rob-993-strategy-loop",
        symbol="DOGEUSDT",
        side="BUY",
        decision_ts=1_700_000_000_000,
    )
    different_side = strategy_loop_correlation_id(
        strategy_loop_tag="rob-993-strategy-loop",
        symbol="XRPUSDT",
        side="SELL",
        decision_ts=1_700_000_000_000,
    )
    different_rung = strategy_loop_correlation_id(
        strategy_loop_tag="rob-993-strategy-loop",
        symbol="XRPUSDT",
        side="BUY",
        decision_ts=1_700_000_000_000,
        rung=1,
    )
    assert len({base, different_symbol, different_side, different_rung}) == 4


def test_correlation_id_is_namespaced() -> None:
    cid = strategy_loop_correlation_id(
        strategy_loop_tag="rob-993-strategy-loop",
        symbol="XRPUSDT",
        side="BUY",
        decision_ts=1_700_000_000_000,
    )
    assert cid.startswith("binance-demo-strategy-loop:rob-993-strategy-loop:")
