"""Adversarial contract tests for the reserve-net candidate-only consumer."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.support_reserve_net_consumer import (
    ATOMICITY_BLOCK_CODE,
    ATOMICITY_STANCE,
    CashSnapshot,
    ReserveNetAttribution,
    ReserveNetCandidate,
    ReserveNetRequest,
    SelfUnfilledOrder,
    SupportReserveNetConsumer,
)


def _consumer() -> SupportReserveNetConsumer:
    return SupportReserveNetConsumer.from_current_policy()


def _cash(
    *,
    market: str = "equity_kr",
    account_mode: str = "kis_live",
    fresh: str = "500000",
    net: str | None = None,
    all_pending: str = "0",
    reserve_armed: str = "0",
) -> CashSnapshot:
    return CashSnapshot(
        account_mode=account_mode,
        broker_account_id="acct-1",
        currency="USD" if market == "equity_us" else "KRW",
        fresh_broker_orderable_cash=Decimal(fresh),
        net_orderable_cash=Decimal(net if net is not None else fresh),
        all_pending_buy_required_cash=Decimal(all_pending),
        reserve_net_armed_required_cash=Decimal(reserve_armed),
        is_fresh=True,
        same_account_currency_pending_accounted=True,
    )


def _new(
    symbol: str = "NEW",
    *,
    market: str = "equity_kr",
    account_mode: str = "kis_live",
    required_cash: str = "200000",
    quantity: str = "1000",
    support_strength: str = "moderate",
    source_families: tuple[str, ...] = ("fib", "bb_lower"),
    honest_upside: str = "45",
    proposed_limit: str = "91.2",
    sector: str = "software",
) -> ReserveNetCandidate:
    return ReserveNetCandidate(
        normalized_symbol=symbol,
        market=market,  # type: ignore[arg-type]
        account_mode=account_mode,
        broker_account_id="acct-1",
        beneficial_owner_id="owner-1",
        intent="new",
        current_price=Decimal("100"),
        support_price=Decimal("96"),
        support_strength=support_strength,
        independent_support_families=source_families,
        honest_upside_pct=Decimal(honest_upside),
        regular_gate_failure="RSI_ONLY",
        discount_below_support_pct=Decimal("5"),
        proposed_limit_price=Decimal(proposed_limit),
        price_tick=Decimal("0.1"),
        quantity=Decimal(quantity),
        required_cash=Decimal(required_cash),
        sector_cluster=sector,
        post_fill_sector_concentration_pct=Decimal("5"),
        post_fill_sector_increase=Decimal("0.01"),
        thesis="fresh support and independent-source evidence",
    )


def _add(
    symbol: str = "ADD",
    *,
    market: str = "equity_kr",
    account_mode: str = "kis_live",
    quantity: str = "5",
    required_cash: str = "500",
    support_strength: str = "strong",
    deep_loss: str = "1",
    sector: str = "energy",
) -> ReserveNetCandidate:
    return ReserveNetCandidate(
        normalized_symbol=symbol,
        market=market,  # type: ignore[arg-type]
        account_mode=account_mode,
        broker_account_id="acct-1",
        beneficial_owner_id="owner-1",
        intent="add",
        current_price=Decimal("100"),
        support_price=Decimal("92"),
        support_strength=support_strength,
        independent_support_families=("fib", "bb_lower"),
        honest_upside_pct=Decimal("45"),
        regular_gate_failure="RSI_ONLY",
        discount_below_support_pct=Decimal("5"),
        proposed_limit_price=Decimal("87.4"),
        price_tick=Decimal("0.1"),
        quantity=Decimal(quantity),
        required_cash=Decimal(required_cash),
        sector_cluster=sector,
        post_fill_sector_concentration_pct=Decimal("5"),
        post_fill_sector_increase=Decimal("0.01"),
        thesis="R-931 review and fresh thesis support the add",
        r931_review_status="PASS",
        r931_review_age_days=Decimal("1"),
        policy_table_age_hours=Decimal("1"),
        held_cost_basis=Decimal("1000"),
        held_average_price=Decimal("100"),
        held_quantity=Decimal("10"),
        lot_size=Decimal("1"),
        deep_loss_pct=Decimal(deep_loss),
    )


def _request(
    *candidates: ReserveNetCandidate,
    cash: tuple[CashSnapshot, ...] | None = None,
    attributions: tuple[ReserveNetAttribution, ...] = (),
    self_unfilled: tuple[SelfUnfilledOrder, ...] = (),
    self_unfilled_complete: bool = True,
    sectors_complete: bool = True,
) -> ReserveNetRequest:
    return ReserveNetRequest(
        candidates=tuple(candidates),
        cash_snapshots=cash or (_cash(),),
        reserve_net_attributions=attributions,
        self_unfilled_orders=self_unfilled,
        sector_exposures=(),
        self_unfilled_order_read_complete=self_unfilled_complete,
        sector_exposure_complete=sectors_complete,
    )


def _rejection_codes(plan) -> dict[str, str]:
    return {item.normalized_symbol: item.code for item in plan.rejected}


def test_acceptance_vector_new_one_add_one_remaining_slot_selects_new() -> None:
    """Required §56 vector: [new, add] with one slot chooses new first."""

    existing = ReserveNetAttribution(
        normalized_symbol="FILLED",
        market="equity_kr",
        beneficial_owner_id="owner-1",
        account_mode="kis_live",
        broker_account_id="acct-1",
        state="filled",
        sector_cluster="hardware",
    )
    plan = _consumer().plan(_request(_new(), _add(), attributions=(existing,)))

    assert [item.normalized_symbol for item in plan.selected] == ["NEW"]
    assert _rejection_codes(plan)["ADD"] == "max_reserve_net_symbols_per_market"


def test_pool_order_allows_one_add_fallback_and_add_tie_breaks_by_symbol() -> None:
    """No new candidates: only one add may claim the fallback slot, ASC ties."""

    plan = _consumer().plan(_request(_add("ZZZ"), _add("AAA")))

    assert [item.normalized_symbol for item in plan.selected] == ["AAA"]
    assert _rejection_codes(plan)["ZZZ"] == "max_add_symbols_per_market"


def test_max_symbol_count_ignores_other_strategy_holdings() -> None:
    """Only reserve-net armed/open/filled attribution consumes this cap."""

    ordinary = (
        ReserveNetAttribution(
            normalized_symbol="OTHER1",
            market="equity_kr",
            beneficial_owner_id="owner-1",
            account_mode="kis_live",
            broker_account_id="acct-1",
            state="filled",
            strategy="ordinary_buy_strategy",
        ),
        ReserveNetAttribution(
            normalized_symbol="OTHER2",
            market="equity_kr",
            beneficial_owner_id="owner-1",
            account_mode="kis_live",
            broker_account_id="acct-1",
            state="filled",
            strategy="ordinary_buy_strategy",
        ),
    )

    plan = _consumer().plan(_request(_new(), attributions=ordinary))

    assert [item.normalized_symbol for item in plan.selected] == ["NEW"]


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            replace(_new(), honest_upside_pct=Decimal("39.99")),
            "honest_upside_below_minimum",
        ),
        (
            replace(_new(), independent_support_families=("fib",)),
            "independent_support_families_insufficient",
        ),
        (
            replace(_new(), proposed_limit_price=Decimal("85")),
            "anchor_not_tick_floor_or_outside_band",
        ),
    ],
)
def test_candidate_zero_never_relaxes_signed_runtime_gates(
    candidate: ReserveNetCandidate, reason: str
) -> None:
    plan = _consumer().plan(_request(candidate))

    assert plan.selected == ()
    assert _rejection_codes(plan)[candidate.normalized_symbol] == reason


def test_armed_cap_cannot_exceed_fifty_percent() -> None:
    candidate = _new(required_cash="100000")
    plan = _consumer().plan(
        _request(candidate, cash=(_cash(fresh="400000", reserve_armed="200000"),))
    )

    assert plan.selected == ()
    assert _rejection_codes(plan)["NEW"] == "tier_armed_cash_cap_exceeded"


def test_all_pending_buy_hard_cap_remains_ninety_percent() -> None:
    candidate = _new(required_cash="100000")
    plan = _consumer().plan(_request(candidate, cash=(_cash(all_pending="450000"),)))

    assert plan.selected == ()
    assert _rejection_codes(plan)["NEW"] == "all_pending_buy_hard_cap_exceeded"


def test_sector_cap_is_fail_closed_when_post_fill_concentration_exceeds_ten_percent() -> (
    None
):
    candidate = replace(_new(), post_fill_sector_concentration_pct=Decimal("10.01"))
    plan = _consumer().plan(_request(candidate))

    assert plan.selected == ()
    assert (
        _rejection_codes(plan)["NEW"]
        == "sector_concentration_unavailable_or_cap_exceeded"
    )


def test_kr_new_requires_available_cash_at_or_above_four_hundred_thousand() -> None:
    candidate = _new(required_cash="100000")
    below = _consumer().plan(
        _request(candidate, cash=(_cash(fresh="399999", net="399999"),))
    )
    exact = _consumer().plan(
        _request(candidate, cash=(_cash(fresh="400000", net="400000"),))
    )

    assert below.selected == ()
    assert _rejection_codes(below)["NEW"] == "kr_new_min_available_cash_not_met"
    assert [item.normalized_symbol for item in exact.selected] == ["NEW"]


def test_us_new_candidates_can_use_two_slots_without_the_kr_threshold() -> None:
    standard_us_limit = {
        "current_price": Decimal("164.50"),
        "support_price": Decimal("157.90"),
        "proposed_limit_price": Decimal("150.00"),
        "price_tick": Decimal("0.01"),
        "quantity": Decimal("1"),
        "required_cash": Decimal("150"),
    }
    first = replace(
        _new("US1", market="equity_us", sector="us_software"),
        **standard_us_limit,
    )
    second = replace(
        _new("US2", market="equity_us", sector="us_healthcare"),
        **standard_us_limit,
    )
    plan = _consumer().plan(
        _request(
            first,
            second,
            cash=(_cash(market="equity_us", fresh="1000", net="1000"),),
        )
    )

    assert [item.normalized_symbol for item in plan.selected] == ["US1", "US2"]
    assert [item.required_cash for item in plan.selected] == [
        Decimal("150"),
        Decimal("150"),
    ]


def test_us_add_is_disabled_even_with_otherwise_complete_evidence() -> None:
    candidate = _add(
        market="equity_us",
        account_mode="kis_live",
        required_cash="500",
    )
    plan = _consumer().plan(
        _request(
            candidate,
            cash=(_cash(market="equity_us", fresh="10000", net="10000"),),
        )
    )

    assert plan.selected == ()
    assert (
        _rejection_codes(plan)["ADD"]
        == "us_add_disabled_thesis_review_contract_missing"
    )


def test_add_recalculates_a_limit_and_forbids_partial_fill() -> None:
    plan = _consumer().plan(_request(_add(quantity="4", required_cash="500")))

    assert plan.selected == ()
    assert _rejection_codes(plan)["ADD"] == "a_limit_not_fully_satisfied"


def test_add_ranking_does_not_reward_deeper_loss() -> None:
    deep_but_weaker = _add("DEEP", support_strength="moderate", deep_loss="99")
    shallow_but_stronger = _add("SHALLOW", support_strength="strong", deep_loss="1")
    plan = _consumer().plan(_request(deep_but_weaker, shallow_but_stronger))

    assert [item.normalized_symbol for item in plan.selected] == ["SHALLOW"]


def test_known_self_unfilled_buy_blocks_same_symbol_before_candidate_selection() -> (
    None
):
    open_buy = SelfUnfilledOrder(
        normalized_symbol="NEW",
        market="equity_kr",
        beneficial_owner_id="owner-1",
        account_mode="kis_live",
        broker_account_id="acct-1",
    )
    plan = _consumer().plan(_request(_new(), self_unfilled=(open_buy,)))

    assert plan.selected == ()
    assert _rejection_codes(plan)["NEW"] == "self_unfilled_buy_exists"


def test_duplicate_candidate_symbol_is_fail_closed() -> None:
    plan = _consumer().plan(_request(_new("DUP"), _new("DUP")))

    assert plan.selected == ()
    assert {item.code for item in plan.rejected} == {"duplicate_candidate_symbol"}


@pytest.mark.asyncio
async def test_atomicity_stance_blocks_proposal_creator_even_for_selected_candidate() -> (
    None
):
    class SpyProposalCreator:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create_proposal(self, **kwargs):
            self.calls.append(kwargs)
            return {"unexpected": True}

    spy = SpyProposalCreator()
    result = await _consumer().consume(_request(_new()), proposal_creator=spy)

    assert ATOMICITY_STANCE == "a_candidate_only_before_proposal_creation"
    assert result.proposal_creation_status == ATOMICITY_BLOCK_CODE
    assert result.plan.proposal_creation_permitted is False
    assert "지금은 원자적이지 않다" in result.plan.unatomicity_notice
    assert spy.calls == []


def test_missing_precheck_evidence_fails_closed_before_selection() -> None:
    plan = _consumer().plan(_request(_new(), self_unfilled_complete=False))

    assert plan.selected == ()
    assert _rejection_codes(plan)["NEW"] == "self_unfilled_order_read_unavailable"
