"""Fail-closed candidate consumer for ``buy.support_reserve_net``.

This module owns deterministic selection and proposal persistence only through
the public watcher-scope seam.  It does not read a broker, open a DB session,
register a scheduler, invoke MCP, or send an order.  Its caller supplies the
same ``OrderProposalsService`` instance and uncommitted transaction for every
inspect/create pair.

``consume`` verifies the actual seam capability on that supplied object.  It
then locks and inspects both the legacy unscoped account representation and the
canonical concrete account scope before any companion create.  A missing
capability, lock failure, active group, or account-id representation ambiguity
is a zero-create result.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Final, Literal, Protocol, TypeGuard

from app.core.symbol import to_db_symbol
from app.schemas.trading_policy import SupportReserveNetDecisionRule
from app.services.order_proposals.service import RungInput, WatchToOrderScopeInspection
from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

Market = Literal["equity_kr", "equity_us", "crypto"]
CandidateIntent = Literal["new", "add"]
ReserveNetState = Literal["armed", "open", "filled"]

STRATEGY: Final = "buy.support_reserve_net"
ATOMICITY_STANCE: Final = "watch_to_order_scope_seam_required"
ATOMICITY_BLOCK_CODE: Final = "atomic_self_open_order_read_seam_unavailable"
UNATOMICITY_NOTICE: Final = "watch-to-order scope seam의 실제 inspect/create capability가 없으면 proposal은 생성하지 않는다"
PROPOSAL_CREATION_CALL_SITE: Final = (
    "SupportReserveNetConsumer._create_after_atomic_watch_to_order_scope_check"
)

# §56차 3항 (b).  This is an additional fail-closed deployment boundary,
# deliberately not a policy-file edit owned by #1840.
KR_NEW_MIN_AVAILABLE_CASH: Final = Decimal("400000")

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
    """Public seam capability required for an automatic proposal create.

    The consumer never falls back to ordinary ``create_proposal``.  A caller
    must supply the same service instance and open transaction for both seam
    operations.
    """

    async def inspect_watch_to_order_scope(
        self,
        symbol: str,
        market: str,
        account_mode: str,
        broker_account_id: str | None,
        action: str = "place",
    ) -> WatchToOrderScopeInspection: ...

    async def create_proposal_in_watch_to_order_scope(
        self,
        inspection: WatchToOrderScopeInspection,
        **kwargs: Any,
    ) -> Any: ...


def _atomic_self_open_order_read_seam_available(
    proposal_creator: object | None,
) -> TypeGuard[ProposalCreator]:
    """Check the supplied object for both public seam operations at runtime."""

    return proposal_creator is not None and all(
        callable(getattr(proposal_creator, method, None))
        for method in (
            "inspect_watch_to_order_scope",
            "create_proposal_in_watch_to_order_scope",
        )
    )


# This preserves the original safety-gate name while making it a runtime
# predicate over the caller-supplied public seam, never a static ``True``.
_ATOMIC_SELF_OPEN_ORDER_READ_SEAM_AVAILABLE: Final = (
    _atomic_self_open_order_read_seam_available
)


def _canonical_broker_account_id(value: object) -> str | None:
    """Accept only the exact opaque account-id representation from evidence."""

    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


@dataclass(frozen=True, slots=True)
class CashSnapshot:
    """Fresh, caller-collected cash evidence for one account/currency.

    ``broker_account_id`` is an opaque canonical identifier.  It must be the
    exact non-empty representation supplied by the account-evidence source;
    this consumer does not guess aliases, case, punctuation, or a missing id.
    """

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

    ``broker_account_id`` follows ``CashSnapshot``'s opaque canonical-id
    contract.  A non-canonical or unavailable identity rejects the candidate
    before any seam inspection or proposal create.
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
class SectorClusterCapAdvisory:
    """A retained projected-concentration warning for a selected candidate."""

    normalized_symbol: str
    intent: CandidateIntent
    sector_cluster: str
    post_fill_sector_concentration_pct: Decimal
    sector_cluster_cap_pct: Decimal
    post_fill_sector_increase: Decimal
    code: Literal["sector_cluster_cap_exceeded"] = "sector_cluster_cap_exceeded"


@dataclass(frozen=True, slots=True)
class PreparedReserveNetProposal:
    """A selected payload that may be persisted only through the public seam."""

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
    sector_cluster_cap_advisories: tuple[SectorClusterCapAdvisory, ...] = ()


@dataclass(frozen=True, slots=True)
class ReserveNetPlan:
    """Deterministic selection output before seam inspection and persistence."""

    selected: tuple[PreparedReserveNetProposal, ...]
    rejected: tuple[CandidateRejection, ...]
    sector_cluster_cap_advisories: tuple[SectorClusterCapAdvisory, ...] = ()
    atomicity_stance: str = ATOMICITY_STANCE
    proposal_creation_permitted: bool = False
    proposal_creation_block_code: str | None = ATOMICITY_BLOCK_CODE
    unatomicity_notice: str | None = UNATOMICITY_NOTICE


@dataclass(frozen=True, slots=True)
class ReserveNetConsumeResult:
    plan: ReserveNetPlan
    proposal_creation_status: str
    proposal_creation_call_site: str = PROPOSAL_CREATION_CALL_SITE
    proposals_created: tuple[Any, ...] = ()


class ReserveNetPolicyContractError(ValueError):
    """The loaded policy stopped matching the signed reserve-net literal."""


class SupportReserveNetConsumer:
    """Policy-driven selector with a seam-gated proposal boundary."""

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
        sector_cluster_cap_advisories: list[SectorClusterCapAdvisory] = []
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
            sector_cluster_cap_advisories.extend(prepared.sector_cluster_cap_advisories)
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
            sector_cluster_cap_advisories.extend(prepared.sector_cluster_cap_advisories)
            selected_add_markets.add(market_key)
            self._claim(
                candidate, counted_symbols, sector_symbols, selected_required_cash
            )

        return ReserveNetPlan(
            selected=tuple(selected),
            rejected=tuple(rejections),
            sector_cluster_cap_advisories=tuple(sector_cluster_cap_advisories),
        )

    async def consume(
        self,
        request: ReserveNetRequest,
        *,
        proposal_creator: ProposalCreator | None = None,
    ) -> ReserveNetConsumeResult:
        """Plan, then create only through a live atomic seam capability."""

        plan = self.plan(request)
        if not plan.selected:
            return ReserveNetConsumeResult(
                plan=plan,
                proposal_creation_status="not_attempted_no_selected_candidates",
            )

        if not _ATOMIC_SELF_OPEN_ORDER_READ_SEAM_AVAILABLE(proposal_creator):
            return ReserveNetConsumeResult(
                plan=plan,
                proposal_creation_status=ATOMICITY_BLOCK_CODE,
            )

        plan = replace(
            plan,
            proposal_creation_permitted=True,
            proposal_creation_block_code=None,
            unatomicity_notice=None,
        )
        status, created = await self._create_after_atomic_watch_to_order_scope_check(
            plan, proposal_creator
        )
        return ReserveNetConsumeResult(
            plan=plan,
            proposal_creation_status=status,
            proposals_created=tuple(created),
        )

    async def _create_after_atomic_watch_to_order_scope_check(
        self,
        plan: ReserveNetPlan,
        proposal_creator: ProposalCreator,
    ) -> tuple[str, list[Any]]:
        """Inspect every scope before a companion create in the same transaction."""

        inspections: list[
            tuple[PreparedReserveNetProposal, WatchToOrderScopeInspection]
        ] = []
        for proposal in plan.selected:
            # The legacy ``None`` account scope is distinct from a concrete
            # account scope in the seam.  It is inspected and locked first so
            # an active unscoped proposal blocks this automatic create rather
            # than being silently skipped by the concrete-scope read.
            legacy_inspection = await proposal_creator.inspect_watch_to_order_scope(
                symbol=proposal.normalized_symbol,
                market=proposal.market,
                account_mode=proposal.account_mode,
                broker_account_id=None,
                action="place",
            )
            if not legacy_inspection.lock_acquired:
                return "watch_to_order_scope_lock_unavailable", []
            if legacy_inspection.active_groups:
                return "legacy_unscoped_active_proposal_exists", []

            # Residual race: this serializes callers using the seam, but the
            # manual MCP ``order_proposal_create`` path does not hold this
            # lock.  The dedicated runbook records the remaining window and
            # its operating constraint; this consumer never treats it as a
            # proof that an out-of-seam manual create cannot intervene.
            inspection = await proposal_creator.inspect_watch_to_order_scope(
                symbol=proposal.normalized_symbol,
                market=proposal.market,
                account_mode=proposal.account_mode,
                broker_account_id=proposal.broker_account_id,
                action="place",
            )
            if not inspection.lock_acquired:
                return "watch_to_order_scope_lock_unavailable", []
            if inspection.active_groups:
                return "watch_to_order_scope_active_groups_present", []
            inspections.append((proposal, inspection))

        # Do not commit or roll back here: every companion create must retain
        # the same service instance and transaction that acquired its scope
        # reservation.  A caller owns the later commit/rollback boundary.
        created: list[Any] = []
        for proposal, inspection in inspections:
            created.append(
                await proposal_creator.create_proposal_in_watch_to_order_scope(
                    inspection,
                    **self._proposal_create_kwargs(proposal),
                )
            )
        return "created_after_atomic_seam", created

    @staticmethod
    def _proposal_create_kwargs(
        proposal: PreparedReserveNetProposal,
    ) -> dict[str, Any]:
        """Build the buy-only, place-only payload accepted by the seam."""
        source_asof: dict[str, Any] = {
            "policy_version": proposal.policy_version,
            "policy_content_hash": proposal.policy_content_hash,
        }
        if proposal.sector_cluster_cap_advisories:
            source_asof["sector_cluster_cap_advisories"] = [
                {
                    "code": advisory.code,
                    "sector_cluster": advisory.sector_cluster,
                    "post_fill_sector_concentration_pct": str(
                        advisory.post_fill_sector_concentration_pct
                    ),
                    "sector_cluster_cap_pct": str(advisory.sector_cluster_cap_pct),
                    "post_fill_sector_increase": str(
                        advisory.post_fill_sector_increase
                    ),
                }
                for advisory in proposal.sector_cluster_cap_advisories
            ]

        return {
            "symbol": proposal.normalized_symbol,
            "market": proposal.market,
            "account_mode": proposal.account_mode,
            "broker_account_id": proposal.broker_account_id,
            "side": "buy",
            "order_type": proposal.order_type,
            "proposer": "support_reserve_net_consumer",
            "strategy": proposal.strategy,
            "action": "place",
            "rungs": [
                RungInput(
                    rung_index=0,
                    side="buy",
                    quantity=proposal.quantity,
                    limit_price=proposal.limit_price,
                    notional=None,
                )
            ],
            "thesis": proposal.thesis,
            "rationale": {
                "tier": STRATEGY,
                "intent": proposal.intent,
                "tif": proposal.tif,
                "required_cash": str(proposal.required_cash),
            },
            "lot_context": {
                "beneficial_owner_id": proposal.beneficial_owner_id,
                "approval_route": proposal.approval_route,
            },
            "source_asof": source_asof,
        }

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
        broker_account_id = _canonical_broker_account_id(candidate.broker_account_id)
        if broker_account_id is None:
            return "broker_account_id_normalization_unavailable"
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
        ):
            return "sector_concentration_negative_data"
        if not " ".join(candidate.thesis.split()):
            return "thesis_required"

        cash_key = (
            candidate.account_mode,
            broker_account_id,
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
        broker_account_id = _canonical_broker_account_id(candidate.broker_account_id)
        if broker_account_id is None:
            raise ReserveNetPolicyContractError(
                "broker account id became unavailable after candidate gating"
            )
        approval_route = (
            "human_approval_required"
            if candidate.account_mode == "toss_live"
            else "policy_auto_classification_required"
        )
        return PreparedReserveNetProposal(
            normalized_symbol=candidate.normalized_symbol,
            market=candidate.market,
            account_mode=candidate.account_mode,
            broker_account_id=broker_account_id,
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
            sector_cluster_cap_advisories=(
                (advisory,)
                if (advisory := self._sector_cluster_cap_advisory(candidate))
                is not None
                else ()
            ),
        )

    def _sector_cluster_cap_advisory(
        self, candidate: ReserveNetCandidate
    ) -> SectorClusterCapAdvisory | None:
        """Keep cap excess observable without using it as an admission gate."""
        if candidate.post_fill_sector_concentration_pct <= self._sector_cap_pct:
            return None
        sector_cluster = (candidate.sector_cluster or "").strip()
        if not sector_cluster:
            # ``_candidate_gate`` rejects this before selection.  Keep this
            # defensive guard so a future call-site cannot turn unknown sector
            # data into a misleading advisory.
            return None
        return SectorClusterCapAdvisory(
            normalized_symbol=candidate.normalized_symbol,
            intent=candidate.intent,
            sector_cluster=sector_cluster,
            post_fill_sector_concentration_pct=(
                candidate.post_fill_sector_concentration_pct
            ),
            sector_cluster_cap_pct=self._sector_cap_pct,
            post_fill_sector_increase=candidate.post_fill_sector_increase,
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
            broker_account_id = _canonical_broker_account_id(snapshot.broker_account_id)
            if broker_account_id is None:
                continue
            key = (
                snapshot.account_mode,
                broker_account_id,
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
            and _canonical_broker_account_id(cash.broker_account_id)
            == cash.broker_account_id
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
