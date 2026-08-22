"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Lane = Literal["buy", "sell", "discovery"]
Market = Literal["kr", "us", "crypto"]
PostureStateName = Literal[
    "RESTING",
    "CONDITIONAL",
    "ARMED_DEFERRED",
    "DISARMED",
    "EXPIRED_REARMABLE",
]

ThresholdValue = int | float | str | list[int | float]
RuleConditionValue = int | float | str | bool | list[int | float | str | bool]
PolicyComparison = Literal["gt", "gte", "lt", "lte", "eq"]
KrBroker = Literal["kis", "toss"]


class OneShareExceptionPolicy(BaseModel):
    """ROB-956 — US shares can't be bought fractionally; if a single share's
    price exceeds a USD notional band's ceiling, allow exactly one share
    instead of blocking the entry outright. absolute_ceiling_usd still hard-
    blocks ultra-high-priced symbols (BRK.A/NVR-class); max_deep_rungs caps
    additional averaging-down exposure on exception entries."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    absolute_ceiling_usd: float
    max_deep_rungs: int


class PolicyThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    value: ThresholdValue
    unit: str
    semantics: str
    of: int | None = None
    one_share_exception: OneShareExceptionPolicy | None = None


class PolicyDecisionRuleTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    conditions: dict[str, RuleConditionValue]
    action: str
    sizing: str


BREAKEVEN_EXTENSION_LADDER_TIER_ID = "breakeven_extension_ladder"
NO_RESISTANCE_REFERENCE_EXCLUSION = "no_resistance_reference"
_RESISTANCE_CONDITION_MARKER = "resistance_near_pct"

# §139차 (2026-08-22) — the two tiers added by §139차 are the first decision
# rules that are not meaningful in every market: the index-ETF admission is a
# KR/US equity-universe rule, and held_majors_support_net is a crypto-only
# LIVE tier. ``markets`` is the structural scope declaration that keeps a
# crypto live tier from being quoted back to a KR or US buy session.
INDEX_ETF_CANDIDATE_TIER_ID = "index_etf_candidate"
HELD_MAJORS_SUPPORT_NET_TIER_ID = "held_majors_support_net"
_NOT_APPLICABLE_GATE = "not_applicable_structurally_absent"
_INDEX_ETF_REQUIRED_EXCLUSIONS = ("leveraged_etf", "inverse_etf")
# §139차 B2 — the per-tier validators below key off the TIER id, so renaming a
# tier silently skips every pin on it. Bind each §139차 rule KEY to the tier id
# it must declare, checked at document level where the key is known.
_S139_REQUIRED_TIER_IDS = {
    "buy.index_etf_candidate": INDEX_ETF_CANDIDATE_TIER_ID,
    "buy.held_majors_support_net": HELD_MAJORS_SUPPORT_NET_TIER_ID,
}
_HELD_MAJORS_REQUIRED_EXCLUSIONS = (
    "new_coin_entry",
    "unheld_symbol",
    "losing_position_averaging_down",
    "market_order",
    "crash_day_new_batch",
)


class PolicyDecisionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    semantics: str
    tiers: list[PolicyDecisionRuleTier]
    tie_breaks: dict[str, str] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)
    # None means "every market", which is what every pre-§139차 rule declares
    # by omission; the field is additive and back-compatible.
    markets: list[Market] | None = None

    @model_validator(mode="after")
    def validate_markets_scope_is_non_empty(self) -> PolicyDecisionRule:
        """An empty ``markets`` list would silently hide the rule everywhere.

        Omitting the key is the sanctioned way to say "all markets"; declaring
        ``markets: []`` is almost certainly a truncation accident, so it fails
        the build instead of quietly disabling a live tier.
        """

        if self.markets is not None and not self.markets:
            raise ValueError(
                "markets must be omitted (all markets) or list at least one market"
            )
        return self

    @model_validator(mode="after")
    def validate_index_etf_candidate_stays_an_equal_candidate(
        self,
    ) -> PolicyDecisionRule:
        """§139차 ③ — the ETF is admitted as a competitor, not as a fallback.

        The retrospective rejected the *allocation* form of this idea ("if N
        rounds produce no candidate, park idle cash in an index ETF") on
        measured grounds: KR 069500 +9.62% vs US VOO -0.51% in the same
        window, i.e. the sign flips by market and picking the market after
        the fact is hindsight. What the operator approved is the narrower
        thing — universe admission on equal terms. Every way the narrow form
        could drift back into the rejected one is asserted here.
        """

        tier = self._tier_by_id(INDEX_ETF_CANDIDATE_TIER_ID)
        if tier is None:
            return self
        conditions = tier.conditions

        # NOT A FALLBACK — none of the three allocation-shaped behaviours may
        # be switched on.
        for key in (
            "slot_reserved_for_etf",
            "promoted_when_candidate_set_empty",
            "idle_cash_allocation_rule",
            "etf_specific_sizing_multiplier",
        ):
            if conditions.get(key) is not False:
                raise ValueError(
                    f"{INDEX_ETF_CANDIDATE_TIER_ID} must declare {key}: false"
                )
        if conditions.get("ranked_against_equities_in_same_pool") is not True:
            raise ValueError(
                f"{INDEX_ETF_CANDIDATE_TIER_ID} must declare "
                "ranked_against_equities_in_same_pool: true"
            )

        # NOT A WAIVER — the gates an ETF *can* satisfy stay on; the ones it
        # structurally cannot are marked not-applicable, never "waived", so no
        # equity can ever inherit the relaxation.
        for key in (
            "rsi_gate_applies",
            "support_strength_gate_applies",
            "support_distance_gate_applies",
        ):
            if conditions.get(key) is not True:
                raise ValueError(f"{INDEX_ETF_CANDIDATE_TIER_ID} must keep {key}: true")
        for key in ("honest_upside_gate", "analyst_gate"):
            if conditions.get(key) != _NOT_APPLICABLE_GATE:
                raise ValueError(
                    f"{INDEX_ETF_CANDIDATE_TIER_ID} must declare {key}: "
                    f"{_NOT_APPLICABLE_GATE}"
                )

        missing = [
            name
            for name in _INDEX_ETF_REQUIRED_EXCLUSIONS
            if name not in self.exclusions
        ]
        if missing:
            raise ValueError(
                f"{INDEX_ETF_CANDIDATE_TIER_ID} must retain exclusions {missing}"
            )
        # Equity-universe rule; crypto has no index ETF to admit.
        if self.markets != ["kr", "us"]:
            raise ValueError(
                f"{INDEX_ETF_CANDIDATE_TIER_ID} is scoped to markets [kr, us]"
            )
        return self

    @model_validator(mode="after")
    def validate_held_majors_support_net_stays_bounded_and_time_boxed(
        self,
    ) -> PolicyDecisionRule:
        """§139차 ⑤ — the only LIVE tier here, so its bounds are machine-pinned.

        This is a pre-registered hypothesis ("상승장이라면 지지선에 매수 걸면
        돈 벌 거잖아") running against measured counter-evidence: ROB-1031's
        60% post-touch breakdown rate with a negative D+20 median, and the
        XRP -8.36% one-hour flush on 2026-08-22. A pre-registration is only
        worth the name if its scope, its size, and its retirement condition
        cannot be edited after the fact, so each is asserted here rather than
        left to prose.
        """

        tier = self._tier_by_id(HELD_MAJORS_SUPPORT_NET_TIER_ID)
        if tier is None:
            return self
        conditions = tier.conditions

        # SCOPE — held, profitable, crypto. The new-coin discovery gate is
        # explicitly untouched, which is only true while this cannot admit an
        # unheld or losing symbol.
        if conditions.get("holding_required") is not True:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must require holding_required: true"
            )
        if conditions.get("unrealized_pnl_pct_min_exclusive") != 0:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must require "
                "unrealized_pnl_pct_min_exclusive: 0 (profitable holdings only)"
            )
        if conditions.get("new_coin_discovery_gate_unchanged") is not True:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must declare "
                "new_coin_discovery_gate_unchanged: true"
            )
        if self.markets != ["crypto"]:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} is scoped to markets [crypto]"
            )

        # ANCHOR — moderate strength is the relaxation the operator approved;
        # the >= 2 independent sources are what pays for it. Neither may drift.
        if conditions.get("support_strength_min") != "moderate":
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} anchor strength must be moderate"
            )
        source_min = conditions.get("independent_support_source_count_min")
        if not isinstance(source_min, int) or isinstance(source_min, bool):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} requires a numeric "
                "independent_support_source_count_min"
            )
        if source_min < 2:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} requires at least 2 "
                "independent support sources"
            )
        band = conditions.get("support_distance_from_current_pct_range")
        if (
            not isinstance(band, list)
            or len(band) != 2
            or any(
                not isinstance(edge, int | float) or isinstance(edge, bool)
                for edge in band
            )
        ):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} requires a two-number "
                "support_distance_from_current_pct_range"
            )
        if band != [-12, -3]:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} band must be [-12, -3]"
            )

        # EXECUTION — resting limit only. A market order here would buy the
        # flush instead of resting under it.
        if conditions.get("order_type") != "limit":
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must be order_type: limit"
            )
        if conditions.get("resting_only") is not True:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must be resting_only: true"
            )
        if conditions.get("per_order_cap_raised") is not False:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must declare "
                "per_order_cap_raised: false — it rides the existing cap"
            )

        # SIZE — per-coin and tier caps, and the per-(coin, level) once rule.
        per_coin = conditions.get("max_notional_krw_per_coin")
        per_tier = conditions.get("max_notional_krw_per_tier")
        if per_coin != 300000:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} per-coin cap must be 300000 KRW"
            )
        if per_tier != 900000:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} tier cap must be 900000 KRW"
            )
        if (
            conditions.get(
                "max_placements_per_coin_per_support_level_per_policy_version"
            )
            != 1
        ):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} allows one placement per "
                "coin per support level per policy version"
            )

        # SCORING — a pre-registration with no scoring obligation is just an
        # authorization, and the retirement bar must predate the batches.
        if conditions.get("forecast_save_required") is not True:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must require forecast_save"
            )
        review_date = conditions.get("review_date")
        if not isinstance(review_date, str):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} requires a review_date"
            )
        date.fromisoformat(review_date)
        if conditions.get("retire_unless_filled_cohort_d20_median_pct_min") != 0:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} retirement bar requires a "
                "filled-cohort D+20 median floor of 0"
            )
        if (
            conditions.get(
                "retire_unless_filled_cohort_d20_lower_quartile_pct_min_exclusive"
            )
            != -8
        ):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} retirement bar requires a "
                "filled-cohort D+20 lower-quartile floor of -8"
            )

        # ENFORCEMENT SURFACE (B1) — the tier is advisory like every other
        # tier here. Saying so is the honest description, so it is pinned:
        # a later edit claiming this tier is code-enforced, or claiming a
        # machine "major" allowlist exists, fails the build instead of
        # shipping a promise the repo cannot keep.
        if conditions.get("enforcement_surface") != "advisory_session_contract":
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must declare "
                "enforcement_surface: advisory_session_contract"
            )
        if (
            conditions.get("code_enforced_boundary")
            != "crypto_per_order_auto_approve_cap_then_card"
        ):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must name the only "
                "code-enforced boundary: crypto_per_order_auto_approve_cap_then_card"
            )
        if (
            conditions.get("major_classification")
            != "session_judgment_no_machine_allowlist"
        ):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must declare "
                "major_classification: session_judgment_no_machine_allowlist — "
                "the tier carries no coin allowlist or classifier"
            )

        # CRASH REGIME — explicitly NOT the preplanned ladder's "keep".
        if conditions.get("crash_day_new_batch_suspended") is not True:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must suspend new batches on "
                "a crash day"
            )
        drawdown = conditions.get("crypto_crash_24h_drawdown_pct_max")
        if not isinstance(drawdown, int | float) or isinstance(drawdown, bool):
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} requires a numeric "
                "crypto_crash_24h_drawdown_pct_max"
            )
        # Pinned, not bounded: a MORE negative threshold is the loosening that
        # matters (it keeps batching through a deeper crash), and a less
        # negative one silently redefines "crash" for this tier only. §139차
        # fixed the number at -10%, so drift in either direction fails.
        if drawdown != -10:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} crash suspension triggers at a "
                "-10% 24h drawdown"
            )

        missing = [
            name
            for name in _HELD_MAJORS_REQUIRED_EXCLUSIONS
            if name not in self.exclusions
        ]
        if missing:
            raise ValueError(
                f"{HELD_MAJORS_SUPPORT_NET_TIER_ID} must retain exclusions {missing}"
            )
        return self

    def _tier_by_id(self, tier_id: str) -> PolicyDecisionRuleTier | None:
        matches = [tier for tier in self.tiers if tier.id == tier_id]
        if not matches:
            return None
        if len(matches) > 1:
            raise ValueError(f"{tier_id} must be declared exactly once")
        return matches[0]

    @model_validator(mode="after")
    def validate_breakeven_extension_ladder_stays_a_fallback(
        self,
    ) -> PolicyDecisionRule:
        """ROB-1298 §115차 — machine-enforce the fallback contract.

        The §115차 tier closes the zero-fresh-named-resistance profit-take gap.
        It must stay a *fallback* for that excluded case, never a replacement
        for the named-resistance tiers and never a route that reopens them by
        deleting the ``no_resistance_reference`` exclusion. Every invariant the
        operator forbade drifting is asserted here so the drift fails the build
        instead of silently shipping.
        """

        tier_ids = [tier.id for tier in self.tiers]
        if BREAKEVEN_EXTENSION_LADDER_TIER_ID not in tier_ids:
            return self

        # NO_EXCLUSION_REMOVAL — the dedicated tier is the only sanctioned fix;
        # dropping the exclusion is explicitly not.
        if NO_RESISTANCE_REFERENCE_EXCLUSION not in self.exclusions:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} requires the "
                f"{NO_RESISTANCE_REFERENCE_EXCLUSION!r} exclusion to be retained"
            )

        # FALLBACK_ORDER — declared last, so first-match-wins can only reach it
        # after every named-resistance tier has failed.
        if tier_ids[-1] != BREAKEVEN_EXTENSION_LADDER_TIER_ID:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must be the last declared "
                "tier so named-resistance tiers keep priority"
            )
        if tier_ids.count(BREAKEVEN_EXTENSION_LADDER_TIER_ID) != 1:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must be declared exactly once"
            )

        priority = self.tie_breaks.get("tier_priority")
        if priority is None:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} requires tie_breaks."
                "tier_priority to state the fallback order"
            )
        if [part.strip() for part in priority.split(">")] != tier_ids:
            raise ValueError(
                "tie_breaks.tier_priority must match the declared tier order "
                f"{tier_ids}"
            )

        tier = self.tiers[-1]
        conditions = tier.conditions

        # Eligibility — reachable only when the holding has zero fresh named
        # resistance, i.e. exactly the excluded case.
        if conditions.get("fresh_named_resistance_count_eq") != 0:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must require "
                "fresh_named_resistance_count_eq: 0"
            )
        if (
            conditions.get("matched_exclusion_case")
            != NO_RESISTANCE_REFERENCE_EXCLUSION
        ):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must declare "
                f"matched_exclusion_case: {NO_RESISTANCE_REFERENCE_EXCLUSION}"
            )
        if conditions.get("resistance_tier_fallback_only") is not True:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must declare "
                "resistance_tier_fallback_only: true"
            )
        # A resistance-proximity condition here would let the fallback tier
        # speak about holdings that still have a resistance frame.
        resistance_keys = [
            key for key in conditions if _RESISTANCE_CONDITION_MARKER in key
        ]
        if resistance_keys:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must not carry "
                f"resistance-proximity conditions: {resistance_keys}"
            )

        # SIZING_REUSE — no new quantity rule is invented by this tier.
        if tier.sizing != "existing_trim_rule":
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must reuse the existing "
                "trim sizing rule (sizing: existing_trim_rule)"
            )

        # Anchors — 3 strictly ascending profit-side multiples of average cost,
        # the lowest of which quotes the existing loss-guard multiple key.
        if (
            conditions.get("anchor_lowest_rung_policy_key")
            != "sell.loss_guard_min_multiple"
        ):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} lowest rung must quote "
                "sell.loss_guard_min_multiple"
            )
        multiples = conditions.get("anchor_average_cost_multiples")
        if not isinstance(multiples, list) or not multiples:
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} requires a non-empty "
                "anchor_average_cost_multiples list"
            )
        if any(
            not isinstance(value, int | float) or isinstance(value, bool)
            for value in multiples
        ):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} anchor multiples must be numeric"
            )
        if conditions.get("rungs_max") != len(multiples):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} rungs_max must equal the "
                "number of declared anchor multiples"
            )
        if any(value <= 1 for value in multiples):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} anchor multiples must all "
                "sit above average cost"
            )
        if any(a >= b for a, b in zip(multiples, multiples[1:], strict=False)):
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} anchor multiples must be "
                "strictly ascending"
            )

        # A sell-side rung rounds up onto the tick grid; rounding down could
        # place a rung below the loss-guard anchor it quotes.
        if conditions.get("tick_snap_direction") != "ceil":
            raise ValueError(
                f"{BREAKEVEN_EXTENSION_LADDER_TIER_ID} must snap rungs up "
                "(tick_snap_direction: ceil)"
            )

        return self


class SupportReserveNetAutoSubmitNotional(BaseModel):
    """The existing, unchanged auto-submit ceiling by settlement currency."""

    model_config = ConfigDict(extra="forbid")

    krw: Literal[200000]
    usd: Literal[150]


class SupportReserveNetCashReservation(BaseModel):
    """Reserve-net cash accounting contract; advisory until a future consumer.

    This schema deliberately describes the fail-closed accounting boundary but
    does not implement it in an order-proposal or broker service.
    """

    model_config = ConfigDict(extra="forbid")

    net_orderable: Literal[
        "fresh_broker_orderable_cash_minus_same_account_currency_pending_required_cash"
    ]
    pending_required_cash_scope: Literal["not_yet_reached_broker"]
    required_cash_primary: Literal["preview_estimated_value_plus_fee"]
    required_cash_fallback: Literal["quantity_times_limit_price"]
    broker_orderable_unavailable_or_error: Literal["FAIL_CLOSED"]
    cancel_proposal_cash_reservation: Literal[
        "KEEP_RESERVED_UNTIL_BROKER_TERMINAL_CONFIRMATION"
    ]


class SupportReserveNetFillTriagePolicy(BaseModel):
    """ROB-755 consumer contract; no broker mutation is enabled here."""

    model_config = ConfigDict(extra="forbid")

    on_first_confirmed_fill: Literal["FREEZE_NEW_SUBMITS"]
    cancellation_mode: Literal["PROPOSAL_REQUIRES_APPROVAL"]
    broker_cancel_confirmation_required_before_releasing_cash: Literal[True]
    same_session_rearm: Literal[False]
    unknown_or_ambiguous_order_state: Literal["KEEP_RESERVED_AND_BLOCK"]
    burst_key: list[str]

    @field_validator("burst_key")
    @classmethod
    def validate_burst_key(cls, value: list[str]) -> list[str]:
        required = ["broker_account_id", "currency", "market_session"]
        if value != required:
            raise ValueError(f"burst_key must be exactly {required}")
        return value


class SupportReserveNetAddCandidatePolicy(BaseModel):
    """Averaging-down feasibility contract for the reserve-net tier."""

    model_config = ConfigDict(extra="forbid")

    r931_review_required: Literal["PASS"]
    r931_review_max_age_days: Literal[7]
    policy_table_max_age_hours: Literal[36]
    k_used: float
    sizing_price: Literal["proposed_limit_price"]
    a_limit_lte_zero: Literal["NO_ORDER"]
    partial_A_limit_fill: Literal["FORBIDDEN"]
    # §136차 (2026-08-21): 1 → 2 — SOL·XRP 같은 회차 동시 add 허용.
    # 자격 게이트(R-931 PASS·지지 앵커·A_limit>0·심볼당 1회/버전) 불변.
    max_add_symbols_per_market: Literal[2]
    max_reserve_net_add_fills_per_symbol_per_policy_version: Literal[1]
    same_day_rearm_after_fill: Literal[False]
    crash_day_averaging_exemption: Literal[False]

    @field_validator("k_used")
    @classmethod
    def validate_k_used(cls, value: float) -> float:
        # §136차 검토: k는 평단 개선 목표 파라미터(새 평단 ≤ 제안가×(1+k)) —
        # 키우면 A_limit≤0(NO_ORDER)이 쉬워져 물타기 확대와 정반대로 작동한다.
        # 그래서 0.10 유지가 §136차의 결론이다.
        if value != 0.10:
            raise ValueError("k_used must be 0.10")
        return value


class SupportReserveNetPriorityRules(BaseModel):
    """Deterministic allocation order for the constrained reserve-net slots.

    This remains a consumer contract only. A later authorized consumer must use
    this order when selecting among otherwise eligible candidates.
    """

    model_config = ConfigDict(extra="forbid")

    allocation_order: list[str]
    same_symbol_active_or_resting: Literal["DEDUPE_FIRST"]
    first_slot: Literal["ELIGIBLE_NEW_CANDIDATE_FIRST"]
    add_candidate_rank: Literal["SECONDARY_CANDIDATE_POOL"]
    add_candidate_r931_review_required: Literal["PASS"]
    add_candidate_a_limit_10: Literal["FULLY_SATISFIED"]
    max_add_symbols_per_market: Literal[2]
    same_intent_class_sort_order: list[str]
    exact_tie_break: Literal["NEW_BEFORE_ADD"]

    @field_validator("allocation_order")
    @classmethod
    def validate_allocation_order(cls, value: list[str]) -> list[str]:
        required = [
            "dedupe_active_or_resting_same_symbol",
            "first_slot_eligible_new_candidate",
            "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
        ]
        if value != required:
            raise ValueError(f"allocation_order must be exactly {required}")
        return value

    @field_validator("same_intent_class_sort_order")
    @classmethod
    def validate_same_intent_class_sort_order(cls, value: list[str]) -> list[str]:
        required = [
            "support_strength_desc",
            "independent_support_source_count_desc",
            "honest_upside_pct_desc",
            "post_fill_sector_increase_asc",
            "required_cash_asc",
        ]
        if value != required:
            raise ValueError(f"same_intent_class_sort_order must be exactly {required}")
        return value


class SupportReserveNetProhibitions(BaseModel):
    """One-to-one encoding of the eight §Q4 prohibitions."""

    model_config = ConfigDict(extra="forbid")

    no_new_add_or_deep_limit_rung_overlap: Literal[True]
    aggregate_active_buy_by_beneficial_owner_across_accounts: Literal[True]
    fresh_cost_basis_quantity_and_A_limit_before_next_day_reissue: Literal[True]
    partial_A_limit_fill: Literal["FORBIDDEN"]
    candidate_zero_runtime_gate_relaxation: Literal["FORBIDDEN"]
    crash_day_averaging_exemption: Literal[False]
    cancel_proposal_is_not_broker_cancellation: Literal[True]
    unconfirmed_cancel_keeps_required_cash_reserved: Literal[True]
    market_order: Literal["FORBIDDEN"]
    gtc: Literal["FORBIDDEN"]
    multi_rung: Literal["FORBIDDEN"]
    daily_regeneration: Literal["REQUIRED"]


class SupportReserveNetDecisionRule(BaseModel):
    """Operator-owned support reserve net contract (operator §45/§46).

    It is intentionally a policy/consumer contract only.  It does not route a
    proposal, write a ledger row, invoke an MCP tool, or mutate a broker.
    """

    model_config = ConfigDict(extra="forbid")

    lanes: list[Literal["buy"]]
    semantics: str
    regular_discovery_precedence: Literal[True]
    eligible_only_when_regular_gate_failure: Literal["RSI_ONLY"]
    rsi_gate: Literal["omitted_for_this_tier_only"]
    support_strength_min: Literal["moderate"]
    independent_support_source_count_min: Literal[2]
    independent_support_source_families: list[str]
    support_within_current_pct_max: Literal[8]
    honest_upside_pct_min: Literal[40]
    honest_upside_reference: Literal["decision_time_current_price"]
    discount_below_support_pct_range: list[int]
    final_limit_distance_from_current_pct_range: list[int]
    anchor_price_formula: Literal["tick_floor(S × (1-d))"]
    final_limit_distance_out_of_range: Literal["EXCLUDE"]
    order_type: Literal["limit"]
    tif: Literal["DAY"]
    all_pending_buy_required_cash_hard_cap_pct: Literal[90]
    tier_armed_required_cash_cap_pct: Literal[50]
    max_owned_or_open_symbols_per_market: Literal[2]
    # §136차 (2026-08-21): the symbol cap bounds NEW-entry spread. An add to
    # an already-owned lot does not grow the symbol count, so it is exempt —
    # the 2026-08-21 20:20 crypto session measured this cap (5 held symbols
    # > 2) blocking every A(k) add, which was never the cap's intent.
    owned_symbol_add_exempt_from_symbol_cap: Literal[True]
    max_active_orders_per_symbol: Literal[1]
    max_symbols_per_sector_cluster: Literal[1]
    unknown_sector: Literal["INELIGIBLE"]
    auto_submit_notional: SupportReserveNetAutoSubmitNotional
    larger_notional_within_existing_band: Literal["HUMAN_APPROVAL_REQUIRED"]
    daily_auto_cap_includes_all_buy_tiers: Literal[True]
    cash_reservation: SupportReserveNetCashReservation
    fill_triage: SupportReserveNetFillTriagePolicy
    add_candidate: SupportReserveNetAddCandidatePolicy
    priority_rules: SupportReserveNetPriorityRules
    prohibitions: SupportReserveNetProhibitions
    toss_live_approval: Literal["HUMAN_APPROVAL_REQUIRED_UNTIL_VETO_WIRING"]

    @field_validator("lanes")
    @classmethod
    def validate_buy_lane_only(
        cls, value: list[Literal["buy"]]
    ) -> list[Literal["buy"]]:
        if value != ["buy"]:
            raise ValueError("support_reserve_net applies to the buy lane only")
        return value

    @field_validator("independent_support_source_families")
    @classmethod
    def validate_independent_support_families(cls, value: list[str]) -> list[str]:
        required = ["fib", "bb_lower", "volume_profile"]
        if value != required:
            raise ValueError(
                f"independent_support_source_families must be exactly {required}"
            )
        return value

    @field_validator("discount_below_support_pct_range")
    @classmethod
    def validate_discount_below_support_range(cls, value: list[int]) -> list[int]:
        if value != [5, 10]:
            raise ValueError("discount_below_support_pct_range must be [5, 10]")
        return value

    @field_validator("final_limit_distance_from_current_pct_range")
    @classmethod
    def validate_final_distance_range(cls, value: list[int]) -> list[int]:
        if value != [-15, -5]:
            raise ValueError(
                "final_limit_distance_from_current_pct_range must be [-15, -5]"
            )
        return value


class SingleShareExitScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[Literal["kr"]]
    brokers: list[KrBroker]
    required_broker_inventory: list[KrBroker]
    order_routable_required: Literal[True]

    @model_validator(mode="after")
    def validate_kis_toss_scope(self) -> SingleShareExitScope:
        required = {"kis", "toss"}
        if set(self.brokers) != required or len(self.brokers) != len(required):
            raise ValueError("brokers must contain exactly kis and toss")
        if set(self.required_broker_inventory) != required or len(
            self.required_broker_inventory
        ) != len(required):
            raise ValueError(
                "required_broker_inventory must contain exactly kis and toss"
            )
        return self


class SingleShareResistanceSourceFamilies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_profile_exact: list[str]
    fibonacci_prefixes: list[str]
    bollinger_prefixes: list[str]


class SingleShareExitConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_routable_sellable_quantity_eq: Literal[1]
    profit_pct_min: float = Field(ge=0)
    resistance_reference_required: Literal[True]
    resistance_strength_min: Literal["strong"]
    resistance_distance_pct_min_exclusive: float = Field(ge=0, le=100)
    resistance_distance_pct_max: float = Field(ge=0, le=100)
    resistance_source_family_min: int = Field(ge=2)
    resistance_source_families: SingleShareResistanceSourceFamilies
    quote_max_age_seconds: int = Field(gt=0)
    resistance_max_age_seconds: int = Field(gt=0)
    holdings_max_age_seconds: int = Field(gt=0)
    open_orders_max_age_seconds: int = Field(gt=0)
    open_actions_max_age_seconds: int = Field(gt=0)
    captured_at_max_age_seconds: int = Field(gt=0)
    snapshot_max_skew_seconds: int = Field(gt=0)
    required_completed_bar_market: Literal["XKRX"]
    min_sell_price_multiple_policy_key: Literal["sell.loss_guard_min_multiple"]
    same_symbol_open_orders_max: Literal[0]
    unresolved_open_actions_max: Literal[0]
    loss_state_uses_existing_path: Literal["loss_cut_only"]

    @model_validator(mode="after")
    def validate_resistance_band(self) -> SingleShareExitConditions:
        if (
            self.resistance_distance_pct_max
            <= self.resistance_distance_pct_min_exclusive
        ):
            raise ValueError("resistance distance max must exceed exclusive min")
        return self


class SingleShareExitProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["full_exit_at_far_resistance"]
    sizing: Literal["full_account_lot_exit"]
    approval: Literal["telegram_manual"]
    auto_approve: Literal[False]
    execution: Literal["proposal_only"]


class SingleShareExitDecisionRule(BaseModel):
    """Deprecated general fallback retained as a KR shadow/replay policy.

    This rule intentionally has a distinct shape from the tiered trim rule:
    ``sell.trim_preplace`` now includes one-share positions for a full-exit
    advisory review, while this legacy path can only classify the narrower KR
    far-resistance shadow cohort while ``proposal_enabled`` is false. Its
    candidate metadata is manual-approval-only for a separately authorized
    future activation; this schema never enables an order.
    """

    model_config = ConfigDict(extra="forbid")

    lanes: list[Literal["sell"]]
    semantics: str
    activation_state: Literal["shadow"]
    proposal_enabled: Literal[False]
    scope: SingleShareExitScope
    conditions: SingleShareExitConditions
    proposal: SingleShareExitProposal
    threshold_status: Literal["provisional"]
    operator_approval_required: Literal[True]
    recalibration_note: str


class PolicyRecoveryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    metric: str
    sources: list[str]
    operator: PolicyComparison | None
    threshold: int | float | None
    unit: str
    semantics: str


class PolicyRecoveryGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    min_conditions_met: int
    of: int
    missing_or_null_threshold: str
    conditions: list[PolicyRecoveryCondition]
    advisory_context: list[PolicyRecoveryCondition] = Field(default_factory=list)


class PolicySupportResistanceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    selection_rule: str
    source_priority: list[str]
    confluence_examples: list[list[str]]


class PolicyNoChasingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    daily_change_pct_threshold: float | None
    min_trade_value_24h_krw: int | None
    criteria: list[str]
    follow_up: str


class PreplannedSupportLadderPolicy(BaseModel):
    """ROB-1289 — advisory support-ladder planning contract."""

    model_config = ConfigDict(extra="forbid")

    lanes: list[Literal["buy"]]
    semantics: str
    enabled: bool
    eligibility: Literal["standard_buy_gates_pass"]
    rungs_max: Literal[2]
    per_rung_notional_multiplier: Annotated[float, Field(ge=0.5, le=0.5)]
    crash_day_behavior: Literal["keep"]


class CryptoMarketRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_gate: PolicyRecoveryGate
    support_resistance: PolicySupportResistanceRule
    no_chasing: PolicyNoChasingRule


class PolicyAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    governs: str
    does_not_govern: list[str]


class PosturePolicy(BaseModel):
    """ROB-1106 stage-1 feature gate and five-state shadow contract.

    Only ``shadow`` is accepted in this stage. Later pilot/live modes need
    separate authorization and implementation rather than silently widening
    this schema.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["shadow"]
    states: list[PostureStateName]
    policy_stamp_required: Literal[True]

    @field_validator("states")
    @classmethod
    def validate_exact_five_states(
        cls, value: list[PostureStateName]
    ) -> list[PostureStateName]:
        required = {
            "RESTING",
            "CONDITIONAL",
            "ARMED_DEFERRED",
            "DISARMED",
            "EXPIRED_REARMABLE",
        }
        if len(value) != len(required) or set(value) != required:
            raise ValueError(
                "posture.states must contain exactly the five posture-v1 states"
            )
        return value


class OrderProposalAutoApprovePolicy(BaseModel):
    """Default-off resting-order auto-approval thresholds (ROB-871).

    Caps are denominated in each market's settlement currency: KRW for KR
    equities and crypto, USD for US equities.

    ``breakeven_band_pct`` and ``round_trip_cost_bps`` are the operator-owned
    inputs to the expanded classification (see
    ``order_proposals/auto_approve.py``). They are optional so a deployment
    pinned to an older YAML still loads; the defaults are the same conservative
    values the code floor enforces, and the code floor means a policy edit can
    only ever make the profit-take classification *narrower*, never wider.
    """

    model_config = ConfigDict(extra="forbid")

    min_distance_pct: float = Field(gt=0, le=100)
    per_order_cap: dict[Market, float]
    daily_cap: dict[Market, float]
    breakeven_band_pct: float = Field(default=1.0, gt=0, le=100)
    round_trip_cost_bps: dict[Market, float] = Field(
        default_factory=lambda: {"kr": 47.4, "us": 90.0, "crypto": 10.0}
    )

    @field_validator("per_order_cap", "daily_cap")
    @classmethod
    def validate_market_caps(cls, value: dict[Market, float]) -> dict[Market, float]:
        required = {"kr", "us", "crypto"}
        if set(value) != required:
            raise ValueError(f"market caps must contain exactly {sorted(required)}")
        if any(cap <= 0 for cap in value.values()):
            raise ValueError("market caps must be positive")
        return value

    @field_validator("round_trip_cost_bps")
    @classmethod
    def validate_round_trip_cost(
        cls, value: dict[Market, float]
    ) -> dict[Market, float]:
        required = {"kr", "us", "crypto"}
        if set(value) != required:
            raise ValueError(
                f"round_trip_cost_bps must contain exactly {sorted(required)}"
            )
        if any(bps < 0 for bps in value.values()):
            raise ValueError("round_trip_cost_bps must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_daily_caps(self) -> OrderProposalAutoApprovePolicy:
        if any(
            self.daily_cap[market] < per_order
            for market, per_order in self.per_order_cap.items()
        ):
            raise ValueError("daily cap must be at least the per-order cap")
        return self


class OrderProposalsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve: OrderProposalAutoApprovePolicy


class CrashDayTrigger(BaseModel):
    """ROB-932 — gap-only trigger. Intraday crashes (e.g. 2026-07-13: gap
    -0.8% -> intraday -9.8%) are NOT covered by this trigger; that gap is a
    documented limitation, not an oversight."""

    model_config = ConfigDict(extra="forbid")

    index_symbol: str
    index_gap_pct_max: float


class CrashDayNewEntryHoldExceptionRequirements(BaseModel):
    """ROB-1289 — every regular buy gate remains mandatory."""

    model_config = ConfigDict(extra="forbid")

    standard_buy_gates: Literal["all_pass_including_support_quality"]
    support_quality: Literal["required"]
    price_zone: Literal["strong_support"]
    gate_relaxation: Literal["none"]


class CrashDayNewEntryHoldExceptionSizing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_symbol_notional_multiplier: Annotated[float, Field(ge=0.5, le=0.5)]
    max_new_symbols: Literal[1]


class CrashDayNewEntryHoldException(BaseModel):
    """ROB-1289 — advisory conditional release of a crash-day full hold."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    requires: CrashDayNewEntryHoldExceptionRequirements
    sizing: CrashDayNewEntryHoldExceptionSizing
    semantics: str


class CrashDayActions(BaseModel):
    """ROB-932/1289 — advisory only, no code enforcement."""

    model_config = ConfigDict(extra="forbid")

    new_entry_hold: bool
    new_entry_hold_exception: CrashDayNewEntryHoldException
    deep_rung_reprice_to_band_floor: bool
    profit_trim_marketable_allowed: bool
    defensive_brief_cross_check: bool


class CrashDayPolicy(BaseModel):
    """ROB-932 — crash-day advisory playbook. Not enforced in code; a
    cross-check reference for judgment only. defensive_trim execution support
    is out of scope for this PR."""

    model_config = ConfigDict(extra="forbid")

    trigger: CrashDayTrigger
    actions: CrashDayActions


class UserStance(BaseModel):
    """ROB-948 — user investment-stance advisory. Cited by session judgment
    (upside/downside weighting) alongside other advisory context; does not
    override fail-closed risk guards (loss-cut sizing, ladder guards) in
    code. Same advisory-only pattern as ROB-932 crash_day."""

    model_config = ConfigDict(extra="forbid")

    id: str
    stance: str
    implications: list[str]
    risk_scenario: str
    review_condition: str
    review_date: str

    @field_validator("review_date")
    @classmethod
    def validate_review_date_parses(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class TradingPolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    captured_as_of: str
    source: str
    authority: PolicyAuthority
    posture: PosturePolicy
    order_proposals: OrderProposalsPolicy
    sector_clusters: dict[str, list[str]]
    thresholds: dict[str, PolicyThreshold]
    decision_rules: dict[
        str,
        PolicyDecisionRule
        | SingleShareExitDecisionRule
        | SupportReserveNetDecisionRule
        | PreplannedSupportLadderPolicy,
    ] = Field(default_factory=dict)
    market_rules: dict[Literal["crypto"], CryptoMarketRules]
    market_overrides: dict[Market, dict[str, ThresholdValue]]
    crash_day: CrashDayPolicy
    user_stances: list[UserStance]

    @model_validator(mode="after")
    def validate_s139_rule_keys_bind_their_tier_ids(self) -> TradingPolicyDocument:
        """A §139차 rule key must carry the tier id its validators key off.

        Removing the rule entirely is a sanctioned retirement (§139차 ⓒ is
        scored and retired on 2026-09-19); renaming its tier while keeping the
        key is not — that keeps the policy surface and drops every guard.
        """

        for key, tier_id in _S139_REQUIRED_TIER_IDS.items():
            rule = self.decision_rules.get(key)
            if rule is None:
                continue
            tier_ids = [tier.id for tier in getattr(rule, "tiers", [])]
            if not isinstance(rule, PolicyDecisionRule) or tier_id not in tier_ids:
                raise ValueError(
                    f"{key} must declare tier id {tier_id!r} so its §139차 "
                    f"validators run; got {tier_ids}"
                )
        return self
