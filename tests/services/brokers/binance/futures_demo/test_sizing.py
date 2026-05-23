"""ROB-298 PR 2 — Futures Demo sizing helper: floor + symbol allowlist + BTCUSDT excluded."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoUnsupportedSymbol,
)
from app.services.brokers.binance.futures_demo.sizing import (
    FUTURES_DEMO_DEFAULT_SYMBOL,
    FUTURES_DEMO_EXCLUDED_SYMBOLS,
    FUTURES_DEMO_FALLBACK_SYMBOLS,
    FuturesSizingBlocked,
    FuturesSizingResult,
    assert_symbol_allowed,
    compute_futures_demo_order_qty,
)


def test_default_symbol_is_xrp() -> None:
    assert FUTURES_DEMO_DEFAULT_SYMBOL == "XRPUSDT"


def test_btcusdt_explicitly_excluded() -> None:
    assert "BTCUSDT" in FUTURES_DEMO_EXCLUDED_SYMBOLS


def test_fallback_allowlist_contents() -> None:
    assert "XRPUSDT" in FUTURES_DEMO_FALLBACK_SYMBOLS
    assert "DOGEUSDT" in FUTURES_DEMO_FALLBACK_SYMBOLS
    assert "SOLUSDT" in FUTURES_DEMO_FALLBACK_SYMBOLS


def test_assert_symbol_allowed_passes_xrp() -> None:
    assert_symbol_allowed("XRPUSDT")  # no raise


def test_assert_symbol_allowed_passes_doge() -> None:
    assert_symbol_allowed("DOGEUSDT")  # no raise


def test_assert_symbol_allowed_rejects_btc() -> None:
    with pytest.raises(BinanceFuturesDemoUnsupportedSymbol, match="excluded"):
        assert_symbol_allowed("BTCUSDT")


def test_assert_symbol_allowed_rejects_unknown_symbol() -> None:
    with pytest.raises(BinanceFuturesDemoUnsupportedSymbol, match="allowlist"):
        assert_symbol_allowed("UNKNOWNUSDT")


def test_override_cannot_unexclude_btc() -> None:
    """Operator override can extend allowlist but cannot un-exclude BTCUSDT."""
    with pytest.raises(BinanceFuturesDemoUnsupportedSymbol, match="excluded"):
        assert_symbol_allowed("BTCUSDT", allowlist_override=frozenset({"BTCUSDT"}))


def test_override_can_add_new_symbol() -> None:
    """Operator override can add a non-excluded symbol like ETHUSDT."""
    assert_symbol_allowed(
        "ETHUSDT",
        allowlist_override=frozenset({"XRPUSDT", "ETHUSDT"}),
    )  # no raise


def test_xrp_floor_to_step_size() -> None:
    # XRPUSDT: target $10, price=$0.6, step=0.1, min_notional=$5, cap=$10
    # raw qty = 10/0.6 = 16.67; floor to 0.1 → 16.6; notional = 16.6 * 0.6 = 9.96
    result = compute_futures_demo_order_qty(
        symbol="XRPUSDT",
        target_notional_usdt=Decimal("10"),
        price=Decimal("0.6"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.1"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, FuturesSizingResult)
    assert result.qty == Decimal("16.6")
    assert result.notional_usdt <= Decimal("10")
    assert result.notional_usdt >= Decimal("5")


def test_btcusdt_raises_before_math() -> None:
    """BTCUSDT raises immediately, before any computation."""
    with pytest.raises(BinanceFuturesDemoUnsupportedSymbol):
        compute_futures_demo_order_qty(
            symbol="BTCUSDT",
            target_notional_usdt=Decimal("10"),
            price=Decimal("60000"),
            min_notional=Decimal("50"),
            step_size=Decimal("0.001"),
            cap_usdt=Decimal("10"),
        )


def test_blocked_when_floor_below_min_notional() -> None:
    # SOLUSDT-like: step too large to fit cap with min_notional
    result = compute_futures_demo_order_qty(
        symbol="SOLUSDT",
        target_notional_usdt=Decimal("10"),
        price=Decimal("100"),
        min_notional=Decimal("8"),
        step_size=Decimal("1.0"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, FuturesSizingBlocked)
    assert "MIN_NOTIONAL" in result.reason


def test_target_above_cap_clipped() -> None:
    # XRPUSDT: target $20 (above cap), cap=$10, price=$0.6 → notional ≤ 10
    result = compute_futures_demo_order_qty(
        symbol="XRPUSDT",
        target_notional_usdt=Decimal("20"),
        price=Decimal("0.6"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.1"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, FuturesSizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_never_rounds_up_past_cap() -> None:
    """Floor-only — never round up to clear MIN_NOTIONAL or hit cap."""
    result = compute_futures_demo_order_qty(
        symbol="XRPUSDT",
        target_notional_usdt=Decimal("10"),
        price=Decimal("0.7"),
        min_notional=Decimal("5"),
        step_size=Decimal("0.1"),
        cap_usdt=Decimal("10"),
    )
    assert isinstance(result, FuturesSizingResult)
    assert result.notional_usdt <= Decimal("10")


def test_cap_must_be_positive() -> None:
    with pytest.raises(ValueError):
        compute_futures_demo_order_qty(
            symbol="XRPUSDT",
            target_notional_usdt=Decimal("10"),
            price=Decimal("0.6"),
            min_notional=Decimal("5"),
            step_size=Decimal("0.1"),
            cap_usdt=Decimal("0"),
        )
