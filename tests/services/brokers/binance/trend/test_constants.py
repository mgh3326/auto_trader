"""LOCKED constants: operator values, no override, validated before any IO."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from app.services.brokers.binance.trend import IDENTITY
from app.services.brokers.binance.trend.constants import (
    LOCKED_LIMITS,
    TrendLimits,
    TrendLimitsNotLocked,
    assert_limits_locked,
)
from app.services.brokers.binance.trend.keys import (
    make_signal_key,
    trend_client_order_id,
    trend_correlation_id,
)
from app.services.brokers.binance.trend.state import TrendStateService

pytestmark = pytest.mark.unit


def test_identity_and_operator_values() -> None:
    assert IDENTITY == "TREND-TSMOM-v1"
    assert LOCKED_LIMITS.universe == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
    assert LOCKED_LIMITS.leg_notional_cap_usdt == Decimal("60")
    assert LOCKED_LIMITS.max_concurrent_positions == 4
    assert LOCKED_LIMITS.max_gross_notional_usdt == Decimal("240")
    assert LOCKED_LIMITS.max_consecutive_hard_stops_per_utc_day == 2
    assert LOCKED_LIMITS.max_conflicts_per_utc_week == 3
    assert LOCKED_LIMITS.required_leverage == 1


def test_locked_limits_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        LOCKED_LIMITS.max_concurrent_positions = 5  # type: ignore[misc]
    assert_limits_locked(LOCKED_LIMITS)
    assert_limits_locked(TrendLimits())


@pytest.mark.parametrize(
    "change",
    [
        {"universe": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")},
        {"universe": ("BTCUSDT",)},
        {"leg_notional_cap_usdt": Decimal("61")},
        {"leg_notional_cap_usdt": Decimal("59")},
        {"max_concurrent_positions": 5},
        {"max_concurrent_positions": 3},
        {"max_gross_notional_usdt": Decimal("300")},
        {"max_consecutive_hard_stops_per_utc_day": 3},
        {"max_conflicts_per_utc_week": 4},
        {"required_leverage": 2},
    ],
)
def test_any_deviation_is_refused(change: dict) -> None:
    with pytest.raises(TrendLimitsNotLocked):
        assert_limits_locked(dataclasses.replace(LOCKED_LIMITS, **change))


def test_a_non_limits_object_is_refused() -> None:
    with pytest.raises(TrendLimitsNotLocked):
        assert_limits_locked({"max_concurrent_positions": 4})  # type: ignore[arg-type]


def test_service_refuses_widened_limits_before_any_db_use() -> None:
    def factory():
        raise AssertionError("session factory must not be touched")

    with pytest.raises(TrendLimitsNotLocked):
        TrendStateService(
            factory,
            limits=dataclasses.replace(LOCKED_LIMITS, max_concurrent_positions=9),
        )
    # The locked envelope builds without touching the factory either.
    TrendStateService(factory)


def test_there_is_no_env_or_cli_path_to_the_limits() -> None:
    # The service takes no setting, env var or flag: only the locked value.
    import inspect

    from app.services.brokers.binance.trend import constants

    source = inspect.getsource(constants)
    assert "environ" not in source and "getenv" not in source
    assert "argparse" not in source and "settings" not in source


def test_signal_key_and_ids_are_deterministic_and_bounded() -> None:
    ts = 1_700_000_000_000
    key = make_signal_key("BTCUSDT", ts, "BUY", "37123.40")
    assert key == "BTCUSDT|2023-11-15 07:13|BUY|37123.40"
    cid = trend_correlation_id(key)
    assert cid.startswith("binance-trend:") and len(cid) == len("binance-trend:") + 24
    assert cid == trend_correlation_id(key)
    order_id = trend_client_order_id(key, "entry")
    assert order_id.startswith("trend-") and len(order_id) <= 36
    assert order_id != trend_client_order_id(key, "exit")


@pytest.mark.parametrize(
    ("symbol", "side", "price"),
    [
        ("DOGEUSDT", "BUY", "1"),
        ("BTCUSDT", "HOLD", "1"),
        ("BTCUSDT", "BUY", "0"),
        ("BTCUSDT", "BUY", "-1"),
        ("BTCUSDT", "BUY", "NaN"),
        ("BTCUSDT", "BUY", "abc"),
        ("BTCUSDT", "BUY", ""),
    ],
)
def test_signal_key_refuses_bad_identity(symbol: str, side: str, price: str) -> None:
    with pytest.raises(ValueError):
        make_signal_key(symbol, 1_700_000_000_000, side, price)
