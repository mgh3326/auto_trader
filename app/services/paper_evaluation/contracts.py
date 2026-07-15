"""Frozen side-effect-free contracts for ROB-850 paper evaluation.

All contracts are ``frozen=True, extra="forbid"`` Pydantic models.
They define the versioned ``EvaluationConfig``, per-view ``ViewMetrics``,
conjunctive ``ScorecardVerdict``, evaluation ``EpochIdentity``, and
7/60-day ``GateVerdict``.

Policy is finalised (see ROB-850 Linear comment 2026-07-12):
* Binance broker view: native USDT
* Alpaca broker view: native USD
* canonical shadow view: USDT
* ``currency_conversion_policy = "none"`` — no USDT/USD conversion, no peg.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from app.services.research_canonical_hash import canonical_sha256

# ---------------------------------------------------------------------------
# Shared type aliases
# ---------------------------------------------------------------------------

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identifier128 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
ReasonCode64 = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ViewName(StrEnum):
    """The exactly-three evaluation views."""

    BINANCE_BROKER = "binance_broker"
    ALPACA_BROKER = "alpaca_broker"
    CANONICAL_SHADOW = "canonical_shadow"


class ViewCurrency(StrEnum):
    USDT = "USDT"
    USD = "USD"


class ViewSource(StrEnum):
    """Read boundary for each view's native data."""

    BINANCE_DEMO_LEDGER = "binance_demo_order_ledger"
    ALPACA_PAPER_LEDGER = "alpaca_paper_order_ledger"
    CANONICAL_MARKET_SNAPSHOT = "canonical_market_snapshot"


class CalendarDaySemantics(StrEnum):
    FULL_CALENDAR_DAY = "full_calendar_day"


class GateType(StrEnum):
    SHADOW_SOAK = "shadow_soak"
    PAPER_PROMOTION = "paper_promotion"


class VerdictStatus(StrEnum):
    """Deterministic verdict outcome consumed by ROB-848."""

    PROMOTION_ELIGIBLE = "promotion_eligible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    GATE_BLOCKED = "gate_blocked"
    BENCHMARK_NOT_BEATEN = "benchmark_not_beaten"
    MDD_EXCEEDED = "mdd_exceeded"


class EpochResetReason(StrEnum):
    ACCOUNT_RESET = "account_reset"
    API_KEY_RECREATION = "api_key_recreation"
    INITIAL_EQUITY_CHANGE = "initial_equity_change"


class MissingDataPolicy(StrEnum):
    """Missing observations are never dropped or zero-filled."""

    FAIL_CLOSE = "fail_close"


class PartialFillPolicy(StrEnum):
    REJECT_PARTIAL = "reject_partial"
    ACCEPT_PARTIAL_WITH_EVIDENCE = "accept_partial_with_evidence"


class CurrencyConversionPolicy(StrEnum):
    NONE = "none"


