"""ROB-307 PR1 — tests for the Demo scalping risk/order-intent contract.

The risk envelope is locked by ROB-307 §5: spot long-only, per-symbol +
global + daily-count + daily-loss caps, 10 USDT notional cap, allowlist
only, plus spread/freshness/cooldown gates. ``evaluate_risk`` is a pure
function over value objects so it is testable without a broker or DB.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import (
    DEFAULT_ALLOWLIST,
    EXCLUDED_SYMBOLS,
    MAX_NOTIONAL_USDT,
    LedgerSnapshot,
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
    evaluate_risk,
)


def _healthy_ledger() -> LedgerSnapshot:
    return LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=0,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )


def _healthy_market(*, spot_free_base_qty: Decimal = Decimal("0")) -> MarketConditions:
    return MarketConditions(
        spread_bps=Decimal("2"),
        data_age_seconds=1.0,
        spot_free_base_qty=spot_free_base_qty,
    )


def _evaluate(
    *,
    product: str = "spot",
    symbol: str = "XRPUSDT",
    side: str = "BUY",
    target_notional_usdt: Decimal = Decimal("10"),
    limits: ScalpingRiskLimits | None = None,
    ledger: LedgerSnapshot | None = None,
    market: MarketConditions | None = None,
):
    return evaluate_risk(
        product=product,
        symbol=symbol,
        side=side,
        target_notional_usdt=target_notional_usdt,
        limits=limits or ScalpingRiskLimits(),
        ledger=ledger or _healthy_ledger(),
        market=market or _healthy_market(),
    )


def test_default_limits_match_locked_envelope() -> None:
    limits = ScalpingRiskLimits()
    assert limits.allowlist == DEFAULT_ALLOWLIST
    assert DEFAULT_ALLOWLIST == frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})
    assert EXCLUDED_SYMBOLS == frozenset({"BTCUSDT"})
    assert limits.max_notional_usdt == MAX_NOTIONAL_USDT == Decimal("10")
    assert limits.global_open_lifecycle_cap >= 1


def test_healthy_buy_is_allowed_with_no_reasons() -> None:
    decision = _evaluate()
    assert decision.allowed is True
    assert decision.reason_codes == ()


def test_excluded_symbol_is_blocked() -> None:
    decision = _evaluate(symbol="BTCUSDT")
    assert decision.allowed is False
    assert ReasonCode.SYMBOL_EXCLUDED in decision.reason_codes


def test_non_allowlisted_symbol_is_blocked() -> None:
    decision = _evaluate(symbol="ETHUSDT")
    assert decision.allowed is False
    assert ReasonCode.SYMBOL_NOT_ALLOWLISTED in decision.reason_codes


def test_wide_spread_is_blocked() -> None:
    market = MarketConditions(
        spread_bps=Decimal("75"),
        data_age_seconds=1.0,
        spot_free_base_qty=Decimal("0"),
    )
    decision = _evaluate(market=market)
    assert decision.allowed is False
    assert ReasonCode.SPREAD_TOO_WIDE in decision.reason_codes


def test_stale_data_is_blocked() -> None:
    market = MarketConditions(
        spread_bps=Decimal("2"),
        data_age_seconds=600.0,
        spot_free_base_qty=Decimal("0"),
    )
    decision = _evaluate(market=market)
    assert decision.allowed is False
    assert ReasonCode.STALE_DATA in decision.reason_codes


def test_notional_above_cap_is_blocked() -> None:
    decision = _evaluate(target_notional_usdt=Decimal("25"))
    assert decision.allowed is False
    assert ReasonCode.NOTIONAL_ABOVE_CAP in decision.reason_codes


def test_open_lifecycle_for_symbol_blocks_reentry() -> None:
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=True,
        global_open_lifecycle_count=1,
        orders_today=1,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )
    decision = _evaluate(ledger=ledger)
    assert decision.allowed is False
    assert ReasonCode.OPEN_LIFECYCLE_EXISTS in decision.reason_codes


def test_global_open_lifecycle_cap_blocks() -> None:
    limits = ScalpingRiskLimits(global_open_lifecycle_cap=2)
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=2,
        orders_today=2,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )
    decision = _evaluate(limits=limits, ledger=ledger)
    assert decision.allowed is False
    assert ReasonCode.GLOBAL_LIFECYCLE_CAP_REACHED in decision.reason_codes


def test_daily_order_count_cap_blocks() -> None:
    limits = ScalpingRiskLimits(daily_order_count_cap=5)
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=5,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=None,
    )
    decision = _evaluate(limits=limits, ledger=ledger)
    assert decision.allowed is False
    assert ReasonCode.DAILY_ORDER_CAP_REACHED in decision.reason_codes


def test_daily_loss_budget_exhausted_blocks() -> None:
    limits = ScalpingRiskLimits(daily_loss_budget_usdt=Decimal("5"))
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=1,
        realized_loss_today_usdt=Decimal("5"),
        seconds_since_last_close_for_symbol=None,
    )
    decision = _evaluate(limits=limits, ledger=ledger)
    assert decision.allowed is False
    assert ReasonCode.DAILY_LOSS_BUDGET_EXHAUSTED in decision.reason_codes


def test_cooldown_active_blocks_reentry() -> None:
    limits = ScalpingRiskLimits(cooldown_seconds=300)
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=1,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=120.0,
    )
    decision = _evaluate(limits=limits, ledger=ledger)
    assert decision.allowed is False
    assert ReasonCode.COOLDOWN_ACTIVE in decision.reason_codes


def test_cooldown_elapsed_does_not_block() -> None:
    limits = ScalpingRiskLimits(cooldown_seconds=300)
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=False,
        global_open_lifecycle_count=0,
        orders_today=1,
        realized_loss_today_usdt=Decimal("0"),
        seconds_since_last_close_for_symbol=600.0,
    )
    decision = _evaluate(limits=limits, ledger=ledger)
    assert ReasonCode.COOLDOWN_ACTIVE not in decision.reason_codes


def test_spot_sell_without_holding_is_blocked() -> None:
    # Spot is long-only: a SELL may only close/reduce an existing holding.
    decision = _evaluate(
        product="spot",
        side="SELL",
        market=_healthy_market(spot_free_base_qty=Decimal("0")),
    )
    assert decision.allowed is False
    assert ReasonCode.SPOT_SELL_WITHOUT_HOLDING in decision.reason_codes


def test_spot_sell_with_holding_is_allowed() -> None:
    decision = _evaluate(
        product="spot",
        side="SELL",
        market=_healthy_market(spot_free_base_qty=Decimal("100")),
    )
    assert decision.allowed is True
    assert ReasonCode.SPOT_SELL_WITHOUT_HOLDING not in decision.reason_codes


def test_futures_sell_to_open_short_is_not_spot_blocked() -> None:
    # Futures may open a short via SELL; the spot long-only rule must not fire.
    decision = _evaluate(
        product="usdm_futures",
        side="SELL",
        market=_healthy_market(spot_free_base_qty=Decimal("0")),
    )
    assert ReasonCode.SPOT_SELL_WITHOUT_HOLDING not in decision.reason_codes


def test_multiple_violations_are_all_reported() -> None:
    # The observe-only record needs every blocking reason, not just the first.
    limits = ScalpingRiskLimits()
    ledger = LedgerSnapshot(
        has_open_lifecycle_for_symbol=True,
        global_open_lifecycle_count=9,
        orders_today=99,
        realized_loss_today_usdt=Decimal("100"),
        seconds_since_last_close_for_symbol=1.0,
    )
    market = MarketConditions(
        spread_bps=Decimal("999"),
        data_age_seconds=9999.0,
        spot_free_base_qty=Decimal("0"),
    )
    decision = _evaluate(
        symbol="BTCUSDT",
        target_notional_usdt=Decimal("50"),
        limits=limits,
        ledger=ledger,
        market=market,
    )
    assert decision.allowed is False
    assert len(decision.reason_codes) >= 5


@pytest.mark.parametrize("symbol", sorted(DEFAULT_ALLOWLIST))
def test_each_allowlisted_symbol_passes_allowlist_gate(symbol: str) -> None:
    decision = _evaluate(symbol=symbol)
    assert ReasonCode.SYMBOL_NOT_ALLOWLISTED not in decision.reason_codes
    assert ReasonCode.SYMBOL_EXCLUDED not in decision.reason_codes
