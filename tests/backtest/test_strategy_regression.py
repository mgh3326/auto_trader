"""Regression tests for strategy behavior parity.

These tests lock in the legacy strategy behavior from commit 8177d23
to prevent regressions during modularization refactoring.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import strategy


# =============================================================================
# Legacy Buy Weight Reference Helper (from commit 8177d23)
# =============================================================================


def legacy_buy_weight(
    symbol: str, bull_flags: dict[str, bool], market_state: dict[str, float]
) -> tuple[float, str | None]:
    """Pure reference helper that preserves the original if/elif order exactly.

    This is the parity oracle - intentionally repetitive and not a refactoring target.
    """
    # Extract flags for readability (matching original variable names)
    dual_rsi_oversold = bull_flags["dual_rsi_oversold"]
    macd_histogram_positive = bull_flags["macd_histogram_positive"]
    close_below_bb_lower = bull_flags["close_below_bb_lower"]
    ema_fast_above_slow = bull_flags["ema_fast_above_slow"]
    momentum_positive = bull_flags["momentum_positive"]
    volume_above_avg = bull_flags["volume_above_avg"]

    # Setup conditions (matching original logic)
    pure_reversion_buy = (
        dual_rsi_oversold
        and close_below_bb_lower
        and volume_above_avg
        and not macd_histogram_positive
    )
    pure_trend_buy = (
        macd_histogram_positive
        and ema_fast_above_slow
        and momentum_positive
        and volume_above_avg
        and not dual_rsi_oversold
    )
    strong_reversion_buy = (
        dual_rsi_oversold and close_below_bb_lower and macd_histogram_positive
    )

    avg_rsi = market_state.get("avg_rsi", 50.0)
    avg_rsi_change = market_state.get("avg_rsi_change", 0.0)
    params = strategy.PARAMS

    # Original if/elif chain from 8177d23 (preserved verbatim)
    if strong_reversion_buy:
        return params[
            "strong_reversion_position_size"
        ], "strong_reversion_position_size"

    if (
        pure_trend_buy
        and symbol == "BTC"
        and avg_rsi >= params["btc_trend_hot_rsi_level"]
        and avg_rsi_change < params["btc_trend_stall_change"]
    ):
        return (
            params["btc_hot_stall_trend_position_size"],
            "btc_hot_stall_trend_position_size",
        )

    if (
        pure_trend_buy
        and symbol == "BTC"
        and params["btc_mid_hot_rsi_low"] <= avg_rsi < params["btc_mid_hot_rsi_high"]
        and avg_rsi_change >= params["btc_extreme_accel_change"]
    ):
        return (
            params["btc_mid_hot_accel_trend_position_size"],
            "btc_mid_hot_accel_trend_position_size",
        )

    if (
        pure_trend_buy
        and symbol == "SOL"
        and params["sol_hot_stall_rsi_low"]
        <= avg_rsi
        < params["sol_hot_stall_rsi_high"]
        and avg_rsi_change < params["sol_hot_stall_change"]
    ):
        return (
            params["sol_hot_stall_trend_position_size"],
            "sol_hot_stall_trend_position_size",
        )

    if (
        pure_trend_buy
        and symbol == "LINK"
        and params["link_hot_stall_rsi_low"]
        <= avg_rsi
        < params["link_hot_stall_rsi_high"]
        and avg_rsi_change < params["link_hot_stall_change"]
    ):
        return (
            params["link_hot_stall_trend_position_size"],
            "link_hot_stall_trend_position_size",
        )

    if (
        pure_reversion_buy
        and symbol == "XRP"
        and avg_rsi < params["xrp_stalled_washout_rsi"]
        and avg_rsi_change > params["xrp_stalled_washout_change"]
    ):
        return (
            params["xrp_stalled_washout_reversion_position_size"],
            "xrp_stalled_washout_reversion_position_size",
        )

    if (
        pure_reversion_buy
        and symbol == "SOL"
        and avg_rsi > params["sol_mild_reversion_rsi"]
    ):
        return (
            params["sol_mild_reversion_position_size"],
            "sol_mild_reversion_position_size",
        )

    if (
        pure_trend_buy
        and symbol == "SOL"
        and avg_rsi < params["sol_low_breadth_rsi"]
        and avg_rsi_change <= params["sol_low_breadth_change"]
    ):
        return (
            params["sol_low_breadth_trend_position_size"],
            "sol_low_breadth_trend_position_size",
        )

    if (
        pure_reversion_buy
        and symbol == "ADA"
        and avg_rsi < params["ada_stalled_washout_rsi"]
        and avg_rsi_change > params["ada_stalled_washout_change"]
    ):
        return (
            params["ada_stalled_washout_reversion_position_size"],
            "ada_stalled_washout_reversion_position_size",
        )

    if (
        pure_reversion_buy
        and symbol == "DOT"
        and avg_rsi > params["dot_mild_reversion_rsi"]
    ):
        return (
            params["dot_mild_reversion_position_size"],
            "dot_mild_reversion_position_size",
        )

    if pure_reversion_buy and symbol == "ETH":
        return (
            params["eth_pure_reversion_position_size"],
            "eth_pure_reversion_position_size",
        )

    if pure_trend_buy and symbol == "AVAX":
        return params["avax_trend_position_size"], "avax_trend_position_size"

    if pure_trend_buy and symbol == "XRP":
        return params["xrp_trend_position_size"], "xrp_trend_position_size"

    return params["position_size"], None


# =============================================================================
# Buy Weight Precedence Tests
# =============================================================================

# Test cases covering all symbol-specific rules
BUY_WEIGHT_TEST_CASES = [
    # (symbol, bull_flags, market_state, description)
    (
        "BTC",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": True,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 71.0, "avg_rsi_change": 1.0},
        "BTC strong reversion (wildcard rule)",
    ),
    (
        "BTC",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 71.0, "avg_rsi_change": 1.0},
        "BTC hot stall trend",
    ),
    (
        "BTC",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 62.0, "avg_rsi_change": 16.0},
        "BTC mid hot accel trend",
    ),
    (
        "SOL",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 67.0, "avg_rsi_change": 1.0},
        "SOL hot stall trend",
    ),
    (
        "SOL",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": False,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 35.0, "avg_rsi_change": -3.0},
        "SOL mild reversion",
    ),
    (
        "SOL",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 45.0, "avg_rsi_change": -6.0},
        "SOL low breadth trend",
    ),
    (
        "LINK",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 67.0, "avg_rsi_change": 1.0},
        "LINK hot stall trend",
    ),
    (
        "XRP",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": False,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 25.0, "avg_rsi_change": -2.0},
        "XRP stalled washout reversion",
    ),
    (
        "XRP",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 50.0, "avg_rsi_change": 0.0},
        "XRP pure trend",
    ),
    (
        "ADA",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": False,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 25.0, "avg_rsi_change": -2.0},
        "ADA stalled washout reversion",
    ),
    (
        "DOT",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": False,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 34.0, "avg_rsi_change": -3.0},
        "DOT mild reversion",
    ),
    (
        "ETH",
        {
            "dual_rsi_oversold": True,
            "macd_histogram_positive": False,
            "close_below_bb_lower": True,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 30.0, "avg_rsi_change": -1.0},
        "ETH pure reversion",
    ),
    (
        "AVAX",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": True,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": True,
            "momentum_positive": True,
            "volume_above_avg": True,
        },
        {"avg_rsi": 50.0, "avg_rsi_change": 0.0},
        "AVAX pure trend",
    ),
    (
        "BTC",
        {
            "dual_rsi_oversold": False,
            "macd_histogram_positive": False,
            "close_below_bb_lower": False,
            "ema_fast_above_slow": False,
            "momentum_positive": False,
            "volume_above_avg": False,
        },
        {"avg_rsi": 50.0, "avg_rsi_change": 0.0},
        "Default position size (no match)",
    ),
]


@pytest.mark.parametrize(
    ("symbol", "bull_flags", "market_state", "description"),
    BUY_WEIGHT_TEST_CASES,
    ids=[case[3] for case in BUY_WEIGHT_TEST_CASES],
)
def test_resolve_symbol_buy_weight_matches_legacy_order(
    symbol: str,
    bull_flags: dict[str, bool],
    market_state: dict[str, float],
    description: str,
) -> None:
    """Current _resolve_symbol_buy_weight must match legacy if/elif order."""
    expected_weight, expected_key = legacy_buy_weight(symbol, bull_flags, market_state)
    actual_weight, actual_key = strategy._resolve_symbol_buy_weight(
        symbol, bull_flags, market_state, strategy.PARAMS
    )

    assert actual_weight == expected_weight, (
        f"Weight mismatch for {description}: "
        f"expected {expected_weight}, got {actual_weight}"
    )
    assert actual_key == expected_key, (
        f"Key mismatch for {description}: expected {expected_key}, got {actual_key}"
    )


# =============================================================================
# Signal Parity Tests for Helper Predicates
# =============================================================================


def test_signal_dual_rsi_oversold_matches_legacy_none_handling() -> None:
    """Test that _signal_dual_rsi_oversold handles None values correctly."""
    # Both RSIs below oversold threshold
    ctx = strategy.SignalContext(
        closes=np.array([100.0] * 30),
        volumes=np.array([1000.0] * 30),
        current_close=100.0,
        current_volume=1000.0,
        rsi_fast=29.0,
        rsi_slow=28.0,
        macd=None,
        bb=None,
        ema_fast=None,
        ema_slow=None,
        momentum=None,
        avg_volume=None,
    )
    assert strategy._signal_dual_rsi_oversold(ctx, strategy.PARAMS) is True

    # Only fast RSI below threshold
    ctx = strategy.SignalContext(
        closes=np.array([100.0] * 30),
        volumes=np.array([1000.0] * 30),
        current_close=100.0,
        current_volume=1000.0,
        rsi_fast=29.0,
        rsi_slow=35.0,
        macd=None,
        bb=None,
        ema_fast=None,
        ema_slow=None,
        momentum=None,
        avg_volume=None,
    )
    assert strategy._signal_dual_rsi_oversold(ctx, strategy.PARAMS) is False

    # None values should return False
    ctx = strategy.SignalContext(
        closes=np.array([100.0] * 30),
        volumes=np.array([1000.0] * 30),
        current_close=100.0,
        current_volume=1000.0,
        rsi_fast=None,
        rsi_slow=28.0,
        macd=None,
        bb=None,
        ema_fast=None,
        ema_slow=None,
        momentum=None,
        avg_volume=None,
    )
    assert strategy._signal_dual_rsi_oversold(ctx, strategy.PARAMS) is False


def test_setup_pure_reversion_buy_matches_legacy() -> None:
    """Test _setup_pure_reversion_buy against legacy logic."""
    # All conditions met
    bull_flags = {
        "dual_rsi_oversold": True,
        "close_below_bb_lower": True,
        "volume_above_avg": True,
        "macd_histogram_positive": False,
    }
    assert strategy._setup_pure_reversion_buy(bull_flags, strategy.PARAMS) is True

    # Missing volume
    bull_flags = {
        "dual_rsi_oversold": True,
        "close_below_bb_lower": True,
        "volume_above_avg": False,
        "macd_histogram_positive": False,
    }
    assert strategy._setup_pure_reversion_buy(bull_flags, strategy.PARAMS) is False

    # MACD positive (should be excluded)
    bull_flags = {
        "dual_rsi_oversold": True,
        "close_below_bb_lower": True,
        "volume_above_avg": True,
        "macd_histogram_positive": True,
    }
    assert strategy._setup_pure_reversion_buy(bull_flags, strategy.PARAMS) is False


def test_setup_strong_reversion_buy_matches_legacy() -> None:
    """Test _setup_strong_reversion_buy against legacy logic."""
    # All conditions met
    bull_flags = {
        "dual_rsi_oversold": True,
        "close_below_bb_lower": True,
        "macd_histogram_positive": True,
    }
    assert strategy._setup_strong_reversion_buy(bull_flags, strategy.PARAMS) is True

    # Missing MACD positive
    bull_flags = {
        "dual_rsi_oversold": True,
        "close_below_bb_lower": True,
        "macd_histogram_positive": False,
    }
    assert strategy._setup_strong_reversion_buy(bull_flags, strategy.PARAMS) is False


def test_setup_pure_trend_buy_matches_legacy() -> None:
    """Test _setup_pure_trend_buy against legacy logic."""
    # All conditions met
    bull_flags = {
        "macd_histogram_positive": True,
        "ema_fast_above_slow": True,
        "momentum_positive": True,
        "volume_above_avg": True,
        "dual_rsi_oversold": False,
    }
    assert strategy._setup_pure_trend_buy(bull_flags, strategy.PARAMS) is True

    # RSI oversold (should be excluded)
    bull_flags = {
        "macd_histogram_positive": True,
        "ema_fast_above_slow": True,
        "momentum_positive": True,
        "volume_above_avg": True,
        "dual_rsi_oversold": True,
    }
    assert strategy._setup_pure_trend_buy(bull_flags, strategy.PARAMS) is False

    # Missing momentum
    bull_flags = {
        "macd_histogram_positive": True,
        "ema_fast_above_slow": True,
        "momentum_positive": False,
        "volume_above_avg": True,
        "dual_rsi_oversold": False,
    }
    assert strategy._setup_pure_trend_buy(bull_flags, strategy.PARAMS) is False


# =============================================================================
# Import Resolution Test
# =============================================================================


def test_backtest_runner_imports_local_strategy_module() -> None:
    """Verify that backtest runner can import the local strategy module."""
    # These assertions verify the module exposes expected APIs
    assert hasattr(strategy, "Strategy")
    assert hasattr(strategy, "_resolve_symbol_buy_weight")
    assert hasattr(strategy, "PARAMS")
    assert hasattr(strategy, "SignalContext")
    assert hasattr(strategy, "MIN_HISTORY_BARS")

    # Verify key functions exist
    assert callable(strategy._resolve_symbol_buy_weight)
    assert callable(strategy._setup_pure_reversion_buy)
    assert callable(strategy._setup_strong_reversion_buy)
    assert callable(strategy._setup_pure_trend_buy)


def test_backtest_runner_uses_direct_imports() -> None:
    """Verify that backtest.py uses direct imports instead of importlib.

    This test imports backtest.py as a module to verify import mechanism.
    """
    import importlib.util
    import os

    backtest_path = (
        Path(__file__).resolve().parent.parent.parent / "backtest" / "backtest.py"
    )
    assert backtest_path.exists(), f"backtest.py not found at {backtest_path}"

    # Read the file content to verify import style
    content = backtest_path.read_text()

    # Should use direct imports, not importlib
    assert "import prepare" in content, "backtest.py should import prepare directly"
    assert "import strategy" in content, "backtest.py should import strategy directly"

    # Should NOT use importlib.import_module for prepare/strategy
    assert 'importlib.import_module("prepare")' not in content, (
        "backtest.py should not use importlib for prepare"
    )
    assert 'importlib.import_module("strategy")' not in content, (
        "backtest.py should not use importlib for strategy"
    )


# =============================================================================
# Parameter Access Test
# =============================================================================


def test_params_contains_all_required_keys() -> None:
    """Verify PARAMS dict contains all keys required by the strategy."""
    required_keys = [
        "rsi_period_fast",
        "rsi_period_slow",
        "rsi_oversold",
        "rsi_exit",
        "max_positions",
        "position_size",
        "strong_reversion_position_size",
        "btc_hot_stall_trend_position_size",
        "btc_mid_hot_accel_trend_position_size",
        "holding_days",
        "stop_loss_pct",
        "cooldown_days",
        "min_votes",
        "min_weighted_buy_votes",
        "min_sell_votes",
    ]

    for key in required_keys:
        assert key in strategy.PARAMS, f"Missing required param: {key}"


# =============================================================================
# Symbol Buy Rules Order Test
# =============================================================================


def test_symbol_buy_rules_strong_reversion_is_first() -> None:
    """Verify that strong_reversion rule is first in SYMBOL_BUY_RULES.

    This is critical for precedence - strong_reversion is a wildcard (*) rule
    that must be checked before symbol-specific rules.
    """
    assert len(strategy.SYMBOL_BUY_RULES) > 0
    first_rule = strategy.SYMBOL_BUY_RULES[0]
    assert first_rule[0] == "*", (
        "First rule should be wildcard (*) for strong_reversion"
    )
    assert first_rule[3] == "strong_reversion_position_size", (
        "First rule should be strong_reversion_position_size"
    )


def test_symbol_buy_rules_btc_rules_precedence() -> None:
    """Verify BTC rules are early in the list (after wildcard)."""
    symbols = [rule[0] for rule in strategy.SYMBOL_BUY_RULES]

    # Find first BTC rule position
    btc_positions = [i for i, s in enumerate(symbols) if s == "BTC"]
    assert len(btc_positions) >= 2, "Should have at least 2 BTC rules"

    # BTC rules should come after wildcard but before most other symbols
    assert btc_positions[0] > 0, "BTC rules should be after wildcard"
