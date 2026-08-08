"""KR §4 envelope (NAV-ratio kill) — the two X-C verification lessons.

orch-mock relayed two MEDIUM findings from the X-C (crypto) verification pass
that this module exists to prove KR does not repeat:

1. ``MAX_TABLE_AGE`` must not silently apply to KR. It is crypto-only
   (``table_source.MAX_TABLE_AGE == {"crypto": ...}``) — this file asserts
   that "kr" is still absent, so an unrelated future edit cannot reintroduce a
   contract-unwritten age gate for KR without a assertion failing here.
2. The shadow lane's kill switch compared a USDT constant directly against a
   KRW-denominated ``realized_pnl_today`` — an absolute threshold in the
   wrong currency. KR's contract §4 value is not even an absolute amount (it
   is "일 손실 −2.5% NAV", a ratio), so this file proves the ratio is turned
   into a same-currency absolute threshold via a same-cycle NAV snapshot
   before ever being compared, and that a missing NAV snapshot fails closed
   instead of silently comparing a ratio against a currency amount.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.b0x import kill_switch
from scripts.b0x.envelope import (
    KR_MOCK_ENVELOPE,
    EnvelopeNotLocked,
    assert_envelope_locked,
)
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import MAX_TABLE_AGE

pytestmark = pytest.mark.unit


def test_kr_envelope_matches_contract_section_4_kr_column() -> None:
    """종목당 신규 30만 KRW · 총투입 신규x5 · 동시 10 · 일 신규 3 · 일손실 -2.5% NAV."""

    envelope = KR_MOCK_ENVELOPE
    assert envelope.market == "kr"
    assert envelope.quote_currency == "KRW"
    assert envelope.per_order_notional == Decimal("300000")
    assert envelope.per_symbol_total_notional == Decimal("1500000")
    assert envelope.max_concurrent_positions == 10
    assert envelope.max_new_entries_per_utc_day == 3
    assert envelope.daily_loss_kill == Decimal("0.025")
    assert envelope.daily_loss_kill_basis == "pct_of_nav"


def test_kr_envelope_is_locked() -> None:
    assert_envelope_locked(KR_MOCK_ENVELOPE)
    widened = replace(KR_MOCK_ENVELOPE, per_order_notional=Decimal("999999"))
    with pytest.raises(EnvelopeNotLocked):
        assert_envelope_locked(widened)


def test_max_table_age_has_no_kr_entry() -> None:
    """Lesson 1 — do not let a future edit quietly add an age gate for KR.

    Contract §2-2 defines exactly two zero-order triggers: table absent, or
    ``STALE`` marker present. Contract §5 gives crypto (only) a 4h rebuild
    cadence, which is why ``stale_by_age`` exists for crypto. KR's cadence
    (일 1회, 장 전) has no such age gate in the contract, and orch's explicit
    instruction was: if one seems needed, report
    ``NEEDS_UPSTREAM(table_age_gate_not_in_contract)`` — do not add it.
    """

    assert "kr" not in MAX_TABLE_AGE
    assert set(MAX_TABLE_AGE) == {"crypto"}, (
        "a new market entry appeared in MAX_TABLE_AGE without a contract "
        "citation — table_source.py docstring only documents crypto's 4h cadence"
    )


def _kr_state(*, realized_pnl_today: Decimal, nav: Decimal | None) -> LaneAccountState:
    return LaneAccountState(
        lane="kis_mock",
        quote_currency="KRW",
        cash=Decimal("5000000"),
        realized_pnl_today=realized_pnl_today,
        nav=nav,
    )


def test_kr_kill_switch_requires_nav_snapshot() -> None:
    """Lesson 2, half A — a pct_of_nav envelope with no NAV fails closed.

    This is the shape of bug X-C shipped: nothing stopped a ratio-basis
    envelope from being evaluated without ever supplying the value the ratio
    is relative to. Here it cannot compile silently into a wrong comparison —
    it raises.
    """

    state = _kr_state(realized_pnl_today=Decimal("-1000"), nav=None)
    with pytest.raises(kill_switch.MissingNavForRatioKill):
        kill_switch.evaluate(state=state, envelope=KR_MOCK_ENVELOPE)


def test_kr_kill_switch_compares_same_currency_absolute_amounts() -> None:
    """Lesson 2, half B — the ratio is multiplied into KRW before comparing.

    NAV = 10,000,000 KRW; -2.5% of that is -250,000 KRW. A 249,000 KRW loss
    must not trip the kill (it is short of the budget); a 250,000 KRW loss
    must trip it exactly at the boundary (<=, not <).
    """

    nav = Decimal("10000000")
    envelope = KR_MOCK_ENVELOPE

    just_under = kill_switch.evaluate(
        state=_kr_state(realized_pnl_today=Decimal("-249000"), nav=nav),
        envelope=envelope,
    )
    assert just_under.allow_new_orders is True
    assert not just_under.tripped
    assert just_under.daily_loss_kill == Decimal("250000")
    assert just_under.daily_loss_kill_basis == "pct_of_nav"
    assert just_under.daily_loss_kill_config == Decimal("0.025")
    assert just_under.nav_snapshot == nav

    at_budget = kill_switch.evaluate(
        state=_kr_state(realized_pnl_today=Decimal("-250000"), nav=nav),
        envelope=envelope,
    )
    assert at_budget.tripped
    assert at_budget.allow_new_orders is False
    assert kill_switch.KillReason.DAILY_LOSS_BUDGET_REACHED in at_budget.kill_reasons
    notice = at_budget.operator_notice(lane="kis_mock")
    assert notice is not None
    assert "250000" in notice
    # The absolute threshold AND the NAV it was derived from are both in the
    # notice text — an operator reading it can verify the multiplication
    # themselves rather than trusting a single opaque number.
    assert "10000000" in notice
    assert "0.025" in notice


def test_kr_kill_switch_recomputes_threshold_every_cycle_from_current_nav() -> None:
    """A NAV-relative budget must track NAV, not freeze at some earlier value.

    Same realized_pnl_today, two different NAV snapshots -> two different
    effective thresholds and potentially different kill outcomes. If this
    were memoized or defaulted from the locked envelope alone, both calls
    would return the same threshold; they must not.
    """

    loss = Decimal("-150000")
    small_nav = kill_switch.evaluate(
        state=_kr_state(realized_pnl_today=loss, nav=Decimal("5000000")),
        envelope=KR_MOCK_ENVELOPE,
    )
    large_nav = kill_switch.evaluate(
        state=_kr_state(realized_pnl_today=loss, nav=Decimal("50000000")),
        envelope=KR_MOCK_ENVELOPE,
    )
    assert small_nav.tripped  # -150,000 <= -125,000 (2.5% of 5,000,000)
    assert not large_nav.tripped  # -150,000 > -1,250,000 (2.5% of 50,000,000)


def test_crypto_kill_switch_unaffected_by_basis_field() -> None:
    """Additive-change regression guard — crypto's absolute path is untouched."""

    from scripts.b0x.envelope import CRYPTO_SIDECAR_ENVELOPE

    assert CRYPTO_SIDECAR_ENVELOPE.daily_loss_kill_basis == "absolute"
    state = LaneAccountState(
        lane="binance_spot_demo_sidecar",
        quote_currency="USDT",
        cash=Decimal("100"),
        realized_pnl_today=Decimal("-5"),
        nav=None,
    )
    decision = kill_switch.evaluate(state=state, envelope=CRYPTO_SIDECAR_ENVELOPE)
    assert decision.tripped
    assert decision.daily_loss_kill == Decimal("5")
    assert decision.nav_snapshot is None
