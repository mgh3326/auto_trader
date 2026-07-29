"""ROB-1145 — strategy plugin registry / ``--strategy`` selector."""

from __future__ import annotations

import pytest

from app.services.brokers.binance.demo_strategy_loop.registry import (
    DEFAULT_STRATEGY_KEY,
    UnknownStrategyKey,
    available_strategy_keys,
    build_strategy,
)
from app.services.brokers.binance.demo_strategy_loop.strategy import (
    LastBarDirectionStrategy,
    NullStrategy,
)


def test_default_is_null_strategy() -> None:
    assert DEFAULT_STRATEGY_KEY == "null"
    assert isinstance(build_strategy(), NullStrategy)
    assert isinstance(build_strategy(None), NullStrategy)
    assert isinstance(build_strategy("null"), NullStrategy)


def test_null_strategy_is_still_registered_and_listed_first() -> None:
    """ROB-1145 must not remove the safe default."""
    keys = available_strategy_keys()
    assert keys[0] == "null"
    assert "last-bar-direction" in keys


def test_last_bar_direction_is_selectable() -> None:
    strategy = build_strategy("last-bar-direction")
    assert isinstance(strategy, LastBarDirectionStrategy)
    assert strategy.strategy_id == "last-bar-direction"


def test_key_resolution_is_case_and_whitespace_insensitive() -> None:
    assert isinstance(build_strategy("  Last-Bar-Direction "), LastBarDirectionStrategy)


def test_unknown_key_fails_closed() -> None:
    with pytest.raises(UnknownStrategyKey) as excinfo:
        build_strategy("s3-signal-engine")
    assert "s3-signal-engine" in str(excinfo.value)
    # The error must enumerate the real options rather than silently
    # falling back to a plugin the operator did not ask for.
    assert "null" in str(excinfo.value)


def test_every_registered_key_builds_a_plugin_with_matching_contract() -> None:
    for key in available_strategy_keys():
        plugin = build_strategy(key)
        assert isinstance(plugin.strategy_id, str) and plugin.strategy_id
        assert callable(plugin.evaluate)
