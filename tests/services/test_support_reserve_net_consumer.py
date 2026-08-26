"""Adversarial contracts for the seam-gated reserve-net consumer."""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.service import RungInput
from app.services.support_reserve_net_consumer import (
    ATOMICITY_BLOCK_CODE,
    ATOMICITY_STANCE,
    CashSnapshot,
    ReserveNetAttribution,
    ReserveNetCandidate,
    ReserveNetRequest,
    SectorExposure,
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
    broker_account_id: str = "acct-1",
) -> CashSnapshot:
    return CashSnapshot(
        account_mode=account_mode,
        broker_account_id=broker_account_id,
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
    broker_account_id: str = "acct-1",
) -> ReserveNetCandidate:
    return ReserveNetCandidate(
        normalized_symbol=symbol,
        market=market,  # type: ignore[arg-type]
        account_mode=account_mode,
        broker_account_id=broker_account_id,
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


@pytest.mark.parametrize(
    ("case_id", "candidate", "expected"),
    [
        # 1. The clarified effect: RSI_ONLY + moderate + two allowed families
        # is eligible for this tier.
        ("1_moderate_two_families", _new(), "selected"),
        # 2. The independent-family count remains a hard lower bound.
        (
            "2_one_family",
            replace(_new(), independent_support_families=("fib",)),
            "independent_support_families_insufficient",
        ),
        # 3. Moderate is the tier floor; weak support is not eligible.
        (
            "3_weak_support",
            replace(_new(), support_strength="weak"),
            "support_strength_below_moderate",
        ),
        # 4. RSI is the only omitted regular gate; upside failure remains a
        # rejection even when the support axis is otherwise complete.
        (
            "4_upside_failure",
            replace(_new(), honest_upside_pct=Decimal("39.99")),
            "honest_upside_below_minimum",
        ),
        # 5. The tier's own support-distance limit remains active.
        (
            "5_support_distance_over_8pct",
            replace(
                _new(),
                support_price=Decimal("91"),
                proposed_limit_price=Decimal("86.4"),
            ),
            "anchor_not_tick_floor_or_outside_band",
        ),
        # 6. RSI_ONLY is a prerequisite, not a synonym for all candidates.
        (
            "6_rsi_passes",
            replace(_new(), regular_gate_failure="REGULAR_PASS"),
            "regular_gate_failure_not_rsi_only",
        ),
        # 8. Two sources outside the closed family enumeration do not count.
        (
            "8_unlisted_families",
            replace(_new(), independent_support_families=("fib", "rsi")),
            "independent_support_families_insufficient",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_k_support_reserve_net_eligibility_matrix(
    case_id: str,
    candidate: ReserveNetCandidate,
    expected: str,
) -> None:
    """Pin the eight-case boundary; only case 1 gains eligibility."""

    plan = _consumer().plan(_request(candidate))
    if expected == "selected":
        assert case_id == "1_moderate_two_families"
        assert [item.normalized_symbol for item in plan.selected] == [
            candidate.normalized_symbol
        ]
        assert plan.rejected == ()
    else:
        assert plan.selected == ()
        assert _rejection_codes(plan)[candidate.normalized_symbol] == expected


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


def test_sector_cap_excess_is_advisory_and_persisted_with_selected_candidate() -> None:
    """MUTATION-ANCHOR: s156-sector-cap-advisory-surface."""
    candidate = replace(_new(), post_fill_sector_concentration_pct=Decimal("10.01"))
    consumer = _consumer()
    plan = consumer.plan(_request(candidate))

    assert [proposal.normalized_symbol for proposal in plan.selected] == ["NEW"]
    assert plan.rejected == ()
    assert len(plan.sector_cluster_cap_advisories) == 1
    advisory = plan.sector_cluster_cap_advisories[0]
    assert advisory.normalized_symbol == "NEW"
    assert advisory.code == "sector_cluster_cap_exceeded"
    assert advisory.sector_cluster == "software"
    assert advisory.post_fill_sector_concentration_pct == Decimal("10.01")
    assert advisory.sector_cluster_cap_pct == Decimal("10")
    assert advisory.post_fill_sector_increase == Decimal("0.01")

    create_kwargs = consumer._proposal_create_kwargs(plan.selected[0])
    assert create_kwargs["source_asof"]["sector_cluster_cap_advisories"] == [
        {
            "code": "sector_cluster_cap_exceeded",
            "sector_cluster": "software",
            "post_fill_sector_concentration_pct": "10.01",
            "sector_cluster_cap_pct": "10",
            "post_fill_sector_increase": "0.01",
        }
    ]
    normal_plan = consumer.plan(_request(_new("NORMAL")))
    normal_create_kwargs = consumer._proposal_create_kwargs(normal_plan.selected[0])
    assert set(normal_create_kwargs["source_asof"]) == {
        "policy_version",
        "policy_content_hash",
    }


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (replace(_new(), sector_cluster=None), "unknown_sector_ineligible"),
        (
            replace(_new(), post_fill_sector_increase=Decimal("-0.01")),
            "sector_concentration_negative_data",
        ),
        (
            replace(_new(), post_fill_sector_concentration_pct=Decimal("-0.01")),
            "sector_concentration_negative_data",
        ),
    ],
)
def test_unknown_sector_and_negative_sector_data_remain_hard_gates(
    candidate: ReserveNetCandidate,
    expected: str,
) -> None:
    plan = _consumer().plan(_request(candidate))

    assert plan.selected == ()
    assert _rejection_codes(plan)[candidate.normalized_symbol] == expected


def test_max_symbols_per_sector_cluster_remains_a_hard_gate() -> None:
    candidate = _new()
    request = replace(
        _request(candidate),
        sector_exposures=(
            SectorExposure(
                normalized_symbol="EXISTING",
                market="equity_kr",
                beneficial_owner_id="owner-1",
                sector_cluster="software",
            ),
        ),
    )

    plan = _consumer().plan(request)

    assert plan.selected == ()
    assert _rejection_codes(plan)[candidate.normalized_symbol] == (
        "max_symbols_per_sector_cluster"
    )


@pytest.mark.asyncio
async def test_sector_cap_advisory_is_persisted_through_the_service_seam(
    db_session,
) -> None:
    candidate = replace(
        _new(f"ADVISORY-{uuid.uuid4().hex.upper()}"),
        post_fill_sector_concentration_pct=Decimal("10.01"),
    )
    service = OrderProposalsService(db_session)

    result = await _consumer().consume(
        _request(candidate),
        proposal_creator=service,
    )

    assert result.proposal_creation_status == "created_after_atomic_seam"
    assert len(result.proposals_created) == 1
    proposal_id = result.proposals_created[0].proposal_id
    # The advisory must survive the proposal-creation write rather than merely
    # exist in the consumer's in-memory plan.  Expiring the session forces the
    # following fresh service read through the database.
    await db_session.commit()
    db_session.expire_all()
    fresh_service = OrderProposalsService(db_session)
    group, _rungs = await fresh_service.get_proposal(proposal_id)
    assert group.source_asof["sector_cluster_cap_advisories"] == [
        {
            "code": "sector_cluster_cap_exceeded",
            "sector_cluster": "software",
            "post_fill_sector_concentration_pct": "10.01",
            "sector_cluster_cap_pct": "10",
            "post_fill_sector_increase": "0.01",
        }
    ]


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
async def test_missing_seam_capability_blocks_proposal_creator_even_for_selected_candidate() -> (
    None
):
    class IncompleteProposalCreator:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def create_proposal(self, **kwargs):
            self.calls.append(kwargs)
            return {"unexpected": True}

    spy = IncompleteProposalCreator()
    result = await _consumer().consume(_request(_new()), proposal_creator=spy)

    assert ATOMICITY_STANCE == "watch_to_order_scope_seam_required"
    assert result.proposal_creation_status == ATOMICITY_BLOCK_CODE
    assert result.plan.proposal_creation_permitted is False
    assert result.plan.proposal_creation_block_code == ATOMICITY_BLOCK_CODE
    assert result.plan.unatomicity_notice is not None
    assert spy.calls == []


@pytest.mark.asyncio
async def test_noncanonical_candidate_account_id_stops_before_seam_inspection() -> None:
    class SeamSpy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def inspect_watch_to_order_scope(self, **kwargs):
            self.calls.append("inspect")
            raise AssertionError("non-canonical account id must not inspect")

        async def create_proposal_in_watch_to_order_scope(self, *args, **kwargs):
            self.calls.append("create")
            raise AssertionError("non-canonical account id must not create")

    spy = SeamSpy()
    result = await _consumer().consume(
        _request(replace(_new(), broker_account_id=" acct-1 ")),
        proposal_creator=spy,
    )

    assert result.plan.selected == ()
    assert (
        _rejection_codes(result.plan)["NEW"]
        == "broker_account_id_normalization_unavailable"
    )
    assert result.proposal_creation_status == "not_attempted_no_selected_candidates"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_noncanonical_cash_account_id_stops_before_seam_inspection() -> None:
    class SeamSpy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def inspect_watch_to_order_scope(self, **kwargs):
            self.calls.append("inspect")
            raise AssertionError("non-canonical account id must not inspect")

        async def create_proposal_in_watch_to_order_scope(self, *args, **kwargs):
            self.calls.append("create")
            raise AssertionError("non-canonical account id must not create")

    spy = SeamSpy()
    result = await _consumer().consume(
        _request(_new(), cash=(_cash(broker_account_id="acct-1 "),)),
        proposal_creator=spy,
    )

    assert result.plan.selected == ()
    assert _rejection_codes(result.plan)["NEW"] == "cash_snapshot_unavailable"
    assert result.proposal_creation_status == "not_attempted_no_selected_candidates"
    assert spy.calls == []


@pytest.mark.asyncio
async def test_consume_inspects_all_scopes_before_companion_create_and_keeps_loss_cut_blocked(
    db_session, monkeypatch
) -> None:
    suffix = uuid.uuid4().hex.upper()
    first = _new(
        f"A-SEAM-{suffix}",
        required_cash="100000",
        sector="software",
    )
    second = _new(
        f"B-SEAM-{suffix}",
        required_cash="100000",
        sector="hardware",
    )
    service = OrderProposalsService(db_session)
    events: list[tuple[str, str, str | None]] = []
    original_inspect = service.inspect_watch_to_order_scope
    original_create = service.create_proposal_in_watch_to_order_scope

    async def record_inspect(**kwargs):
        events.append(("inspect", kwargs["symbol"], kwargs["broker_account_id"]))
        return await original_inspect(**kwargs)

    async def record_create(inspection, **kwargs):
        events.append(("create", kwargs["symbol"], inspection.scope.broker_account_id))
        return await original_create(inspection, **kwargs)

    monkeypatch.setattr(service, "inspect_watch_to_order_scope", record_inspect)
    monkeypatch.setattr(
        service,
        "create_proposal_in_watch_to_order_scope",
        record_create,
    )

    result = await _consumer().consume(
        _request(first, second), proposal_creator=service
    )

    assert result.proposal_creation_status == "created_after_atomic_seam"
    assert result.plan.proposal_creation_permitted is True
    assert result.plan.proposal_creation_block_code is None
    assert result.plan.unatomicity_notice is None
    assert events == [
        ("inspect", first.normalized_symbol, None),
        ("inspect", first.normalized_symbol, "acct-1"),
        ("inspect", second.normalized_symbol, None),
        ("inspect", second.normalized_symbol, "acct-1"),
        ("create", first.normalized_symbol, "acct-1"),
        ("create", second.normalized_symbol, "acct-1"),
    ]
    assert len(result.proposals_created) == 2
    for created in result.proposals_created:
        group, rungs = await service.get_proposal(created.proposal_id)
        assert group.side == "buy"
        assert group.action == "place"
        assert group.exit_intent is None
        assert group.exit_reason is None
        assert [(rung.side, rung.state) for rung in rungs] == [
            ("buy", "pending_approval")
        ]


@pytest.mark.asyncio
async def test_concrete_scope_active_group_blocks_companion_create(
    db_session, monkeypatch
) -> None:
    symbol = f"CONCRETE-ACTIVE-{uuid.uuid4().hex.upper()}"
    service = OrderProposalsService(db_session)
    await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="acct-1",
        side="buy",
        order_type="limit",
        proposer="concrete-probe",
        strategy="concrete_scope_probe",
        action="place",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("91.2"), None)],
    )
    companion_calls = 0

    async def forbid_companion_create(*args, **kwargs):
        nonlocal companion_calls
        companion_calls += 1
        raise AssertionError("active concrete scope must block before companion create")

    monkeypatch.setattr(
        service,
        "create_proposal_in_watch_to_order_scope",
        forbid_companion_create,
    )

    result = await _consumer().consume(_request(_new(symbol)), proposal_creator=service)

    assert (
        result.proposal_creation_status == "watch_to_order_scope_active_groups_present"
    )
    assert result.proposals_created == ()
    assert companion_calls == 0