# ---------------------------------------------------------------------------
# Frozen base
# ---------------------------------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvaluationConfigError(Exception):
    """Stable fail-closed evaluation config error."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


# ---------------------------------------------------------------------------
# Config sub-contracts
# ---------------------------------------------------------------------------


class ViewMapping(_Frozen):
    """Maps one evaluation view to its native currency, source, and symbols."""

    view_name: ViewName
    currency: ViewCurrency
    source: ViewSource
    symbols: tuple[str, ...]
    benchmark_symbols: tuple[str, ...]

    @model_validator(mode="after")
    def validate_view_currency_source_consistency(self) -> ViewMapping:
        expected: dict[ViewName, tuple[ViewCurrency, ViewSource]] = {
            ViewName.BINANCE_BROKER: (
                ViewCurrency.USDT,
                ViewSource.BINANCE_DEMO_LEDGER,
            ),
            ViewName.ALPACA_BROKER: (
                ViewCurrency.USD,
                ViewSource.ALPACA_PAPER_LEDGER,
            ),
            ViewName.CANONICAL_SHADOW: (
                ViewCurrency.USDT,
                ViewSource.CANONICAL_MARKET_SNAPSHOT,
            ),
        }
        if (self.currency, self.source) != expected[self.view_name]:
            raise EvaluationConfigError(
                "invalid_view_mapping",
                f"view {self.view_name} must use {expected[self.view_name]}",
            )
        if len(self.symbols) == 0:
            raise EvaluationConfigError("invalid_view_mapping", "symbols required")
        if len(self.benchmark_symbols) != 2:
            raise EvaluationConfigError(
                "invalid_view_mapping", "exactly two benchmark symbols required"
            )
        return self


class FillCostPolicy(_Frozen):
    """Frozen fee/spread/slippage/partial-fill assumptions for shadow fills."""

    fee_rate_bps: NonNegativeDecimal
    spread_bps: NonNegativeDecimal
    slippage_bps: NonNegativeDecimal
    partial_fill_policy: PartialFillPolicy
    partial_fill_ratio: Decimal = Field(
        default=Decimal("0"), ge=0, le=1, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_partial_fill_ratio(self) -> FillCostPolicy:
        if (
            self.partial_fill_policy is PartialFillPolicy.ACCEPT_PARTIAL_WITH_EVIDENCE
            and self.partial_fill_ratio <= 0
        ):
            raise EvaluationConfigError(
                "invalid_fill_cost_policy",
                "accept_partial_with_evidence requires partial_fill_ratio > 0",
            )
        return self


class MarkFillTiming(_Frozen):
    """When marks are taken and when fills are assumed for the shadow view."""

    mark_timing: Literal["canonical_close"]
    fill_timing: Literal["next_bar_open", "canonical_close"]


class MinimumEvidence(_Frozen):
    """Minimum observation/fill/day counts for a view to be eligible."""

    min_observations: int = Field(gt=0)
    min_fills: int = Field(ge=0)
    min_calendar_days: int = Field(gt=0)


class PromotionThresholds(_Frozen):
    """Frozen thresholds for the conjunctive promotion gate."""

    min_benchmark_delta_pct: PositiveDecimal
    max_drawdown_target_pct: PositiveDecimal


class BenchmarkWeights(_Frozen):
    """BTC/ETH equal-weight benchmark definition."""

    btc_weight: Decimal = Field(default=Decimal("0.5"), gt=0, lt=1, allow_inf_nan=False)
    eth_weight: Decimal = Field(default=Decimal("0.5"), gt=0, lt=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> BenchmarkWeights:
        if self.btc_weight + self.eth_weight != 1:
            raise EvaluationConfigError(
                "invalid_benchmark_weights", "btc + eth weights must equal 1"
            )
        return self


class AnnualizationRules(_Frozen):
    """Annualization and risk-free assumptions hashed into config."""

    periods_per_year: int = Field(gt=0)
    risk_free_rate_pct: NonNegativeDecimal


# ---------------------------------------------------------------------------
# EvaluationConfig
# ---------------------------------------------------------------------------

_V1_VIEW_NAMES = frozenset(
    {
        ViewName.BINANCE_BROKER,
        ViewName.ALPACA_BROKER,
        ViewName.CANONICAL_SHADOW,
    }
)
_V1_VIEW_ORDER = (
    ViewName.BINANCE_BROKER,
    ViewName.ALPACA_BROKER,
    ViewName.CANONICAL_SHADOW,
)


def _freeze_view_mapping(
    mapping: Mapping[ViewName, object],
) -> Mapping[ViewName, object]:
    """Return a deterministic, read-only mapping in canonical view order."""
    return MappingProxyType({name: mapping[name] for name in _V1_VIEW_ORDER})


class EvaluationConfig(_Frozen):
    """Versioned, frozen, SHA-256-hashed evaluation configuration.

    The hash covers every field listed in ROB-850 AC 5/8 and the confirmed
    policy comment.  Mapping input order does not affect the hash because
    ``canonical_sha256`` sorts dict keys.  Changing any meaningful field
    changes the hash.
    """

    schema_id: Literal["paper_evaluation_config.v1"] = "paper_evaluation_config.v1"
    formula_version: Literal["v1"] = "v1"

    # view → currency/source/symbol mapping (dict → order-independent hash)
    views: Mapping[ViewName, ViewMapping]

    # shadow currency
    shadow_currency: Literal[ViewCurrency.USDT] = ViewCurrency.USDT

    # initial-equity per view (native currency)
    initial_equity: Mapping[ViewName, PositiveDecimal]

    # canonical snapshot source/schema
    canonical_snapshot_source: Literal["binance_public_spot"]
    canonical_snapshot_schema: Literal["canonical_market_snapshot.v1"]

    # mark/fill timing
    mark_fill_timing: MarkFillTiming

    # fee/spread/slippage/partial-fill policy
    fill_cost_policy: FillCostPolicy

    # missing-data policy
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.FAIL_CLOSE

    # annualization/risk-free rules
    annualization: AnnualizationRules

    # benchmark definitions/weights
    benchmark_weights: BenchmarkWeights

    # 7/60 calendar-day semantics
    shadow_soak_days: Literal[7] = 7
    paper_promotion_days: Literal[60] = 60
    calendar_day_semantics: CalendarDaySemantics = (
        CalendarDaySemantics.FULL_CALENDAR_DAY
    )

    # minimum evidence
    minimum_evidence: MinimumEvidence

    # promotion thresholds
    promotion_thresholds: PromotionThresholds

    # MDD target (also in promotion_thresholds but explicitly hashed)
    mdd_target_pct: PositiveDecimal

    # currency conversion policy — explicit "none"
    currency_conversion_policy: CurrencyConversionPolicy = CurrencyConversionPolicy.NONE

    @model_validator(mode="after")
    def validate_views_and_equity(self) -> EvaluationConfig:
        if set(self.views) != _V1_VIEW_NAMES:
            raise EvaluationConfigError(
                "invalid_view_set",
                f"exactly {_V1_VIEW_NAMES} views required",
            )
        if set(self.initial_equity) != _V1_VIEW_NAMES:
            raise EvaluationConfigError(
                "invalid_equity_set",
                "initial_equity must cover exactly the three views",
            )
        for name, mapping in self.views.items():
            if mapping.view_name != name:
                raise EvaluationConfigError(
                    "invalid_view_mapping",
                    f"view key {name} mismatches mapping.view_name",
                )
        if self.mdd_target_pct != self.promotion_thresholds.max_drawdown_target_pct:
            raise EvaluationConfigError(
                "mdd_target_mismatch",
                "mdd_target_pct must equal promotion_thresholds.max_drawdown_target_pct",
            )
        object.__setattr__(self, "views", _freeze_view_mapping(self.views))
        object.__setattr__(
            self, "initial_equity", _freeze_view_mapping(self.initial_equity)
        )
        return self

    @field_serializer("views", "initial_equity")
    def serialize_view_mappings(
        self, value: Mapping[ViewName, object]
    ) -> dict[ViewName, object]:
        return dict(value.items())

    # ------------------------------------------------------------------
    # Canonical hash
    # ------------------------------------------------------------------

    def to_hash_payload(self) -> dict[str, object]:
        """Return a canonical-serialisable dict for ``canonical_sha256``.

        Dicts are used for mappings so key order is collapsed by the hash
        function.  Tuples preserve order where it is semantically meaningful.
        """
        return {
            "schema_id": self.schema_id,
            "formula_version": self.formula_version,
            "views": {
                name.value: mapping.model_dump(mode="python")
                for name, mapping in self.views.items()
            },
            "shadow_currency": self.shadow_currency.value,
            "initial_equity": {
                name.value: str(equity) for name, equity in self.initial_equity.items()
            },
            "canonical_snapshot_source": self.canonical_snapshot_source,
            "canonical_snapshot_schema": self.canonical_snapshot_schema,
            "mark_fill_timing": self.mark_fill_timing.model_dump(mode="python"),
            "fill_cost_policy": self.fill_cost_policy.model_dump(mode="python"),
            "missing_data_policy": self.missing_data_policy.value,
            "annualization": self.annualization.model_dump(mode="python"),
            "benchmark_weights": self.benchmark_weights.model_dump(mode="python"),
            "shadow_soak_days": self.shadow_soak_days,
            "paper_promotion_days": self.paper_promotion_days,
            "calendar_day_semantics": self.calendar_day_semantics.value,
            "minimum_evidence": self.minimum_evidence.model_dump(mode="python"),
            "promotion_thresholds": self.promotion_thresholds.model_dump(mode="python"),
            "mdd_target_pct": str(self.mdd_target_pct),
            "currency_conversion_policy": self.currency_conversion_policy.value,
        }

    def config_hash(self) -> str:
        return canonical_sha256(self.to_hash_payload())


# ---------------------------------------------------------------------------
# Epoch
# ---------------------------------------------------------------------------


class EpochIdentity(_Frozen):
    """Immutable evaluation epoch identity.

    A new epoch is created on broker account reset, API-key recreation,
    or frozen initial-equity change.  Prior epochs remain separately
    queryable and are never spliced.
    """

    epoch_id: Identifier128
    assignment_id: Identifier128
    validation_id: Identifier128
    cohort_id: Identifier128
    config_hash: Sha256
    experiment_hash: Sha256
    cohort_hash: Sha256
    initial_equity: Mapping[ViewName, PositiveDecimal]
    started_at: datetime
    reset_reason: EpochResetReason | None = None
    prior_epoch_id: Identifier128 | None = None

    @field_validator("started_at")
    @classmethod
    def started_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise EvaluationConfigError(
                "invalid_epoch", "started_at must be timezone-aware"
            )
        return value

    @model_validator(mode="after")
    def validate_equity_matches_views(self) -> EpochIdentity:
        if set(self.initial_equity) != _V1_VIEW_NAMES:
            raise EvaluationConfigError(
                "invalid_epoch", "initial_equity must cover exactly three views"
            )
        object.__setattr__(
            self, "initial_equity", _freeze_view_mapping(self.initial_equity)
        )
        if (self.prior_epoch_id is None) != (self.reset_reason is None):
            raise EvaluationConfigError(
                "invalid_epoch", "prior_epoch_id and reset_reason must appear together"
            )
        if self.prior_epoch_id == self.epoch_id:
            raise EvaluationConfigError(
                "invalid_epoch", "epoch cannot reference itself"
            )
        return self

    @field_serializer("initial_equity")
    def serialize_initial_equity(
        self, value: Mapping[ViewName, PositiveDecimal]
    ) -> dict[ViewName, PositiveDecimal]:
        return dict(value.items())


# ---------------------------------------------------------------------------
# ViewMetrics (computed, per view)
# ---------------------------------------------------------------------------


class ViewMetrics(_Frozen):
    """Computed P&L and risk metrics for one evaluation view.

    All nominal amounts are in the view's native currency.  No cross-view
    nominal aggregation is performed.
    """

    view_name: ViewName
    currency: ViewCurrency
    source: ViewSource
    symbol_mapping: tuple[str, ...]

    # equity
    initial_equity: PositiveDecimal
    ending_equity: Decimal = Field(allow_inf_nan=False)

    # nominal P&L
    nominal_net_pnl: Decimal = Field(allow_inf_nan=False)
    fees: NonNegativeDecimal

    # normalised metrics
    net_return_pct: Decimal = Field(allow_inf_nan=False)
    max_drawdown_pct: NonNegativeDecimal
    turnover: NonNegativeDecimal
    exposure: Decimal = Field(ge=0, le=1, allow_inf_nan=False)

    # reference risk metrics
    sharpe_reference: Decimal | None = None
    dsr_reference: Decimal | None = None

    # fill / observation counts
    fill_count: int = Field(ge=0)
    observation_count: int = Field(default=0, ge=0)
    partial_fill_count: int = Field(ge=0)
    missing_observation_count: int = Field(ge=0)

    # benchmarks (native currency)
    cash_benchmark_return_pct: Decimal = Field(allow_inf_nan=False)
    cash_benchmark_delta_pct: Decimal = Field(allow_inf_nan=False)
    btc_eth_benchmark_return_pct: Decimal = Field(allow_inf_nan=False)
    btc_eth_benchmark_delta_pct: Decimal = Field(allow_inf_nan=False)

    # backtest → forward decay
    backtest_forward_decay: Decimal | None = None

    # canonical snapshot lineage consumed
    canonical_snapshot_hashes: tuple[str, ...] = ()

    # experiment/cohort/epoch/config lineage
    experiment_hash: Sha256
    cohort_hash: Sha256
    epoch_id: Identifier128
    config_hash: Sha256

    @model_validator(mode="after")
    def validate_currency_source_consistency(self) -> ViewMetrics:
        expected: dict[ViewName, tuple[ViewCurrency, ViewSource]] = {
            ViewName.BINANCE_BROKER: (
                ViewCurrency.USDT,
                ViewSource.BINANCE_DEMO_LEDGER,
            ),
            ViewName.ALPACA_BROKER: (
                ViewCurrency.USD,
                ViewSource.ALPACA_PAPER_LEDGER,
            ),
            ViewName.CANONICAL_SHADOW: (
                ViewCurrency.USDT,
                ViewSource.CANONICAL_MARKET_SNAPSHOT,
            ),
        }
        if (self.currency, self.source) != expected[self.view_name]:
            raise EvaluationConfigError(
                "invalid_view_metrics",
                f"view {self.view_name} currency/source mismatch",
            )
        return self

    @model_validator(mode="after")
    def validate_ending_equity(self) -> ViewMetrics:
        computed = self.initial_equity + self.nominal_net_pnl
        if self.ending_equity != computed:
            raise EvaluationConfigError(
                "invalid_view_metrics",
                "ending_equity must equal initial_equity + nominal_net_pnl",
            )
        return self


# ---------------------------------------------------------------------------
# Gate verdict
# ---------------------------------------------------------------------------


class GateVerdict(_Frozen):
    """Deterministic 7-day shadow / 60-day paper promotion gate result."""

    gate_type: GateType
    calendar_days_observed: int = Field(ge=0)
    required_days: int = Field(gt=0)
    passed: bool
    reason_code: ReasonCode64
    reason_text: NonBlank

    @model_validator(mode="after")
    def validate_boundary(self) -> GateVerdict:
        if self.passed and self.calendar_days_observed < self.required_days:
            raise EvaluationConfigError(
                "invalid_gate_verdict",
                "passed=True requires calendar_days_observed >= required_days",
            )
        if not self.passed and self.calendar_days_observed >= self.required_days:
            raise EvaluationConfigError(
                "invalid_gate_verdict",
                "passed=False requires calendar_days_observed < required_days",
            )
        return self


# ---------------------------------------------------------------------------
# Scorecard verdict (conjunctive across 3 views)
# ---------------------------------------------------------------------------


class ScorecardVerdict(_Frozen):
    """Conjunctive verdict across all three native-currency views.

    Aggregate extrema are fieldwise:
    * ``min_net_return_pct`` — minimum across views
    * ``max_max_drawdown_pct`` — maximum across views
    * ``min_benchmark_delta_pct`` — minimum across views and both benchmarks

    No cross-currency nominal total is emitted.
    """

    status: VerdictStatus
    epoch_id: Identifier128
    config_hash: Sha256
    experiment_hash: Sha256
    cohort_hash: Sha256

    # per-view metrics (never aggregated nominally)
    view_metrics: Mapping[ViewName, ViewMetrics]

    # aggregate extrema (normalised only)
    min_net_return_pct: Decimal = Field(allow_inf_nan=False)
    max_max_drawdown_pct: NonNegativeDecimal
    min_benchmark_delta_pct: Decimal = Field(allow_inf_nan=False)

    # gate evidence
    shadow_gate: GateVerdict
    paper_gate: GateVerdict

    # evidence IDs for ROB-848 PromotionEligibilityEvidence
    evidence_ids: tuple[NonBlank, ...] = Field(min_length=1)

    reason_code: ReasonCode64
    reason_text: NonBlank

    @model_validator(mode="after")
    def validate_views_present(self) -> ScorecardVerdict:
        if set(self.view_metrics) != _V1_VIEW_NAMES:
            raise EvaluationConfigError(
                "invalid_verdict", "exactly three views required"
            )
        for name, metrics in self.view_metrics.items():
            if metrics.view_name != name:
                raise EvaluationConfigError(
                    "invalid_verdict", f"view key {name} mismatches nested metric"
                )
            for field in (
                "epoch_id",
                "config_hash",
                "experiment_hash",
                "cohort_hash",
            ):
                if getattr(metrics, field) != getattr(self, field):
                    raise EvaluationConfigError(
                        "invalid_verdict", f"nested metric {name} {field} mismatch"
                    )
        if self.shadow_gate.gate_type is not GateType.SHADOW_SOAK:
            raise EvaluationConfigError("invalid_verdict", "shadow_gate type mismatch")
        if self.paper_gate.gate_type is not GateType.PAPER_PROMOTION:
            raise EvaluationConfigError("invalid_verdict", "paper_gate type mismatch")
        object.__setattr__(
            self, "view_metrics", _freeze_view_mapping(self.view_metrics)
        )
        return self

    @field_serializer("view_metrics")
    def serialize_view_metrics(
        self, value: Mapping[ViewName, ViewMetrics]
    ) -> dict[ViewName, ViewMetrics]:
        return dict(value.items())

    @model_validator(mode="after")
    def validate_aggregate_extrema(self) -> ScorecardVerdict:
        returns = [m.net_return_pct for m in self.view_metrics.values()]
        mdds = [m.max_drawdown_pct for m in self.view_metrics.values()]
        deltas = []
        for m in self.view_metrics.values():
            deltas.append(m.cash_benchmark_delta_pct)
            deltas.append(m.btc_eth_benchmark_delta_pct)
        if self.min_net_return_pct != min(returns):
            raise EvaluationConfigError(
                "invalid_verdict", "min_net_return_pct must be min across views"
            )
        if self.max_max_drawdown_pct != max(mdds):
            raise EvaluationConfigError(
                "invalid_verdict", "max_max_drawdown_pct must be max across views"
            )
        if self.min_benchmark_delta_pct != min(deltas):
            raise EvaluationConfigError(
                "invalid_verdict",
                "min_benchmark_delta_pct must be min across views and benchmarks",
            )
        return self


__all__ = [
    "AnnualizationRules",
    "BenchmarkWeights",
    "CalendarDaySemantics",
    "CurrencyConversionPolicy",
    "EpochIdentity",
    "EpochResetReason",
    "EvaluationConfig",
    "EvaluationConfigError",
    "FillCostPolicy",
    "GateType",
    "GateVerdict",
    "MarkFillTiming",
    "MinimumEvidence",
    "MissingDataPolicy",
    "NonBlank",
    "PartialFillPolicy",
    "PromotionThresholds",
    "ScorecardVerdict",
    "ViewCurrency",
    "ViewMapping",
    "ViewMetrics",
    "ViewName",
    "ViewSource",
    "VerdictStatus",
]
