"""Typed contracts for the provisional ROB-1037 KR resting lane.

The contracts are deliberately closed: judgment inputs are enums/numbers, not
LLM-authored rationale.  The evaluator may produce shadow PLACE verdicts, but
the shipped policy keeps proposal generation, scheduling, mutation, and
auto-approval disabled.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EVIDENCE_SCHEMA_VERSION = "rob1037-evidence-v1"
DECISION_SCHEMA_VERSION = "rob1037-decision-v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KRRestingSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class KRAccountMode(StrEnum):
    KIS_LIVE = "kis_live"
    TOSS_LIVE = "toss_live"
    KIS_MOCK = "kis_mock"
    UPBIT = "upbit"
    DB_SIMULATED = "db_simulated"
    MANUAL = "manual"
    ISA_MANUAL = "isa_manual"
    RETIREMENT_MANUAL = "retirement_manual"
    UNKNOWN = "unknown"


class KRRoutableAccountMode(StrEnum):
    KIS_LIVE = "kis_live"
    TOSS_LIVE = "toss_live"


class KRSessionState(StrEnum):
    KRX_REGULAR = "krx_regular"
    PREOPEN = "preopen"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class KREvaluationStage(StrEnum):
    DISCOVERY = "discovery"
    PROPOSAL = "proposal"
    APPROVAL = "approval"


class KREvidenceStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class KRMarketDataState(StrEnum):
    LIVE = "live"
    DELAYED = "delayed"
    UNAVAILABLE = "unavailable"


class KRQuoteSource(StrEnum):
    KIS = "kis"
    TOSS = "toss"
    MARKET_DATA = "market_data"


class KRLevelKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class KRLevelSource(StrEnum):
    """Closed vocabulary covering current production and persisted spellings."""

    FIB_0 = "fib_0"
    FIB_23_6 = "fib_23.6"
    FIB_38_2 = "fib_38.2"
    FIB_50 = "fib_50"
    FIB_61_8 = "fib_61.8"
    FIB_78_6 = "fib_78.6"
    FIB_100 = "fib_100"
    FIB_RATIO_0 = "fib_0.0"
    FIB_RATIO_0_236 = "fib_0.236"
    FIB_RATIO_0_382 = "fib_0.382"
    FIB_RATIO_0_5 = "fib_0.5"
    FIB_RATIO_0_618 = "fib_0.618"
    FIB_RATIO_0_786 = "fib_0.786"
    FIB_RATIO_1 = "fib_1.0"
    BB_LOWER = "bb_lower"
    BB_MIDDLE = "bb_middle"
    BB_UPPER = "bb_upper"
    VOLUME_POC = "volume_poc"
    VOLUME_VALUE_AREA_HIGH = "volume_value_area_high"
    VOLUME_VALUE_AREA_LOW = "volume_value_area_low"


class KRSourceFamily(StrEnum):
    FIBONACCI = "fibonacci"
    BOLLINGER = "bollinger"
    VOLUME_PROFILE = "volume_profile"


class KRDirectionState(StrEnum):
    NONE = "none"
    ACCUMULATING = "accumulating"
    DISTRIBUTING = "distributing"
    CONFLICT = "conflict"


class KRConcurrencyState(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    ACQUIRED = "acquired"
    MISSING = "missing"


class KRGateState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class KROpenActionState(StrEnum):
    NONE = "none"
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class KRNearResistanceAction(StrEnum):
    PLACE_TRIM = "place_trim"
    WATCH = "watch"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class KRApprovalMode(StrEnum):
    TELEGRAM_MANUAL = "telegram_manual"


class KRRestingVerdict(StrEnum):
    BLOCK_ACCOUNT_SCOPE = "BLOCK_ACCOUNT_SCOPE"
    BLOCK_SESSION = "BLOCK_SESSION"
    BLOCK_LIFECYCLE_UNRECONCILED = "BLOCK_LIFECYCLE_UNRECONCILED"
    BLOCK_STALE_SUPPORT = "BLOCK_STALE_SUPPORT"
    BLOCK_STALE_RESISTANCE = "BLOCK_STALE_RESISTANCE"
    BLOCK_OPPOSITE_PENDING = "BLOCK_OPPOSITE_PENDING"
    BLOCK_DUPLICATE_RESTING = "BLOCK_DUPLICATE_RESTING"
    BLOCK_CASH = "BLOCK_CASH"
    DEFER_POLICY_GATE = "DEFER_POLICY_GATE"
    DEFER_COMPOSITE_EVIDENCE = "DEFER_COMPOSITE_EVIDENCE"
    PLACE_MODERATE_SUPPORT = "PLACE_MODERATE_SUPPORT"
    PLACE_DEEP_SUPPORT = "PLACE_DEEP_SUPPORT"
    HOLD_NO_SUPPORT = "HOLD_NO_SUPPORT"
    PLACE_NEAR_RESISTANCE_TRIM = "PLACE_NEAR_RESISTANCE_TRIM"
    PLACE_FAR_RESISTANCE = "PLACE_FAR_RESISTANCE"
    FULL_EXIT_SINGLE_SHARE_AT_FAR_RESISTANCE = (
        "FULL_EXIT_SINGLE_SHARE_AT_FAR_RESISTANCE"
    )
    WATCH_RESISTANCE = "WATCH_RESISTANCE"
    HOLD_LOSS_GUARD = "HOLD_LOSS_GUARD"
    HOLD_NO_RESISTANCE = "HOLD_NO_RESISTANCE"


class KRRestingReason(StrEnum):
    ACCOUNT_MODE_UNSUPPORTED = "ACCOUNT_MODE_UNSUPPORTED"
    ACCOUNT_NOT_ROUTABLE = "ACCOUNT_NOT_ROUTABLE"
    SESSION_NOT_REGULAR = "SESSION_NOT_REGULAR"
    BROKER_SCAN_MISSING = "BROKER_SCAN_MISSING"
    BROKER_OPEN_SCAN_INCOMPLETE = "BROKER_OPEN_SCAN_INCOMPLETE"
    BROKER_CLOSED_SCAN_INCOMPLETE = "BROKER_CLOSED_SCAN_INCOMPLETE"
    BROKER_PAGINATION_INCOMPLETE = "BROKER_PAGINATION_INCOMPLETE"
    LEDGER_BROKER_MISMATCH = "LEDGER_BROKER_MISMATCH"
    GHOST_RESTING_PRESENT = "GHOST_RESTING_PRESENT"
    DAY_EXPIRY_UNRECONCILED = "DAY_EXPIRY_UNRECONCILED"
    APPROVAL_REQUERY_MISSING = "APPROVAL_REQUERY_MISSING"
    SYMBOL_RESERVATION_MISSING = "SYMBOL_RESERVATION_MISSING"
    SYMBOL_LOCK_MISSING = "SYMBOL_LOCK_MISSING"
    QUOTE_MISSING = "QUOTE_MISSING"
    QUOTE_NOT_LIVE = "QUOTE_NOT_LIVE"
    QUOTE_STALE = "QUOTE_STALE"
    QUOTE_FROM_FUTURE = "QUOTE_FROM_FUTURE"
    LEVEL_SET_INCOMPLETE = "LEVEL_SET_INCOMPLETE"
    EXPECTED_BASELINE_MISMATCH = "EXPECTED_BASELINE_MISMATCH"
    LEVELS_MIXED_VINTAGE = "LEVELS_MIXED_VINTAGE"
    LEVEL_COMPUTED_IN_FUTURE = "LEVEL_COMPUTED_IN_FUTURE"
    OPPOSITE_BUY_OPEN = "OPPOSITE_BUY_OPEN"
    OPPOSITE_SELL_OPEN = "OPPOSITE_SELL_OPEN"
    SAME_SIDE_BUY_RESTING_OPEN = "SAME_SIDE_BUY_RESTING_OPEN"
    SAME_SIDE_SELL_RESTING_OPEN = "SAME_SIDE_SELL_RESTING_OPEN"
    POLICY_DOCUMENT_VERSION_MISMATCH = "POLICY_DOCUMENT_VERSION_MISMATCH"
    POLICY_CONTENT_HASH_MISMATCH = "POLICY_CONTENT_HASH_MISMATCH"
    LANE_POLICY_VERSION_MISMATCH = "LANE_POLICY_VERSION_MISMATCH"
    POSITION_EVIDENCE_INCOMPLETE = "POSITION_EVIDENCE_INCOMPLETE"
    NO_SELLABLE_POSITION = "NO_SELLABLE_POSITION"
    CASH_EVIDENCE_INCOMPLETE = "CASH_EVIDENCE_INCOMPLETE"
    CASH_INSUFFICIENT = "CASH_INSUFFICIENT"
    ADD_SIZING_UNDEFINED = "ADD_SIZING_UNDEFINED"
    NOTIONAL_POLICY_UNSATISFIED = "NOTIONAL_POLICY_UNSATISFIED"
    TICK_SNAP_OUTSIDE_ACTION_BAND = "TICK_SNAP_OUTSIDE_ACTION_BAND"
    SUPPORT_PROVENANCE_INCOMPLETE = "SUPPORT_PROVENANCE_INCOMPLETE"
    SUPPORT_CONFLUENCE_INSUFFICIENT = "SUPPORT_CONFLUENCE_INSUFFICIENT"
    NO_SUPPORT_BELOW_MARKET = "NO_SUPPORT_BELOW_MARKET"
    NO_SUPPORT_IN_ACTION_BAND = "NO_SUPPORT_IN_ACTION_BAND"
    SUPPORT_MODERATE_BAND = "SUPPORT_MODERATE_BAND"
    SUPPORT_DEEP_BAND = "SUPPORT_DEEP_BAND"
    RESISTANCE_PROVENANCE_INCOMPLETE = "RESISTANCE_PROVENANCE_INCOMPLETE"
    RESISTANCE_CONFLUENCE_INSUFFICIENT = (
        "RESISTANCE_CONFLUENCE_INSUFFICIENT"
    )
    NO_RESISTANCE_ABOVE_MARKET = "NO_RESISTANCE_ABOVE_MARKET"
    NO_RESISTANCE_ABOVE_GUARD = "NO_RESISTANCE_ABOVE_GUARD"
    LOSS_GUARD_NOT_CLEARED = "LOSS_GUARD_NOT_CLEARED"
    RESISTANCE_NEAR_PLACE = "RESISTANCE_NEAR_PLACE"
    RESISTANCE_NEAR_WATCH = "RESISTANCE_NEAR_WATCH"
    RESISTANCE_FAR_BAND = "RESISTANCE_FAR_BAND"
    RESISTANCE_TOO_FAR = "RESISTANCE_TOO_FAR"
    SINGLE_SHARE_NEAR_NOT_FULL_EXIT = "SINGLE_SHARE_NEAR_NOT_FULL_EXIT"
    SINGLE_SHARE_FULL_EXIT = "SINGLE_SHARE_FULL_EXIT"
    FULL_EXIT_POLICY_DISABLED = "FULL_EXIT_POLICY_DISABLED"
    OPEN_ACTION_UNRESOLVED = "OPEN_ACTION_UNRESOLVED"
    COMPOSITE_EVIDENCE_INCOMPLETE = "COMPOSITE_EVIDENCE_INCOMPLETE"
    COMPOSITE_GATE_FAILED = "COMPOSITE_GATE_FAILED"
    COMPOSITE_GATE_UNKNOWN = "COMPOSITE_GATE_UNKNOWN"
    NEAR_RESISTANCE_ACTION_UNKNOWN = "NEAR_RESISTANCE_ACTION_UNKNOWN"


class KRPercentBandPolicy(_StrictFrozenModel):
    lower_pct: Decimal
    upper_pct: Decimal
    lower_inclusive: bool
    upper_inclusive: bool

    @model_validator(mode="after")
    def validate_order(self) -> KRPercentBandPolicy:
        if self.lower_pct >= self.upper_pct:
            raise ValueError("band lower_pct must be less than upper_pct")
        return self

    def contains(self, value: Decimal) -> bool:
        lower_ok = (
            value >= self.lower_pct
            if self.lower_inclusive
            else value > self.lower_pct
        )
        upper_ok = (
            value <= self.upper_pct
            if self.upper_inclusive
            else value < self.upper_pct
        )
        return lower_ok and upper_ok


class KRBandsPolicy(_StrictFrozenModel):
    moderate_support: KRPercentBandPolicy
    deep_support: KRPercentBandPolicy
    far_resistance: KRPercentBandPolicy
    near_resistance_upper_inclusive_pct: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_boundaries(self) -> KRBandsPolicy:
        moderate = self.moderate_support
        deep = self.deep_support
        far = self.far_resistance
        if not (
            deep.lower_pct < deep.upper_pct == moderate.lower_pct
            < moderate.upper_pct
            < 0
        ):
            raise ValueError("support bands must be contiguous, ordered, and negative")
        if (
            not deep.lower_inclusive
            or not deep.upper_inclusive
            or moderate.lower_inclusive
            or not moderate.upper_inclusive
        ):
            raise ValueError("support band inclusivity does not match the contract")
        if not (
            Decimal("0")
            < self.near_resistance_upper_inclusive_pct
            == far.lower_pct
            < far.upper_pct
        ):
            raise ValueError("far resistance must start above the near boundary")
        if far.lower_inclusive or not far.upper_inclusive:
            raise ValueError("far resistance must be lower-exclusive, upper-inclusive")
        return self


class KRSourceFamilyPolicy(_StrictFrozenModel):
    minimum_independent_family_count: Literal[2]
    fibonacci_levels_share_one_family: Literal[True]
    poc_vah_val_share_one_volume_profile_family: Literal[True]


class KRFreshnessPolicy(_StrictFrozenModel):
    baseline: Literal["expected_completed_krx_bar"]
    discovery_quote_ttl_seconds: Literal[300]
    final_revalidation_quote_ttl_seconds: Literal[60]
    mixed_vintage_action: Literal["fail_closed"]
    policy_hash_mismatch_action: Literal["fail_closed"]


class KRBuySizingPolicy(_StrictFrozenModel):
    new_entry_min_notional_krw: Literal[200000]
    new_entry_max_notional_krw: Literal[400000]
    quantity_rule: Literal["minimum_integer_at_or_above_min"]
    existing_position_action: Literal["defer_add_sizing_undefined"]


class KRSellPolicy(_StrictFrozenModel):
    normal_sell_floor_multiple: Decimal
    multi_share_far_quantity: Literal[1]
    single_share_far_full_exit_enabled: bool
    above_far_action: Literal["watch_only"]

    @field_validator("normal_sell_floor_multiple")
    @classmethod
    def preserve_normal_loss_guard(cls, value: Decimal) -> Decimal:
        if value != Decimal("1.01"):
            raise ValueError("normal sell floor must remain avg_cost * 1.01")
        return value


class KRActivationPolicy(_StrictFrozenModel):
    evaluation_enabled: Literal[True]
    calibration_status: Literal["provisional_shadow"]
    proposal_generation_enabled: Literal[False]
    schedule_enabled: Literal[False]
    mutation_enabled: Literal[False]
    auto_approve_allowed: Literal[False]
    auto_approve_reconsideration: Literal[
        "operator_pr_only_after_phase_6_manual_reps"
    ]


class KRAccountScopePolicy(_StrictFrozenModel):
    allowed_account_modes: tuple[KRRoutableAccountMode, ...]
    order_routable_required: Literal[True]

    @field_validator("allowed_account_modes")
    @classmethod
    def only_kis_and_toss(
        cls, value: tuple[KRRoutableAccountMode, ...]
    ) -> tuple[KRRoutableAccountMode, ...]:
        expected = (KRRoutableAccountMode.KIS_LIVE, KRRoutableAccountMode.TOSS_LIVE)
        if value != expected:
            raise ValueError("KR resting lane account order must be kis_live,toss_live")
        return value


class KROrderShapePolicy(_StrictFrozenModel):
    order_type: Literal["limit"]
    time_in_force: Literal["DAY"]
    rung_count: Literal[1]
    approval_mode: Literal[KRApprovalMode.TELEGRAM_MANUAL]
    next_day_reuse: Literal["never_fresh_decision_required"]


class KRDirectionPolicy(_StrictFrozenModel):
    scope: Literal["symbol_across_kis_and_toss"]
    approval_requery_required: Literal[True]
    symbol_reservation_required_at_approval: Literal[True]
    symbol_lock_required_at_approval: Literal[True]
    enforcement_phase: Literal[3]


class KRRestingLanePolicy(_StrictFrozenModel):
    policy_version: str = Field(min_length=1)
    calibration_version: str = Field(min_length=1)
    activation: KRActivationPolicy
    account_scope: KRAccountScopePolicy
    order_shape: KROrderShapePolicy
    direction: KRDirectionPolicy
    bands: KRBandsPolicy
    source_families: KRSourceFamilyPolicy
    freshness: KRFreshnessPolicy
    buy_sizing: KRBuySizingPolicy
    sell: KRSellPolicy


class KRPolicyStampEvidence(_StrictFrozenModel):
    document_version: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    lane_policy_version: str = Field(min_length=1)


class KRAccountEvidence(_StrictFrozenModel):
    account_mode: KRAccountMode
    broker_account_id: str = Field(min_length=1)
    order_routable: bool
    session_state: KRSessionState


class KRQuoteEvidence(_StrictFrozenModel):
    price: Decimal = Field(gt=0)
    source: KRQuoteSource
    captured_at: datetime
    market_data_state: KRMarketDataState

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value


class KRLevelEvidence(_StrictFrozenModel):
    level_id: str = Field(min_length=1)
    kind: KRLevelKind
    price: Decimal = Field(gt=0)
    sources: tuple[KRLevelSource, ...]
    provenance_status: KREvidenceStatus
    snapshot_id: str = Field(min_length=1)
    computed_at: datetime
    ohlcv_through: date

    @field_validator("computed_at")
    @classmethod
    def computed_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("computed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_sources(self) -> KRLevelEvidence:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("level sources must be unique")
        if self.provenance_status is KREvidenceStatus.COMPLETE and not self.sources:
            raise ValueError("complete provenance requires at least one typed source")
        return self


class KRLevelSetEvidence(_StrictFrozenModel):
    status: KREvidenceStatus
    expected_completed_bar_date: date
    snapshot_id: str = Field(min_length=1)
    computed_at: datetime
    levels: tuple[KRLevelEvidence, ...]

    @field_validator("computed_at")
    @classmethod
    def computed_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("computed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_level_ids(self) -> KRLevelSetEvidence:
        ids = [level.level_id for level in self.levels]
        if len(set(ids)) != len(ids):
            raise ValueError("level_id values must be unique")
        return self


class KRBrokerScanEvidence(_StrictFrozenModel):
    account_mode: KRRoutableAccountMode
    queried_at: datetime
    open_complete: bool
    closed_complete: bool
    pagination_complete: bool

    @field_validator("queried_at")
    @classmethod
    def queried_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("queried_at must be timezone-aware")
        return value


class KROpenOrderEvidence(_StrictFrozenModel):
    broker_order_id: str = Field(min_length=1)
    account_mode: KRRoutableAccountMode
    side: KRRestingSide


class KRReconciliationEvidence(_StrictFrozenModel):
    broker_scans: tuple[KRBrokerScanEvidence, ...]
    open_orders: tuple[KROpenOrderEvidence, ...]
    ledger_matches_broker: bool
    ghost_resting_present: bool
    day_expiry_unreconciled: bool
    approval_requery_at: datetime | None = None
    symbol_reservation_state: KRConcurrencyState = KRConcurrencyState.NOT_EVALUATED
    symbol_lock_state: KRConcurrencyState = KRConcurrencyState.NOT_EVALUATED

    @field_validator("approval_requery_at")
    @classmethod
    def approval_requery_at_must_be_aware(
        cls, value: datetime | None
    ) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("approval_requery_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_unique_keys(self) -> KRReconciliationEvidence:
        scan_modes = [scan.account_mode for scan in self.broker_scans]
        if len(set(scan_modes)) != len(scan_modes):
            raise ValueError("broker scans must be unique by account_mode")
        order_ids = [order.broker_order_id for order in self.open_orders]
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("open broker_order_id values must be unique")
        return self


class KRCashEvidence(_StrictFrozenModel):
    status: KREvidenceStatus
    captured_at: datetime
    orderable_cash_krw: Decimal | None = Field(default=None, ge=0)
    reserved_cash_krw: Decimal | None = Field(default=None, ge=0)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def complete_cash_has_values(self) -> KRCashEvidence:
        if self.status is KREvidenceStatus.COMPLETE and (
            self.orderable_cash_krw is None or self.reserved_cash_krw is None
        ):
            raise ValueError("complete cash evidence requires orderable and reserved")
        return self


class KRPositionEvidence(_StrictFrozenModel):
    status: KREvidenceStatus
    captured_at: datetime
    symbol_total_qty: int | None = Field(default=None, ge=0)
    selected_account_qty: int | None = Field(default=None, ge=0)
    sellable_qty: int | None = Field(default=None, ge=0)
    avg_cost: Decimal | None = Field(default=None, gt=0)

    @field_validator("captured_at")
    @classmethod
    def captured_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_complete_position(self) -> KRPositionEvidence:
        if self.status is KREvidenceStatus.INCOMPLETE:
            return self
        required = (
            self.symbol_total_qty,
            self.selected_account_qty,
            self.sellable_qty,
        )
        if any(value is None for value in required):
            raise ValueError("complete position evidence requires all quantities")
        assert self.symbol_total_qty is not None
        assert self.selected_account_qty is not None
        assert self.sellable_qty is not None
        if self.sellable_qty > self.selected_account_qty:
            raise ValueError("sellable_qty cannot exceed selected_account_qty")
        if self.selected_account_qty > self.symbol_total_qty:
            raise ValueError("selected_account_qty cannot exceed symbol_total_qty")
        if self.selected_account_qty > 0 and self.avg_cost is None:
            raise ValueError("held selected-account position requires avg_cost")
        if self.selected_account_qty == 0 and self.avg_cost is not None:
            raise ValueError("empty selected-account position cannot have avg_cost")
        return self


class KRCompositeEvidence(_StrictFrozenModel):
    status: KREvidenceStatus
    gate_state: KRGateState
    open_action_state: KROpenActionState
    near_resistance_action: KRNearResistanceAction


class KRMarketMicrostructureEvidence(_StrictFrozenModel):
    tick_size: Decimal = Field(gt=0)


class KRRestingLaneEvidence(_StrictFrozenModel):
    schema_version: Literal[EVIDENCE_SCHEMA_VERSION] = EVIDENCE_SCHEMA_VERSION
    evaluated_at: datetime
    krx_trading_date: date
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    side: KRRestingSide
    stage: KREvaluationStage
    policy_stamp: KRPolicyStampEvidence
    account: KRAccountEvidence
    quote: KRQuoteEvidence | None
    level_set: KRLevelSetEvidence
    reconciliation: KRReconciliationEvidence
    position: KRPositionEvidence
    cash: KRCashEvidence | None = None
    composite: KRCompositeEvidence | None = None
    microstructure: KRMarketMicrostructureEvidence

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value


class KRSelectedLevel(_StrictFrozenModel):
    level_id: str
    price: Decimal
    distance_pct: Decimal
    source_families: tuple[KRSourceFamily, ...]
    family_count: int = Field(ge=0)


class KRShadowOrderIntent(_StrictFrozenModel):
    side: KRRestingSide
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["DAY"] = "DAY"
    approval_mode: Literal[KRApprovalMode.TELEGRAM_MANUAL] = (
        KRApprovalMode.TELEGRAM_MANUAL
    )
    rung_count: Literal[1] = 1
    limit_price: Decimal = Field(gt=0)
    quantity: int = Field(gt=0)
    notional_krw: Decimal = Field(gt=0)
    normal_sell_floor: Decimal | None = Field(default=None, gt=0)


_PLACE_VERDICTS = frozenset(
    {
        KRRestingVerdict.PLACE_MODERATE_SUPPORT,
        KRRestingVerdict.PLACE_DEEP_SUPPORT,
        KRRestingVerdict.PLACE_NEAR_RESISTANCE_TRIM,
        KRRestingVerdict.PLACE_FAR_RESISTANCE,
        KRRestingVerdict.FULL_EXIT_SINGLE_SHARE_AT_FAR_RESISTANCE,
    }
)


class KRRestingLaneDecision(_StrictFrozenModel):
    schema_version: Literal[DECISION_SCHEMA_VERSION] = DECISION_SCHEMA_VERSION
    decision_id: str = Field(min_length=1)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_document_version: str
    policy_content_hash: str
    lane_policy_version: str
    calibration_version: str
    calibration_status: Literal["provisional_shadow"]
    verdict: KRRestingVerdict
    primary_reason: KRRestingReason
    reason_codes: tuple[KRRestingReason, ...]
    direction_state: KRDirectionState
    selected_level: KRSelectedLevel | None
    shadow_order: KRShadowOrderIntent | None
    shadow_place_candidate: bool
    proposal_allowed: Literal[False] = False
    manual_approval_required: Literal[True] = True
    auto_approve_allowed: Literal[False] = False
    rung_count: Literal[0, 1]
    evidence: KRRestingLaneEvidence

    @model_validator(mode="after")
    def validate_decision_consistency(self) -> KRRestingLaneDecision:
        if not self.reason_codes or self.primary_reason is not self.reason_codes[0]:
            raise ValueError("primary_reason must be the first reason code")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        is_place = self.verdict in _PLACE_VERDICTS
        if is_place != self.shadow_place_candidate:
            raise ValueError("shadow_place_candidate must match PLACE verdicts")
        if is_place:
            if self.shadow_order is None or self.rung_count != 1:
                raise ValueError("PLACE verdict requires exactly one shadow order")
        elif self.shadow_order is not None or self.rung_count != 0:
            raise ValueError("non-PLACE verdict cannot carry a shadow order")
        return self


__all__ = [
    "DECISION_SCHEMA_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "KRAccountEvidence",
    "KRAccountMode",
    "KRActivationPolicy",
    "KRApprovalMode",
    "KRBandsPolicy",
    "KRBrokerScanEvidence",
    "KRBuySizingPolicy",
    "KRCashEvidence",
    "KRCompositeEvidence",
    "KRConcurrencyState",
    "KRDirectionPolicy",
    "KRDirectionState",
    "KREvaluationStage",
    "KREvidenceStatus",
    "KRFreshnessPolicy",
    "KRGateState",
    "KRLevelEvidence",
    "KRLevelKind",
    "KRLevelSetEvidence",
    "KRLevelSource",
    "KRMarketDataState",
    "KRMarketMicrostructureEvidence",
    "KRNearResistanceAction",
    "KROpenActionState",
    "KROpenOrderEvidence",
    "KROrderShapePolicy",
    "KRPercentBandPolicy",
    "KRPolicyStampEvidence",
    "KRPositionEvidence",
    "KRQuoteEvidence",
    "KRQuoteSource",
    "KRReconciliationEvidence",
    "KRRestingLaneDecision",
    "KRRestingLaneEvidence",
    "KRRestingLanePolicy",
    "KRRestingReason",
    "KRRestingSide",
    "KRRestingVerdict",
    "KRRoutableAccountMode",
    "KRSellPolicy",
    "KRSelectedLevel",
    "KRSessionState",
    "KRShadowOrderIntent",
    "KRSourceFamily",
    "KRSourceFamilyPolicy",
]