@pytest.mark.asyncio
async def test_probe_b_legacy_none_scope_blocks_concrete_create(
    db_session, monkeypatch
) -> None:
    symbol = f"PROBE-B-{uuid.uuid4().hex.upper()}"
    service = OrderProposalsService(db_session)
    legacy = await service.create_proposal(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id=None,
        side="buy",
        order_type="limit",
        proposer="legacy-probe",
        strategy="legacy_scope_probe",
        action="place",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("91.2"), None)],
    )
    companion_calls = 0

    async def forbid_companion_create(*args, **kwargs):
        nonlocal companion_calls
        companion_calls += 1
        raise AssertionError("legacy None scope must block before companion create")

    monkeypatch.setattr(
        service,
        "create_proposal_in_watch_to_order_scope",
        forbid_companion_create,
    )

    result = await _consumer().consume(_request(_new(symbol)), proposal_creator=service)

    assert result.proposal_creation_status == "legacy_unscoped_active_proposal_exists"
    assert result.proposals_created == ()
    assert companion_calls == 0
    concrete = await service.inspect_watch_to_order_scope(
        symbol=symbol,
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id="acct-1",
        action="place",
    )
    assert concrete.lock_acquired is True
    assert concrete.active_groups == ()
    assert legacy.broker_account_id is None


def test_missing_precheck_evidence_fails_closed_before_selection() -> None:
    plan = _consumer().plan(_request(_new(), self_unfilled_complete=False))

    assert plan.selected == ()
    assert _rejection_codes(plan)["NEW"] == "self_unfilled_order_read_unavailable"
