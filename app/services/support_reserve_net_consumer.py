"""Fail-closed candidate consumer for ``buy.support_reserve_net``.

This module deliberately owns *selection*, not broker access.  It is an
operator-policy consumer which turns already-collected evidence into a bounded
set of proposal-shaped candidates.  It does not read a broker, open a DB
session, register a scheduler, invoke MCP, or send an order.

ATOMICITY_STANCE = (a): **지금은 원자적이지 않다**.  The public
``OrderProposalsService`` surface has no transactionally locked read for a
beneficial owner's same-symbol non-terminal order.  Calling
``create_proposal`` after a best-effort read could therefore coexist with a
resting order.  Until the dedicated public seam exists, ``consume`` stops at
the proposal-creation boundary even when every candidate gate passes.

The dormant call site is intentionally kept small and explicit so the future
seam job has one integration point to review.  The hard false gate below is
not an environment setting and is not operator-configurable.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Final, Literal, Protocol

from app.core.symbol import to_db_symbol
from app.schemas.trading_policy import SupportReserveNetDecisionRule
from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

Market = Literal["equity_kr", "equity_us", "crypto"]
CandidateIntent = Literal["new", "add"]
ReserveNetState = Literal["armed", "open", "filled"]

STRATEGY: Final = "buy.support_reserve_net"
ATOMICITY_STANCE: Final = "a_candidate_only_before_proposal_creation"
ATOMICITY_BLOCK_CODE: Final = "atomic_self_open_order_read_seam_unavailable"
UNATOMICITY_NOTICE: Final = (
    "지금은 원자적이지 않다: same-symbol self-open-order read+lock public seam이 없다"
)
PROPOSAL_CREATION_CALL_SITE: Final = (
    "SupportReserveNetConsumer._create_after_atomic_self_open_order_check"
)

# §56차 3항 (b).  This is an additional fail-closed deployment boundary,
# deliberately not a policy-file edit owned by #1840.
KR_NEW_MIN_AVAILABLE_CASH: Final = Decimal("400000")

# The seam does not exist in this revision.  Do not replace this with an env
# flag, a best-effort list_recent read, or a direct repository import.
_ATOMIC_SELF_OPEN_ORDER_READ_SEAM_AVAILABLE: Final = False

_SUPPORTED_ACCOUNT_MODES: Final[dict[str, frozenset[str]]] = {
    "equity_kr": frozenset({"kis_live", "toss_live"}),
    "equity_us": frozenset({"kis_live", "toss_live"}),
    "crypto": frozenset({"upbit"}),
}
_CURRENCY_FOR_MARKET: Final[dict[str, str]] = {
    "equity_kr": "KRW",
    "equity_us": "USD",
    "crypto": "KRW",
}
_ACTIVE_RESERVE_NET_STATES: Final = frozenset({"armed", "open"})
_COUNTED_RESERVE_NET_STATES: Final = frozenset({"armed", "open", "filled"})


class ProposalCreator(Protocol):
    """Future-only public proposal-service port.

    The existing ``OrderProposalsService`` implements this method.  The type is
    deliberately structural so planning remains free of DB/model imports.
    """

    async def create_proposal(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class CashSnapshot:
    """Fresh, caller-collected cash evidence for one account/currency."""

    account_mode: str
    broker_account_id: str
    currency: str
    fresh_broker_orderable_cash: Decimal
    net_orderable_cash: Decimal
    all_pending_buy_required_cash: Decimal
    reserve_net_armed_required_cash: Decimal
    is_fresh: bool
    same_account_currency_pending_accounted: bool


@dataclass(frozen=True, slots=True)
class ReserveNetAttribution:
    """A durable reserve-net-attributable symbol state supplied by a caller."""

    normalized_symbol: str
    market: Market
    beneficial_owner_id: str
    account_mode: str
    broker_account_id: str
    state: ReserveNetState
    strategy: str = STRATEGY
    sector_cluster: str | None = None
    intent: CandidateIntent | None = None


@dataclass(frozen=True, slots=True)
class SelfUnfilledOrder:
    """Known non-terminal buy order, regardless of its originating strategy."""

    normalized_symbol: str
    market: Market
    beneficial_owner_id: str
    account_mode: str
    broker_account_id: str
    side: str = "buy"


@dataclass(frozen=True, slots=True)
class SectorExposure:
    """A complete caller-provided sector exposure needed for the one-symbol cap."""

    normalized_symbol: str
    market: Market
    beneficial_owner_id: str
    sector_cluster: str


@dataclass(frozen=True, slots=True)
class ReserveNetCandidate:
    """Evidence needed to decide one new-entry or averaging-down candidate.

    ``deep_loss_pct`` is retained for diagnostics only.  It is deliberately
    absent from ``_rank_key``: a deeper loss must never be a positive ranking
    criterion for an add.
    """

    normalized_symbol: str
    market: Market
    account_mode: str
    broker_account_id: str
    beneficial_owner_id: str
    intent: CandidateIntent
    current_price: Decimal
    support_price: Decimal
    support_strength: str
    independent_support_families: tuple[str, ...]
    honest_upside_pct: Decimal
    regular_gate_failure: str
    discount_below_support_pct: Decimal
    proposed_limit_price: Decimal
    price_tick: Decimal
    quantity: Decimal
    required_cash: Decimal
    sector_cluster: str | None
    post_fill_sector_concentration_pct: Decimal
    post_fill_sector_increase: Decimal
    thesis: str
    r931_review_status: str | None = None
    r931_review_age_days: Decimal | None = None
    policy_table_age_hours: Decimal | None = None
    held_cost_basis: Decimal | None = None
    held_average_price: Decimal | None = None
    held_quantity: Decimal | None = None
    lot_size: Decimal | None = None
    reserve_net_add_fill_for_policy_version: bool = False
    deep_loss_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReserveNetRequest:
    """All evidence is injected; this consumer performs no reads of its own."""

    candidates: tuple[ReserveNetCandidate, ...]
    cash_snapshots: tuple[CashSnapshot, ...]
    reserve_net_attributions: tuple[ReserveNetAttribution, ...]
    self_unfilled_orders: tuple[SelfUnfilledOrder, ...]
    sector_exposures: tuple[SectorExposure, ...]
    self_unfilled_order_read_complete: bool
    sector_exposure_complete: bool
    submissions_frozen: bool = False


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    normalized_symbol: str
    intent: CandidateIntent
    code: str


@dataclass(frozen=True, slots=True)
class PreparedReserveNetProposal:
    """A proposal-shaped candidate, never a persisted proposal in this release."""

    normalized_symbol: str
    market: Market
    account_mode: str
    broker_account_id: str
    beneficial_owner_id: str
    intent: CandidateIntent
    quantity: Decimal
    limit_price: Decimal
    required_cash: Decimal
    thesis: str
    strategy: Literal["buy.support_reserve_net"]
    order_type: Literal["limit"]
    tif: Literal["DAY"]
    approval_route: str
    policy_version: str
    policy_content_hash: str


@dataclass(frozen=True, slots=True)
class ReserveNetPlan:
    """Deterministic output of the candidate-only phase."""

    selected: tuple[PreparedReserveNetProposal, ...]
    rejected: tuple[CandidateRejection, ...]
    atomicity_stance: str = ATOMICITY_STANCE
    proposal_creation_permitted: Literal[False] = False
    proposal_creation_block_code: str = ATOMICITY_BLOCK_CODE
    unatomicity_notice: str = UNATOMICITY_NOTICE


@dataclass(frozen=True, slots=True)
class ReserveNetConsumeResult:
    plan: ReserveNetPlan
    proposal_creation_status: str
    proposal_creation_call_site: str = PROPOSAL_CREATION_CALL_SITE
    proposals_created: tuple[Any, ...] = ()


class ReserveNetPolicyContractError(ValueError):
    """The loaded policy stopped matching the signed reserve-net literal."""


class SupportReserveNetConsumer:
    """Policy-driven selector with an intentionally closed proposal boundary."""

    def __init__(
        self,
        *,
        policy: SupportReserveNetDecisionRule,
        policy_version: str,
        policy_content_hash: str,
        sector_cap_pct: Decimal,
    ) -> None:
        self._policy = policy
        self._policy_version = policy_version
        self._policy_content_hash = policy_content_hash
        self._sector_cap_pct = sector_cap_pct
        self._assert_signed_policy_contract()

    @classmethod
    def from_current_policy(cls) -> SupportReserveNetConsumer:
        """Build from the read-only, operator-owned policy source of truth."""

        document = load_trading_policy()
        rule = document.decision_rules.get(STRATEGY)
        if not isinstance(rule, SupportReserveNetDecisionRule):
            raise ReserveNetPolicyContractError(
                f"{STRATEGY} is absent or has an unexpected policy type"
            )
        sector_cap = document.thresholds.get("portfolio.sector_cluster_cap_pct")
        if sector_cap is None or not isinstance(sector_cap.value, (int, float)):
            raise ReserveNetPolicyContractError("sector cap policy is unavailable")
        stamp = policy_version_stamp()
        return cls(
            policy=rule,
            policy_version=stamp["version"],
            policy_content_hash=stamp["content_hash"],
            sector_cap_pct=Decimal(str(sector_cap.value)),
        )

    def plan(self, request: ReserveNetRequest) -> ReserveNetPlan:
        """Select bounded candidates only; no persistence or broker activity."""

        rejections: list[CandidateRejection] = []
        if request.submissions_frozen:
            return self._reject_all(request, "fill_triage_freeze_new_submits")
        if not request.self_unfilled_order_read_complete:
            return self._reject_all(request, "self_unfilled_order_read_unavailable")
        if not request.sector_exposure_complete:
            return self._reject_all(request, "sector_exposure_unavailable")

        cash_by_key, ambiguous_cash_keys = self._cash_index(request.cash_snapshots)
        conflicts = self._candidate_conflicts(request.candidates)
        counted_symbols = self._counted_reserve_net_symbols(
            request.reserve_net_attributions
        )
        active_reserve_symbols = self._active_reserve_net_symbols(
            request.reserve_net_attributions
        )
        active_add_markets = self._active_add_markets(request.reserve_net_attributions)
        self_unfilled_symbols = self._self_unfilled_symbols(
            request.self_unfilled_orders
        )
        sector_symbols = self._sector_symbols(request.sector_exposures)

        eligible_new: list[ReserveNetCandidate] = []
        eligible_add: list[ReserveNetCandidate] = []
        for candidate in request.candidates:
            code = self._candidate_gate(
                candidate,
                cash_by_key=cash_by_key,
                ambiguous_cash_keys=ambiguous_cash_keys,
                candidate_conflicts=conflicts,
                active_reserve_symbols=active_reserve_symbols,
                self_unfilled_symbols=self_unfilled_symbols,
            )
            if code is not None:
                rejections.append(self._rejection(candidate, code))
                continue
            if candidate.intent == "new":
                eligible_new.append(candidate)
            else:
                eligible_add.append(candidate)

        selected: list[PreparedReserveNetProposal] = []
        selected_add_markets: set[tuple[str, str]] = set()
        selected_required_cash: defaultdict[tuple[str, str, str], Decimal] = (
            defaultdict(Decimal)
        )

        # POOL_ORDER is intentionally explicit.  All eligible new candidates
        # are considered first; only then can a maximum of one add per market
        # use an otherwise unclaimed slot.
        for candidate in sorted(eligible_new, key=self._rank_key):
            code = self._allocation_gate(
                candidate,
                counted_symbols=counted_symbols,
                sector_symbols=sector_symbols,
                cash_by_key=cash_by_key,
                selected_required_cash=selected_required_cash,
            )
            if code is not None:
                rejections.append(self._rejection(candidate, code))
                continue
            prepared = self._prepare(candidate)
            selected.append(prepared)
            self._claim(
                candidate, counted_symbols, sector_symbols, selected_required_cash
            )

        for candidate in sorted(eligible_add, key=self._rank_key):
            market_key = (candidate.beneficial_owner_id, candidate.market)
            if market_key in selected_add_markets or market_key in active_add_markets:
                rejections.append(
                    self._rejection(candidate, "max_add_symbols_per_market")
                )
                continue
            code = self._allocation_gate(
                candidate,
                counted_symbols=counted_symbols,
                sector_symbols=sector_symbols,
                cash_by_key=cash_by_key,
                selected_required_cash=selected_required_cash,
            )
            if code is not None:
                rejections.append(self._rejection(candidate, code))
                continue
            prepared = self._prepare(candidate)
            selected.append(prepared)
            selected_add_markets.add(market_key)
            self._claim(
                candidate, counted_symbols, sector_symbols, selected_required_cash
            )

        return ReserveNetPlan(selected=tuple(selected), rejected=tuple(rejections))

    async def consume(
        self,
        request: ReserveNetRequest,
        *,
        proposal_creator: ProposalCreator | None = None,
    ) -> ReserveNetConsumeResult:
        """Plan, then stop before proposal creation until the atomic seam lands."""

        plan = self.plan(request)
        if not plan.selected:
            return ReserveNetConsumeResult(
                plan=plan,
                proposal_creation_status="not_attempted_no_selected_candidates",
            )

        # 지금은 원자적이지 않다.  A non-atomic read followed by create is not
        # a safety check; it is a TOCTOU window that can create a second live
        # resting buy.  This branch is deliberately unreachable in this job.
        if not _ATOMIC_SELF_OPEN_ORDER_READ_SEAM_AVAILABLE:
            return ReserveNetConsumeResult(
                plan=plan,
                proposal_creation_status=ATOMICITY_BLOCK_CODE,
            )

        if proposal_creator is None:
            # Kept for the future seam integration, where the caller must own
            # the proposal transaction and post-commit dispatch boundary.
            return ReserveNetConsumeResult(
                plan=plan,
                proposal_creation_status="proposal_creator_unavailable",
            )
        created = await self._create_after_atomic_self_open_order_check(
            plan, proposal_creator
        )
        return ReserveNetConsumeResult(
            plan=plan,
            proposal_creation_status="created_after_atomic_seam",
            proposals_created=tuple(created),
        )

    async def _create_after_atomic_self_open_order_check(
        self,
        plan: ReserveNetPlan,
        proposal_creator: ProposalCreator,
    ) -> list[Any]:
        """The sole future call location for ``OrderProposalsService.create_proposal``.

        The required public atomic read+lock seam is intentionally not
        implemented here.  A future seam owner must make the caller prove that
        it held the lock and observed no same-owner/symbol non-terminal order
        immediately before this method becomes reachable.
        """

        # Delayed import keeps candidate planning free of proposal models and
        # makes this explicit dependency dormant while the hard gate is false.
        from app.services.order_proposals.service import RungInput

        created: list[Any] = []
        for proposal in plan.selected:
            created.append(
                await proposal_creator.create_proposal(
                    symbol=proposal.normalized_symbol,
                    market=proposal.market,
                    account_mode=proposal.account_mode,
                    broker_account_id=proposal.broker_account_id,
                    side="buy",
                    order_type=proposal.order_type,
                    proposer="support_reserve_net_consumer",
                    strategy=proposal.strategy,
                    rungs=[
                        RungInput(
                            rung_index=0,
                            side="buy",
                            quantity=proposal.quantity,
                            limit_price=proposal.limit_price,
                            notional=None,
                        )
                    ],
                    thesis=proposal.thesis,
                    rationale={
                        "tier": STRATEGY,
                        "intent": proposal.intent,
                        "tif": proposal.tif,
                        "required_cash": str(proposal.required_cash),
                    },
                    lot_context={
                        "beneficial_owner_id": proposal.beneficial_owner_id,
                        "approval_route": proposal.approval_route,
                    },
                    source_asof={
                        "policy_version": proposal.policy_version,
                        "policy_content_hash": proposal.policy_content_hash,
                    },
                )
            )
        return created

    def _assert_signed_policy_contract(self) -> None:
        """Reject a malformed policy object rather than silently widening it."""

        policy = self._policy
        expected = (
            policy.regular_discovery_precedence is True
            and policy.eligible_only_when_regular_gate_failure == "RSI_ONLY"
            and policy.support_strength_min == "moderate"
            and policy.independent_support_source_count_min == 2
            and policy.support_within_current_pct_max == 8
            and policy.honest_upside_pct_min == 40
            and policy.discount_below_support_pct_range == [5, 10]
            and policy.final_limit_distance_from_current_pct_range == [-15, -5]
            and policy.order_type == "limit"
            and policy.tif == "DAY"
            and policy.tier_armed_required_cash_cap_pct == 50
            and policy.all_pending_buy_required_cash_hard_cap_pct == 90
            and policy.max_owned_or_open_symbols_per_market == 2
            and policy.max_active_orders_per_symbol == 1
            and self._sector_cap_pct == Decimal("10")
            and policy.priority_rules.allocation_order
            == [
                "dedupe_active_or_resting_same_symbol",
                "first_slot_eligible_new_candidate",
                "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
            ]
            and policy.add_candidate.k_used == 0.10
            and policy.add_candidate.partial_A_limit_fill == "FORBIDDEN"
        )
        if not expected:
            raise ReserveNetPolicyContractError(
                "support_reserve_net policy literal changed; consumer remains closed"
            )

    def _candidate_gate(
        self,
        candidate: ReserveNetCandidate,
        *,
        cash_by_key: dict[tuple[str, str, str], CashSnapshot],
        ambiguous_cash_keys: set[tuple[str, str, str]],
        candidate_conflicts: dict[tuple[str, str, str], str],
        active_reserve_symbols: set[tuple[str, str, str]],
        self_unfilled_symbols: set[tuple[str, str, str]],
    ) -> str | None:
        if candidate.market not in _SUPPORTED_ACCOUNT_MODES:
            return "market_not_supported"
        normalized = self._normalize_symbol(
            candidate.normalized_symbol, candidate.market
        )
        candidate_key = (
            candidate.beneficial_owner_id,
            candidate.market,
            normalized,
        )
        if not candidate.beneficial_owner_id.strip():
            return "beneficial_owner_required"
        if not candidate.broker_account_id.strip():
            return "broker_account_id_required"
        if not normalized or normalized != candidate.normalized_symbol:
            return "normalized_symbol_required"
        if conflict_code := candidate_conflicts.get(candidate_key):
            return conflict_code
        if candidate.account_mode not in _SUPPORTED_ACCOUNT_MODES[candidate.market]:
            return "account_mode_not_supported_for_market"
        if candidate_key in active_reserve_symbols:
            return "reserve_net_active_or_resting_symbol_exists"
        if candidate_key in self_unfilled_symbols:
            return "self_unfilled_buy_exists"
        if not (candidate.sector_cluster or "").strip():
            return "unknown_sector_ineligible"
        if (
            candidate.post_fill_sector_increase < 0
            or candidate.post_fill_sector_concentration_pct < 0
            or candidate.post_fill_sector_concentration_pct > self._sector_cap_pct
        ):
            return "sector_concentration_unavailable_or_cap_exceeded"
        if not " ".join(candidate.thesis.split()):
            return "thesis_required"

        cash_key = (
            candidate.account_mode,
            candidate.broker_account_id,
            self._currency(candidate.market),
        )
        if cash_key in ambiguous_cash_keys:
            return "cash_snapshot_ambiguous"
        cash = cash_by_key.get(cash_key)
        if cash is None:
            return "cash_snapshot_unavailable"
        if not self._cash_is_usable(cash):
            return "cash_snapshot_unavailable"

        policy = self._policy
        if (
            candidate.regular_gate_failure
            != policy.eligible_only_when_regular_gate_failure
        ):
            return "regular_gate_failure_not_rsi_only"
        if candidate.support_strength not in {"moderate", "strong"}:
            return "support_strength_below_moderate"
        if not self._independent_families_ok(candidate.independent_support_families):
            return "independent_support_families_insufficient"
        if candidate.honest_upside_pct < Decimal(policy.honest_upside_pct_min):
            return "honest_upside_below_minimum"
        if not self._anchor_is_valid(candidate):
            return "anchor_not_tick_floor_or_outside_band"
        if candidate.quantity <= 0 or candidate.required_cash <= 0:
            return "quantity_or_required_cash_non_positive"
        if (
            candidate.required_cash
            < candidate.quantity * candidate.proposed_limit_price
        ):
            return "required_cash_below_executable_notional"

        if candidate.intent == "new":
            if candidate.market == "equity_kr" and (
                cash.net_orderable_cash < KR_NEW_MIN_AVAILABLE_CASH
            ):
                return "kr_new_min_available_cash_not_met"
            return None
        if candidate.intent != "add":
            return "unknown_candidate_intent"
        return self._add_gate(candidate)

    def _add_gate(self, candidate: ReserveNetCandidate) -> str | None:
        """Apply the add-only feasibility gates before it enters pool two."""

        if candidate.market == "equity_us":
            # There is no US thesis-review contract, so do not turn a policy
            # table row into an averaging-down right.
            return "us_add_disabled_thesis_review_contract_missing"
        policy = self._policy.add_candidate
        if candidate.r931_review_status != policy.r931_review_required:
            return "r931_review_not_pass"
        if (
            candidate.r931_review_age_days is None
            or candidate.r931_review_age_days > policy.r931_review_max_age_days
        ):
            return "r931_review_stale_or_missing"
        if (
            candidate.policy_table_age_hours is None
            or candidate.policy_table_age_hours > policy.policy_table_max_age_hours
        ):
            return "policy_table_stale_or_missing"
        if candidate.reserve_net_add_fill_for_policy_version:
            return "reserve_net_add_fill_already_exists_for_policy_version"
        if (
            candidate.held_cost_basis is None
            or candidate.held_average_price is None
            or candidate.held_quantity is None
            or candidate.lot_size is None
            or candidate.held_cost_basis <= 0
            or candidate.held_average_price <= 0
            or candidate.held_quantity <= 0
            or candidate.lot_size <= 0
        ):
            return "add_cost_basis_or_lot_context_unavailable"

        a_limit = self._a_limit_10(candidate)
        if a_limit <= 0:
            return "a_limit_lte_zero_no_order"
        expected_quantity = self._ceil_to_lot(
            a_limit / candidate.proposed_limit_price,
            candidate.lot_size,
        )
        if candidate.quantity < expected_quantity:
            return "a_limit_not_fully_satisfied"
        if candidate.quantity > expected_quantity:
            return "a_limit_exceeds_full_recalculation"
        post_fill_average = (
            candidate.held_cost_basis
            + candidate.quantity * candidate.proposed_limit_price
        ) / (candidate.held_quantity + candidate.quantity)
        target_average = candidate.proposed_limit_price * (
            Decimal(1) + Decimal(str(policy.k_used))
        )
        if post_fill_average > target_average:
            return "a_limit_not_fully_satisfied"
        return None

    def _allocation_gate(
        self,
        candidate: ReserveNetCandidate,
        *,
        counted_symbols: dict[tuple[str, str], set[str]],
        sector_symbols: dict[tuple[str, str, str], set[str]],
        cash_by_key: dict[tuple[str, str, str], CashSnapshot],
        selected_required_cash: defaultdict[tuple[str, str, str], Decimal],
    ) -> str | None:
        owner_market = (candidate.beneficial_owner_id, candidate.market)
        current_symbols = counted_symbols[owner_market]
        if (
            candidate.normalized_symbol not in current_symbols
            and len(current_symbols)
            >= self._policy.max_owned_or_open_symbols_per_market
        ):
            return "max_reserve_net_symbols_per_market"
        sector_key = (
            candidate.beneficial_owner_id,
            candidate.market,
            str(candidate.sector_cluster),
        )
        existing_sector_symbols = sector_symbols[sector_key]
        if (
            candidate.normalized_symbol not in existing_sector_symbols
            and existing_sector_symbols
        ):
            return "max_symbols_per_sector_cluster"
        cash_key = (
            candidate.account_mode,
            candidate.broker_account_id,
            self._currency(candidate.market),
        )
        cash = cash_by_key[cash_key]
        selected_cash = selected_required_cash[cash_key]
        if candidate.required_cash > cash.net_orderable_cash - selected_cash:
            return "net_orderable_cash_insufficient"
        all_pending_after = (
            cash.all_pending_buy_required_cash + selected_cash + candidate.required_cash
        )
        global_cap = (
            cash.fresh_broker_orderable_cash
            * Decimal(self._policy.all_pending_buy_required_cash_hard_cap_pct)
            / Decimal("100")
        )
        if all_pending_after > global_cap:
            return "all_pending_buy_hard_cap_exceeded"
        tier_after = (
            cash.reserve_net_armed_required_cash
            + selected_cash
            + candidate.required_cash
        )
        tier_cap = (
            cash.fresh_broker_orderable_cash
            * Decimal(self._policy.tier_armed_required_cash_cap_pct)
            / Decimal("100")
        )
        if tier_after > tier_cap:
            return "tier_armed_cash_cap_exceeded"
        return None

    def _prepare(self, candidate: ReserveNetCandidate) -> PreparedReserveNetProposal:
        approval_route = (
            "human_approval_required"
            if candidate.account_mode == "toss_live"
            else "policy_auto_classification_required"
        )
        return PreparedReserveNetProposal(
            normalized_symbol=candidate.normalized_symbol,
            market=candidate.market,
            account_mode=candidate.account_mode,
            broker_account_id=candidate.broker_account_id,
            beneficial_owner_id=candidate.beneficial_owner_id,
            intent=candidate.intent,
            quantity=candidate.quantity,
            limit_price=candidate.proposed_limit_price,
            required_cash=candidate.required_cash,
            thesis=" ".join(candidate.thesis.split()),
            strategy=STRATEGY,
            order_type="limit",
            tif="DAY",
            approval_route=approval_route,
            policy_version=self._policy_version,
            policy_content_hash=self._policy_content_hash,
        )

    def _anchor_is_valid(self, candidate: ReserveNetCandidate) -> bool:
        if (
            candidate.current_price <= 0
            or candidate.support_price <= 0
            or candidate.proposed_limit_price <= 0
            or candidate.price_tick <= 0
        ):
            return False
        support_distance = (
            (candidate.current_price - candidate.support_price)
            / candidate.current_price
            * Decimal("100")
        )
        if not (
            Decimal("0")
            <= support_distance
            <= Decimal(self._policy.support_within_current_pct_max)
        ):
            return False
        lower_discount, upper_discount = map(
            Decimal, self._policy.discount_below_support_pct_range
        )
        if not lower_discount <= candidate.discount_below_support_pct <= upper_discount:
            return False
        expected_limit = self._tick_floor(
            candidate.support_price
            * (Decimal(1) - candidate.discount_below_support_pct / Decimal("100")),
            candidate.price_tick,
        )
        # A range breach is EXCLUDE, never a clamp to the nearest band edge.
        if candidate.proposed_limit_price != expected_limit:
            return False
        distance_from_current = (
            (candidate.proposed_limit_price - candidate.current_price)
            / candidate.current_price
            * Decimal("100")
        )
        low, high = map(
            Decimal, self._policy.final_limit_distance_from_current_pct_range
        )
        return low <= distance_from_current <= high

    def _independent_families_ok(self, families: tuple[str, ...]) -> bool:
        normalized = {family.strip().lower() for family in families if family.strip()}
        allowed = set(self._policy.independent_support_source_families)
        return (
            normalized.issubset(allowed)
            and len(normalized) >= self._policy.independent_support_source_count_min
        )

    def _rank_key(self, candidate: ReserveNetCandidate) -> tuple[Any, ...]:
        """The signed five-stage sort; deep-loss is intentionally not consulted."""

        strength = {"strong": 0, "moderate": 1}[candidate.support_strength]
        source_count = len(
            {
                family.strip().lower()
                for family in candidate.independent_support_families
                if family.strip()
            }
        )
        return (
            strength,
            -source_count,
            -candidate.honest_upside_pct,
            candidate.post_fill_sector_increase,
            candidate.required_cash,
            # §56차 2항: add exact ties are deterministic ASC.  Applying the
            # same final tie-break to new candidates is only narrowing.
            candidate.normalized_symbol,
        )

    @staticmethod
    def _tick_floor(price: Decimal, tick: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 28
            units = (price / tick).to_integral_value(rounding=ROUND_FLOOR)
            return units * tick

    @staticmethod
    def _ceil_to_lot(quantity: Decimal, lot_size: Decimal) -> Decimal:
        with localcontext() as context:
            context.prec = 28
            units = (quantity / lot_size).to_integral_value(rounding=ROUND_CEILING)
            return units * lot_size

    @staticmethod
    def _a_limit_10(candidate: ReserveNetCandidate) -> Decimal:
        assert candidate.held_cost_basis is not None
        assert candidate.held_average_price is not None
        return (
            candidate.held_cost_basis
            * (
                Decimal(1)
                - (candidate.proposed_limit_price / candidate.held_average_price)
                * Decimal("1.10")
            )
            / Decimal("0.10")
        )

    @staticmethod
    def _cash_index(
        snapshots: tuple[CashSnapshot, ...],
    ) -> tuple[dict[tuple[str, str, str], CashSnapshot], set[tuple[str, str, str]]]:
        index: dict[tuple[str, str, str], CashSnapshot] = {}
        ambiguous: set[tuple[str, str, str]] = set()
        for snapshot in snapshots:
            key = (
                snapshot.account_mode,
                snapshot.broker_account_id,
                snapshot.currency,
            )
            if key in index:
                ambiguous.add(key)
            index[key] = snapshot
        return index, ambiguous

    @staticmethod
    def _cash_is_usable(cash: CashSnapshot) -> bool:
        return (
            cash.is_fresh
            and cash.same_account_currency_pending_accounted
            and bool(cash.broker_account_id.strip())
            and cash.fresh_broker_orderable_cash > 0
            and cash.net_orderable_cash >= 0
            and cash.net_orderable_cash <= cash.fresh_broker_orderable_cash
            and cash.all_pending_buy_required_cash >= 0
            and cash.reserve_net_armed_required_cash >= 0
        )

    def _counted_reserve_net_symbols(
        self,
        attributions: tuple[ReserveNetAttribution, ...],
    ) -> dict[tuple[str, str], set[str]]:
        result: dict[tuple[str, str], set[str]] = defaultdict(set)
        for attribution in attributions:
            if (
                attribution.strategy == STRATEGY
                and attribution.state in _COUNTED_RESERVE_NET_STATES
            ):
                result[(attribution.beneficial_owner_id, attribution.market)].add(
                    self._normalize_symbol(
                        attribution.normalized_symbol, attribution.market
                    )
                )
        return result

    def _active_reserve_net_symbols(
        self,
        attributions: tuple[ReserveNetAttribution, ...],
    ) -> set[tuple[str, str, str]]:
        return {
            (
                attribution.beneficial_owner_id,
                attribution.market,
                self._normalize_symbol(
                    attribution.normalized_symbol, attribution.market
                ),
            )
            for attribution in attributions
            if (
                attribution.strategy == STRATEGY
                and attribution.state in _ACTIVE_RESERVE_NET_STATES
            )
        }

    @staticmethod
    def _active_add_markets(
        attributions: tuple[ReserveNetAttribution, ...],
    ) -> set[tuple[str, str]]:
        return {
            (attribution.beneficial_owner_id, attribution.market)
            for attribution in attributions
            if (
                attribution.strategy == STRATEGY
                and attribution.intent == "add"
                and attribution.state in _ACTIVE_RESERVE_NET_STATES
            )
        }

    def _self_unfilled_symbols(
        self,
        orders: tuple[SelfUnfilledOrder, ...],
    ) -> set[tuple[str, str, str]]:
        return {
            (
                order.beneficial_owner_id,
                order.market,
                self._normalize_symbol(order.normalized_symbol, order.market),
            )
            for order in orders
            if order.side == "buy"
        }

    def _sector_symbols(
        self,
        exposures: tuple[SectorExposure, ...],
    ) -> dict[tuple[str, str, str], set[str]]:
        result: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for exposure in exposures:
            result[
                (
                    exposure.beneficial_owner_id,
                    exposure.market,
                    exposure.sector_cluster,
                )
            ].add(self._normalize_symbol(exposure.normalized_symbol, exposure.market))
        return result

    def _candidate_conflicts(
        self,
        candidates: tuple[ReserveNetCandidate, ...],
    ) -> dict[tuple[str, str, str], str]:
        candidates_by_key: defaultdict[
            tuple[str, str, str], list[ReserveNetCandidate]
        ] = defaultdict(list)
        for candidate in candidates:
            candidates_by_key[
                (
                    candidate.beneficial_owner_id,
                    candidate.market,
                    self._normalize_symbol(
                        candidate.normalized_symbol, candidate.market
                    ),
                )
            ].append(candidate)
        conflicts: dict[tuple[str, str, str], str] = {}
        for key, same_symbol_candidates in candidates_by_key.items():
            if len(same_symbol_candidates) <= 1:
                continue
            intents = {candidate.intent for candidate in same_symbol_candidates}
            conflicts[key] = (
                "new_add_same_symbol_conflict"
                if len(intents) > 1
                else "duplicate_candidate_symbol"
            )
        return conflicts

    @staticmethod
    def _currency(market: Market) -> str:
        return _CURRENCY_FOR_MARKET[market]

    @staticmethod
    def _normalize_symbol(symbol: str, market: Market) -> str:
        normalized = symbol.strip().upper()
        if market == "equity_us":
            # DB-standard . notation is delegated to the shared conversion
            # helper; do not hand-roll '-'/'/' replacement in this consumer.
            return to_db_symbol(normalized)
        return normalized

    @staticmethod
    def _rejection(candidate: ReserveNetCandidate, code: str) -> CandidateRejection:
        return CandidateRejection(
            normalized_symbol=candidate.normalized_symbol,
            intent=candidate.intent,
            code=code,
        )

    def _claim(
        self,
        candidate: ReserveNetCandidate,
        counted_symbols: dict[tuple[str, str], set[str]],
        sector_symbols: dict[tuple[str, str, str], set[str]],
        selected_required_cash: defaultdict[tuple[str, str, str], Decimal],
    ) -> None:
        counted_symbols[(candidate.beneficial_owner_id, candidate.market)].add(
            candidate.normalized_symbol
        )
        sector_symbols[
            (
                candidate.beneficial_owner_id,
                candidate.market,
                str(candidate.sector_cluster),
            )
        ].add(candidate.normalized_symbol)
        selected_required_cash[
            (
                candidate.account_mode,
                candidate.broker_account_id,
                self._currency(candidate.market),
            )
        ] += candidate.required_cash

    def _reject_all(self, request: ReserveNetRequest, code: str) -> ReserveNetPlan:
        return ReserveNetPlan(
            selected=(),
            rejected=tuple(
                self._rejection(candidate, code) for candidate in request.candidates
            ),
        )


__all__ = [
    "ATOMICITY_BLOCK_CODE",
    "ATOMICITY_STANCE",
    "CandidateRejection",
    "CashSnapshot",
    "KR_NEW_MIN_AVAILABLE_CASH",
    "PROPOSAL_CREATION_CALL_SITE",
    "PreparedReserveNetProposal",
    "ProposalCreator",
    "ReserveNetAttribution",
    "ReserveNetCandidate",
    "ReserveNetConsumeResult",
    "ReserveNetPlan",
    "ReserveNetPolicyContractError",
    "ReserveNetRequest",
    "SectorExposure",
    "STRATEGY",
    "SelfUnfilledOrder",
    "SupportReserveNetConsumer",
    "UNATOMICITY_NOTICE",
]
