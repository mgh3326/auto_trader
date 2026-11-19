"""Pure invariant functions (I2, I3, I4, I5, I6, I15, I17) with the design's own
fixtures (§3.2 "회차 기준값", §3.8). No database."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.mandate import vocabulary as v
from app.services.mandate.invariants import (
    InvariantViolation,
    PlanAmounts,
    account_binding_ok,
    check_one_share,
    is_parking_symbol,
    min_gap_met,
    sector_cap_check,
    sleeve_budget_check,
    symbol_allowed,
    tranche_amounts,
    tranche_order_ok,
    tranche_trigger_met,
)
from tests.services.mandate.support import Stream

pytestmark = pytest.mark.unit
D = Decimal
MAN = D(10_000)  # 1 man = 10,000 won; the design table is in man


def man(n: str) -> Decimal:
    return D(n) * MAN


# ------------------------------------------------------------------ I5 triggers
@pytest.mark.parametrize(
    ("price", "pct", "met"),
    [
        # tranche 2 (-15%), first fill 888,600 / 9 shares
        ("83923", "-15", True),
        ("83924", "-15", False),
        # tranche 3 (-30%)
        ("69113", "-30", True),
        ("69114", "-30", False),
    ],
)
def test_i5_trigger_boundary_is_division_free_and_inclusive(price, pct, met) -> None:
    assert tranche_trigger_met(D(price), D("888600"), D("9"), D(pct)) is met


def test_i5_no_trigger_without_a_first_fill_or_a_price() -> None:
    assert not tranche_trigger_met(D("1"), D("0"), D("0"), D("-15"))
    assert not tranche_trigger_met(D("0"), D("888600"), D("9"), D("-15"))
    assert not tranche_trigger_met(D("NaN"), D("888600"), D("9"), D("-15"))


# ---------------------------------------------------------------------- I6 gap
def _sessions(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def test_i6_twenty_sessions_after_the_settle_day() -> None:
    settled = date(2026, 11, 24)
    sessions = _sessions(settled, 25)
    assert not min_gap_met(settled, sessions[18], sessions, 20)  # 19 sessions
    assert min_gap_met(settled, sessions[19], sessions, 20)  # the 20th
    assert not min_gap_met(None, sessions[24], sessions, 20)  # no fill, no gap


# ---------------------------------------------------------------------- I7 order
def test_i7_order_reads_the_fold() -> None:
    s = Stream()
    s.approve()
    assert tranche_order_ok(s.state, 1)
    assert not tranche_order_ok(s.state, 2)
    s.fill_first()
    assert tranche_order_ok(s.state, 2)


# ---------------------------------------------------------------- tranche amounts
def test_tranche_amounts_split_only_the_new_money() -> None:
    amounts = tranche_amounts(man("235"), man("100"), ["0.4", "0.3", "0.3"])
    assert amounts == (man("54"), man("40.5"), man("40.5"))
    assert sum(amounts) == man("135")


def test_tranche_amounts_refuse_a_split_that_does_not_add_up() -> None:
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100.0001"), D("0"), ["0.4", "0.3", "0.3"])  # 5 decimals
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100"), D("0"), ["0.5", "0.3", "0.3"])  # weights != 1
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100"), D("200"), ["0.4", "0.3", "0.3"])  # adopted > planned


def test_every_tranche_must_afford_one_share() -> None:
    check_one_share((D("940000"),), D("100000"))
    with pytest.raises(InvariantViolation) as caught:
        check_one_share((D("940000"), D("99999")), D("100000"))
    assert caught.value.code is v.MandateRefusal.TRANCHE_BELOW_ONE_SHARE
    with pytest.raises(InvariantViolation):
        check_one_share((D("940000"),), D("0"))  # no price: no registration


# ---------------------------------------------------------------------- I3 budget
def test_i3_sleeve_budget_counts_new_money_only() -> None:
    cap = man("1000")
    x = PlanAmounts(man("235"), man("100"), "auto")  # N = 135
    z = PlanAmounts(man("235"), man("0"), "auto")  # N = 235
    sleeve_budget_check(z, [x], cap)  # 370 <= 1000
    with pytest.raises(InvariantViolation) as caught:
        sleeve_budget_check(PlanAmounts(man("900"), D("0"), "x"), [x, z], cap)
    assert caught.value.code is v.MandateRefusal.BUDGET_EXCEEDED


# ---------------------------------------------------------------------- I4 sector
def _basket() -> tuple[list[PlanAmounts], tuple[Decimal, Decimal]]:
    plans = [
        PlanAmounts(man("235"), man("100"), "auto"),  # X (Toss)
        PlanAmounts(man("235"), man("100"), "auto"),  # Y (KIS)
        PlanAmounts(man("235"), man("0"), "auto"),  # Z
        PlanAmounts(man("235"), man("60"), "fin"),  # W
    ]
    return plans, (man("1000"), man("950"))


def test_i4_design_example_three_pass_and_a_fourth_auto_plan_is_refused() -> None:
    plans, caps = _basket()
    # the first three automotive plans: numerator 705 <= 35% x 2,210 (773.5)
    sector_cap_check(plans[2], plans[:2] + plans[3:], caps, D("35"))
    # a fourth automotive plan: numerator 940 > 773.5
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    with pytest.raises(InvariantViolation) as caught:
        sector_cap_check(fourth, plans, caps, D("35"))
    assert caught.value.code is v.MandateRefusal.SECTOR_CAP


def test_i4_adopted_amounts_enter_the_numerator_through_planned_once() -> None:
    """Dropping A from the numerator would let the fourth plan pass (505 < 773.5)."""
    plans, caps = _basket()
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    wrong_numerator = sum(p.new_amount for p in [*plans[:3], fourth])
    assert wrong_numerator * 100 <= man("2210") * 35  # the mistake would pass
    with pytest.raises(InvariantViolation):
        sector_cap_check(fourth, plans, caps, D("35"))


def test_i4_a_released_plan_no_longer_counts_but_a_retired_one_still_does() -> None:
    plans, caps = _basket()
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    # the caller passes only unreleased plans: a retired (unreleased) Y still counts
    with pytest.raises(InvariantViolation):
        sector_cap_check(fourth, plans, caps, D("35"))
    # once Y is released (left out), the denominator loses its A (100) as well
    without_y = [plans[0], plans[2], plans[3]]
    sector_cap_check(fourth, without_y, caps, D("35"))


# ------------------------------------------------------------------ I17 / I2 / I15
@pytest.mark.parametrize("symbol", ["459580", "357870", "SGOV", "BIL", " sgov "])
def test_i17_parking_symbols_are_not_plan_targets(symbol) -> None:
    assert is_parking_symbol(symbol)


@pytest.mark.parametrize("symbol", ["005930", "BRK.B", "AAPL"])
def test_i17_ordinary_symbols_pass(symbol) -> None:
    assert not is_parking_symbol(symbol)


def test_i17_an_unreadable_symbol_fails_closed() -> None:
    assert is_parking_symbol("한글")


def test_i2_symbol_must_be_listed() -> None:
    assert symbol_allowed("005930", ["005930", "000270"])
    assert not symbol_allowed("005380", ["005930", "000270"])


def test_i15_plan_account_is_the_clause_account_and_a_scope_account() -> None:
    ok = {
        "terms_account_ref": "t",
        "plan_account_ref": "t",
        "scope_account_refs": ["t", "k"],
        "realm": "live",
    }
    assert account_binding_ok(**ok)
    assert not account_binding_ok(**{**ok, "plan_account_ref": "k"})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["k"]})
    assert not account_binding_ok(**{**ok, "scope_account_refs": []})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["t", "t"]})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["t", "k", "x"]})


def test_i15_live_scope_is_both_real_accounts_and_mock_scope_is_one() -> None:
    live = {
        "terms_account_ref": "t",
        "plan_account_ref": "t",
        "scope_account_refs": ["t"],
        "realm": "live",
    }
    assert not account_binding_ok(**live)  # one account leaves the other out
    assert account_binding_ok(**{**live, "scope_account_refs": ["t", "k"]})
    mock = {**live, "realm": "mock"}
    assert account_binding_ok(**mock)
    assert not account_binding_ok(**{**mock, "scope_account_refs": ["t", "k"]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("I7_unarmed", "mandate_invalid_transition"),
        ("I8_tranche", "mandate_tranche_amount_exceeded"),
        ("I8_plan", "mandate_plan_amount_exceeded"),
        ("I9_open", "mandate_tranche_duplicate"),
        ("I9_uncertain", "mandate_tranche_uncertain"),
        ("I11_missing_freeze_reader", "mandate_drawdown_frozen"),
        ("I16_not_effective", "mandate_plan_not_active"),
    ],
)
async def test_t4a_creation_refuses_before_write(case, reason):
    from dataclasses import replace
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from app.services.mandate.fold import PlanState
    from app.services.mandate.proposal_gate import (
        PlanProposalContext,
        check_proposal_creation,
    )

    today = date(2026, 11, 20)
    state = PlanState(authority="approved", effective_session=today)
    index = 2 if case == "I7_unarmed" else 1
    first = state.tranche(1)
    if case == "I9_open":
        first = replace(first, open_proposals=(uuid4(),))
    if case == "I9_uncertain":
        first = replace(first, unclosed_fences=(10,))
    state = replace(state, tranches=(first, *state.tranches[1:]))
    if case == "I16_not_effective":
        state = replace(state, effective_session=date(2026, 11, 23))
    context = PlanProposalContext(
        plan=SimpleNamespace(
            planned_amount=Decimal(50 if case == "I8_plan" else 200),
            adopted_amount=Decimal(0),
        ),
        sleeve=SimpleNamespace(id=1, account_mode="kiwoom_mock", market="equity_kr"),
        state=state,
        terms=SimpleNamespace(),
        tranche=SimpleNamespace(tranche_index=index, amount=Decimal(100)),
    )
    session = AsyncMock()

    with pytest.raises(ValueError, match=reason):
        await check_proposal_creation(
            session,
            context,
            notional=Decimal(101 if case == "I8_tranche" else 80),
            now=datetime(2026, 11, 20, 1, tzinfo=UTC),
        )
    session.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("I4_sector", "mandate_sector_cap"),
        ("I11_stale_freeze", "mandate_drawdown_frozen"),
        ("I12_expired", "mandate_expired"),
        ("I15_account", "mandate_account_mismatch"),
        ("I18_ceiling", "mandate_tranche_not_activated"),
        ("I21_hash", "mandate_binding_broken"),
    ],
)
async def test_t4a_dispatch_checks_durable_evidence(monkeypatch, case, reason):
    from dataclasses import replace
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from app.services.mandate import proposal_gate as gate
    from app.services.mandate.fold import PlanState

    now = datetime(2026, 11, 20, 1, tzinfo=UTC)
    proposal_id = uuid4()
    index = 2 if case == "I18_ceiling" else 1
    state = PlanState(
        authority="active", effective_session=now.date(), current_terms_sha256="terms"
    )
    state = replace(
        state,
        tranches=tuple(
            replace(tranche, open_proposals=(proposal_id,))
            if tranche.index == index
            else replace(tranche, completed=True)
            if tranche.index < index
            else tranche
            for tranche in state.tranches
        ),
    )
    terms = SimpleNamespace(
        id=9,
        approval_id=10,
        content_sha256="terms",
        expires_at=now if case == "I12_expired" else now + timedelta(days=1),
        basket_key="basket",
    )
    profile = SimpleNamespace(
        terms_id=9,
        approval_id=11,
        content_sha256="profile",
        expires_at=now + timedelta(days=1),
        effective_session=now.date(),
        max_tranche_index=1,
    )
    context = gate.PlanProposalContext(
        plan=SimpleNamespace(sleeve_id=1, account_ref="reference", sector_key="sector"),
        sleeve=SimpleNamespace(id=1, account_mode="kiwoom_mock", market="equity_kr"),
        state=state,
        terms=terms,
        tranche=SimpleNamespace(tranche_index=index),
    )
    monkeypatch.setattr(
        gate.store,
        "current_terms",
        AsyncMock(return_value=(SimpleNamespace(state="live"), terms)),
    )
    monkeypatch.setattr(gate.store, "latest_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr(
        gate,
        "buy_freeze_state",
        AsyncMock(
            return_value=gate.FreezeVerdict(
                frozen=False,
                reason="fixture",
                evidence={},
                valid_until=now
                if case == "I11_stale_freeze"
                else now + timedelta(seconds=1),
            )
        ),
    )
    monkeypatch.setattr(gate.store, "terms_content_from_rows", lambda *_: {})
    monkeypatch.setattr(gate.store, "profile_content_from_row", lambda *_: {})
    monkeypatch.setattr(
        gate, "_approval_matches", AsyncMock(return_value=case != "I21_hash")
    )
    monkeypatch.setattr(
        gate.store,
        "basket_view",
        AsyncMock(return_value=gate.store.BasketView((Decimal(100),), Decimal(35))),
    )
    monkeypatch.setattr(
        gate.store,
        "unreleased_plan_amounts",
        AsyncMock(return_value=[PlanAmounts(Decimal(40), Decimal(0), "sector")]),
    )

    verdict, detail = await gate.check_dispatch(
        AsyncMock(),
        context,
        proposal_id=proposal_id,
        broker_account_id="other" if case == "I15_account" else "reference",
        now=now,
    )
    assert verdict == reason
    if case == "I4_sector":
        assert detail == {
            "mandate_sector_amount": "40",
            "mandate_sector_denominator": "100",
            "mandate_sector_cap_pct": "35",
        }
    if case == "I18_ceiling":
        assert detail == {"tranche_index": "2", "max_tranche_index": "1"}


@pytest.mark.parametrize(
    ("case", "price", "sessions", "failure_field"),
    [
        ("I5_trigger", "96", (date(2026, 11, 20),), "trigger_pct_from_first"),
        ("I6_gap", "95", (), "min_gap_sessions"),
        ("I8_notional", "95", (date(2026, 11, 20),), "mandate_remaining"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_t4a_dispatch_market_recheck(case, price, sessions, failure_field):
    from dataclasses import replace
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.services.mandate.fold import PlanState
    from app.services.mandate.proposal_gate import (
        PlanProposalContext,
        TrancheMarketEvidence,
        check_tranche_market_evidence,
    )

    state = PlanState(first_fill_amount=Decimal(100), first_fill_quantity=Decimal(1))
    first = replace(state.tranche(1), completed=True, settled_on=date(2026, 11, 19))
    state = replace(state, tranches=(first, *state.tranches[1:]))
    context = PlanProposalContext(
        plan=SimpleNamespace(planned_amount=Decimal(1000), adopted_amount=Decimal(0)),
        sleeve=SimpleNamespace(),
        state=state,
        terms=SimpleNamespace(),
        tranche=SimpleNamespace(
            tranche_index=2,
            amount=Decimal(94 if case == "I8_notional" else 100),
            trigger_pct_from_first=Decimal(-5),
            min_gap_sessions=1,
        ),
    )
    refusal = check_tranche_market_evidence(
        context,
        TrancheMarketEvidence(Decimal(price), Decimal(94), Decimal(1), sessions),
        now=datetime(2026, 11, 20, 1, tzinfo=UTC),
    )

    assert refusal is not None
    reason, detail = refusal
    assert reason == (
        "mandate_tranche_amount" if case == "I8_notional" else "mandate_tranche_not_due"
    )
    assert failure_field in detail


@pytest.mark.asyncio
async def test_i11_freeze_stub_fails_closed_for_live_and_mock():
    from datetime import UTC, datetime

    from app.services.mandate.buy_freeze import buy_freeze_state

    for account_mode in ("kis_live", "toss_live", "kiwoom_mock"):
        verdict = await buy_freeze_state(
            None,  # type: ignore[arg-type]  # The stub performs no DB read.
            account_mode=account_mode,
            market="kr",
            sleeve_id=1,
            now=datetime.now(UTC),
        )
        assert verdict.frozen is True
        assert verdict.valid_until is None
