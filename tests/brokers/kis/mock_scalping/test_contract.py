"""KIS mock scalping risk contract tests (ROB-321 PR3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
    evaluate_risk,
)


def _clean_ledger(**kw) -> LedgerSnapshot:
    base: dict = {
        "has_open_position_for_symbol": False,
        "open_position_count": 0,
        "orders_today": 0,
        "realized_loss_today_krw": Decimal("0"),
        "seconds_since_last_close_for_symbol": None,
    }
    base.update(kw)
    return LedgerSnapshot(**base)


def _clean_market(**kw) -> MarketConditions:
    base: dict = {"spread_bps": Decimal("5"), "data_age_seconds": 1.0}
    base.update(kw)
    return MarketConditions(**base)


def _eval(**kw):
    params: dict = {
        "symbol": "005930",
        "side": "BUY",
        "target_notional_krw": Decimal("50000"),
        "limits": ScalpingRiskLimits(),
        "ledger": _clean_ledger(),
        "market": _clean_market(),
    }
    params.update(kw)
    return evaluate_risk(**params)


@pytest.mark.unit
def test_clean_buy_is_allowed() -> None:
    decision = _eval()
    assert decision.allowed is True
    assert decision.reason_codes == ()


@pytest.mark.unit
def test_sell_entry_blocked_long_only() -> None:
    decision = _eval(side="SELL")
    assert decision.allowed is False
    assert ReasonCode.SHORT_ENTRY_NOT_ALLOWED in decision.reason_codes


@pytest.mark.unit
def test_symbol_not_allowlisted_blocked() -> None:
    decision = _eval(symbol="999999")
    assert ReasonCode.SYMBOL_NOT_ALLOWLISTED in decision.reason_codes


@pytest.mark.unit
def test_spread_and_stale_gates() -> None:
    decision = _eval(
        market=_clean_market(spread_bps=Decimal("99"), data_age_seconds=999.0)
    )
    assert ReasonCode.SPREAD_TOO_WIDE in decision.reason_codes
    assert ReasonCode.STALE_DATA in decision.reason_codes


@pytest.mark.unit
def test_notional_above_cap_blocked() -> None:
    decision = _eval(target_notional_krw=Decimal("200000"))
    assert ReasonCode.NOTIONAL_ABOVE_CAP in decision.reason_codes


@pytest.mark.unit
def test_lifecycle_and_daily_caps() -> None:
    ledger = _clean_ledger(
        has_open_position_for_symbol=True,
        open_position_count=1,
        orders_today=10,
        realized_loss_today_krw=Decimal("50000"),
        seconds_since_last_close_for_symbol=10.0,
    )
    decision = _eval(ledger=ledger)
    assert ReasonCode.OPEN_POSITION_EXISTS in decision.reason_codes
    assert ReasonCode.MAX_OPEN_POSITIONS_REACHED in decision.reason_codes
    assert ReasonCode.DAILY_ORDER_CAP_REACHED in decision.reason_codes
    assert ReasonCode.DAILY_LOSS_BUDGET_EXHAUSTED in decision.reason_codes
    assert ReasonCode.COOLDOWN_ACTIVE in decision.reason_codes


@pytest.mark.unit
def test_cooldown_none_is_not_blocking() -> None:
    decision = _eval(ledger=_clean_ledger(seconds_since_last_close_for_symbol=None))
    assert ReasonCode.COOLDOWN_ACTIVE not in decision.reason_codes


@pytest.mark.unit
def test_reasons_accumulate_not_short_circuit() -> None:
    decision = _eval(
        symbol="999999",
        target_notional_krw=Decimal("200000"),
        market=_clean_market(spread_bps=Decimal("99")),
    )
    assert {
        ReasonCode.SYMBOL_NOT_ALLOWLISTED,
        ReasonCode.NOTIONAL_ABOVE_CAP,
        ReasonCode.SPREAD_TOO_WIDE,
    } <= set(decision.reason_codes)
