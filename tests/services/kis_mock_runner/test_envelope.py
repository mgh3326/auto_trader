from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.kis_mock_runner.envelope import (
    CONFIGURABLE_OFF_SWITCH,
    LOCKED_ENVELOPE,
    AccountEnvelopeSnapshot,
    EnvelopeOverrideAttempt,
    EnvelopeReason,
    HardEnvelope,
    OrderIntent,
    assert_envelope_locked,
    assert_no_envelope_overrides,
    evaluate_envelope,
)


def _snapshot(**overrides: object) -> AccountEnvelopeSnapshot:
    values: dict[str, object] = {
        "session_start_nlv_krw": Decimal("100000000"),
        "current_nlv_krw": Decimal("100000000"),
        "available_cash_krw": Decimal("10000000"),
        "projected_gross_exposure_krw": Decimal("40000000"),
        "positions_including_pending_reserved": 4,
        "new_entries_this_xkrx_session": 2,
        "planned_exits_this_xkrx_session": 2,
        "cash_is_fresh": True,
        "is_cash_only": True,
        "margin_enabled": False,
        "short_enabled": False,
    }
    values.update(overrides)
    return AccountEnvelopeSnapshot(**values)  # type: ignore[arg-type]


def _entry(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "side": "buy",
        "role": "entry",
        "order_type": "limit",
        "quantity": Decimal("10"),
        "limit_price_krw": Decimal("100000"),
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def test_locked_envelope_has_operator_approved_literals() -> None:
    assert CONFIGURABLE_OFF_SWITCH == "NO"
    assert LOCKED_ENVELOPE.max_single_notional_krw == Decimal("5000000")
    assert LOCKED_ENVELOPE.session_start_nlv_fraction == Decimal("0.05")
    assert LOCKED_ENVELOPE.max_gross_exposure_fraction == Decimal("0.50")
    assert LOCKED_ENVELOPE.max_positions_including_pending_reserved == 10
    assert LOCKED_ENVELOPE.max_new_entries_per_xkrx_session == 10
    assert LOCKED_ENVELOPE.max_planned_exits_per_xkrx_session == 10
    assert LOCKED_ENVELOPE.daily_loss_halt_fraction == Decimal("0.025")
    assert LOCKED_ENVELOPE.per_order_notional_cap(Decimal("200000000")) == Decimal(
        "5000000"
    )
    assert LOCKED_ENVELOPE.per_order_notional_cap(Decimal("40000000")) == Decimal(
        "2000000"
    )


def test_environment_override_attempt_fails_closed() -> None:
    with pytest.raises(EnvelopeOverrideAttempt, match="MAX_SINGLE_NOTIONAL"):
        assert_no_envelope_overrides(
            {"KIS_MOCK_RUNNER_MAX_SINGLE_NOTIONAL_KRW": "999999999"}
        )


def test_non_cli_caller_cannot_supply_a_wider_envelope() -> None:
    with pytest.raises(ValueError, match="differs"):
        assert_envelope_locked(
            replace(LOCKED_ENVELOPE, max_positions_including_pending_reserved=11)
        )
    with pytest.raises(ValueError, match="differs"):
        evaluate_envelope(
            intent=_entry(),
            snapshot=_snapshot(),
            envelope=HardEnvelope(max_single_notional_krw=Decimal("9000000")),
        )


def test_envelope_rejects_market_price_missing_and_unfresh_cash() -> None:
    decision = evaluate_envelope(
        intent=_entry(order_type="market", limit_price_krw=None),
        snapshot=_snapshot(cash_is_fresh=False),
    )
    assert decision.allowed is False
    assert set(decision.reason_codes) >= {
        EnvelopeReason.LIMIT_ONLY,
        EnvelopeReason.LIMIT_PRICE_REQUIRED,
        EnvelopeReason.CASH_NOT_FRESH,
    }


def test_envelope_rejects_hard_caps_and_daily_loss_halt() -> None:
    decision = evaluate_envelope(
        intent=_entry(quantity=Decimal("100"), limit_price_krw=Decimal("100000")),
        snapshot=_snapshot(
            current_nlv_krw=Decimal("97500000"),
            projected_gross_exposure_krw=Decimal("50000001"),
            positions_including_pending_reserved=11,
            new_entries_this_xkrx_session=10,
        ),
    )
    assert decision.allowed is False
    assert set(decision.reason_codes) >= {
        EnvelopeReason.PER_ORDER_NOTIONAL_CAP,
        EnvelopeReason.GROSS_EXPOSURE_CAP,
        EnvelopeReason.POSITION_CAP,
        EnvelopeReason.SESSION_NEW_ENTRY_CAP,
        EnvelopeReason.DAILY_LOSS_ENTRY_HALT,
    }
    assert decision.requires_entry_halt is True


def test_daily_loss_halt_blocks_entries_but_not_exits() -> None:
    drawdown_snapshot = _snapshot(current_nlv_krw=Decimal("97500000"))
    entry = evaluate_envelope(intent=_entry(), snapshot=drawdown_snapshot)
    exit = evaluate_envelope(
        intent=OrderIntent(
            side="sell",
            role="exit",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price_krw=Decimal("100000"),
        ),
        snapshot=drawdown_snapshot,
    )

    assert entry.allowed is False
    assert entry.requires_entry_halt is True
    assert EnvelopeReason.DAILY_LOSS_ENTRY_HALT in entry.reason_codes
    assert exit.allowed is True
    assert exit.reason_codes == ()


def test_exit_cap_is_distinct_from_new_entry_cap() -> None:
    decision = evaluate_envelope(
        intent=OrderIntent(
            side="sell",
            role="exit",
            order_type="limit",
            quantity=Decimal("1"),
            limit_price_krw=Decimal("100000"),
        ),
        snapshot=_snapshot(planned_exits_this_xkrx_session=10),
    )
    assert decision.allowed is False
    assert decision.reason_codes == (EnvelopeReason.SESSION_PLANNED_EXIT_CAP,)
