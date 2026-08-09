"""KR §4 envelope (NAV-ratio kill) + §2-2 v1.1 MAX_TABLE_AGE.

orch-mock relayed two MEDIUM findings from the X-C (crypto) verification pass
that this module exists to prove KR does not repeat, plus one amendment:

1. ``MAX_TABLE_AGE`` initially had no contract basis for KR — the first
   instruction was not to add one and to report
   ``NEEDS_UPSTREAM(table_age_gate_not_in_contract)`` instead. That was then
   **reversed**: the operator promoted the X-C-verification-found safety net
   into contract v1.1 §2-2 (sha256
   ``97278b0e8b8000e2e663c936328686001af5850087897270bc80a95ebf8f6b2e``,
   confirmed 2026-08-08), which gives KR a literal value: 36h. This file now
   pins *that* value, cited, rather than pinning its absence.
2. The shadow lane's kill switch compared a USDT constant directly against a
   KRW-denominated ``realized_pnl_today`` — an absolute threshold in the
   wrong currency. KR's contract §4 value is not even an absolute amount (it
   is "일 손실 −2.5% NAV", a ratio), so this file proves the ratio is turned
   into a same-currency absolute threshold via a same-cycle NAV snapshot
   before ever being compared, and that a missing NAV snapshot fails closed
   instead of silently comparing a ratio against a currency amount. This
   guidance did **not** change.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from scripts.b0x import kill_switch
from scripts.b0x.broker_truth import BrokerTruth
from scripts.b0x.envelope import (
    KR_MOCK_ENVELOPE,
    EnvelopeNotLocked,
    assert_envelope_locked,
)
from scripts.b0x.state import LaneAccountState
from scripts.b0x.table_source import MAX_TABLE_AGE

#: The literal this whole file's age-gate assertions trace back to —
#: contract v1.1 §2-2, operator-confirmed 2026-08-08. Not a magic string:
#: independently re-verifiable against
#: ``~/work/herdr-inbox/b0x-experiment-contract-v1-20260808.md``.
CONTRACT_V1_1_SHA256 = (
    "97278b0e8b8000e2e663c936328686001af5850087897270bc80a95ebf8f6b2e"
)

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


def test_max_table_age_kr_is_36h_per_contract_v1_1() -> None:
    """Lesson 1 (amended) — KR's age gate is a contract literal, not a guess.

    Contract §2-2 v1.1: *"MAX_TABLE_AGE (v1.1, 운영자 확정 2026-08-08): crypto
    8h · KR 36h · US 36h"* — promoted from an X-C-verification-found safety
    net into a 3-market contract rule. This pins the exact value so a future
    edit that silently drifts it (in either direction) fails here first.
    """

    assert MAX_TABLE_AGE["kr"] == dt.timedelta(hours=36)
    # crypto's pre-existing value is unchanged by the v1.1 promotion.
    assert MAX_TABLE_AGE["crypto"] == dt.timedelta(hours=8)
    # "us" belongs to a separate job (X-U) — not this file's concern to add.
    assert set(MAX_TABLE_AGE) <= {"crypto", "kr", "us"}, (
        f"unexpected market entries in MAX_TABLE_AGE: {set(MAX_TABLE_AGE)} — "
        "every key must trace to contract v1.1 §2-2"
    )


def _kr_state(*, realized_pnl_today: Decimal, nav: Decimal | None) -> LaneAccountState:
    return LaneAccountState(
        lane="kis_mock",
        quote_currency="KRW",
        cash=Decimal("5000000"),
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
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
        broker_truth=BrokerTruth(position_symbols=(), own_pending=()),
        realized_pnl_today=Decimal("-5"),
        nav=None,
    )
    decision = kill_switch.evaluate(state=state, envelope=CRYPTO_SIDECAR_ENVELOPE)
    assert decision.tripped
    assert decision.daily_loss_kill == Decimal("5")
    assert decision.nav_snapshot is None
