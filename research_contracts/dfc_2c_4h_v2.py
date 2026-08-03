"""Immutable, offline-only registration contract for ``DFC-2C-4H-v2``.

This module is deliberately separate from the old DFC-4H seal and from every
collector, broker, account, ledger, and runtime service.  It fixes the two
feature calculations and the only allowed tail threshold so a caller cannot
turn a desired incidence rate into a quantile-search parameter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "CANONICAL_HASH",
    "CONTRACT_ID",
    "CONTRACT_SCHEMA_VERSION",
    "FIXED_QUANTILE_DENOMINATOR",
    "FIXED_QUANTILE_NUMERATOR",
    "HISTORICAL_HOLDOUT_END_UTC",
    "HISTORICAL_HOLDOUT_START_UTC",
    "HISTORICAL_HOLDOUT_SCHEDULED_EPOCHS",
    "HISTORICAL_WARMUP_START_UTC",
    "PIT_LOOKBACK",
    "PROSPECTIVE_SHADOW_DAYS",
    "PROSPECTIVE_SHADOW_SCHEDULED_EPOCHS",
    "REQUIRED_EVIDENCE_FIELDS",
    "SIGNAL_SYMBOLS",
    "SYMBOL_PRIORITY",
    "TAIL_LOOKBACK",
    "BasketDecision",
    "BasketEvaluationState",
    "IntegrityState",
    "SymbolEpochInput",
    "SymbolScore",
    "canonical_contract_hash",
    "contract_as_machine_data",
    "evaluate_basket",
    "ofi_from_base_volumes",
    "pit_rank",
    "premium_index_close_from_complete_4h",
    "score_symbol",
    "tail_threshold_q75",
    "validate_evidence_manifest",
]


CONTRACT_ID: Final = "DFC-2C-4H-v2"
CONTRACT_SCHEMA_VERSION: Final = "dfc-2c-4h-v2.contract.v1"
SIGNAL_SYMBOLS: Final = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
SYMBOL_PRIORITY: Final = MappingProxyType(
    {symbol: index for index, symbol in enumerate(SIGNAL_SYMBOLS)}
)

EPOCH_HOURS: Final = 4
PIT_LOOKBACK: Final = 252
TAIL_LOOKBACK: Final = 252
FIXED_QUANTILE_NUMERATOR: Final = 3
FIXED_QUANTILE_DENOMINATOR: Final = 4

# These windows are deliberately disjoint from the 2023-08-04 through
# 2026-08-03 exploratory proxy artifact.  The historical holdout is bounded
# and fixed before the exact-feature implementation is run.
HISTORICAL_WARMUP_START_UTC: Final = "2021-02-02T00:00:00Z"
HISTORICAL_HOLDOUT_START_UTC: Final = "2021-05-02T00:00:00Z"
HISTORICAL_HOLDOUT_END_UTC: Final = "2021-10-29T00:00:00Z"
HISTORICAL_HOLDOUT_SCHEDULED_EPOCHS: Final = 1_080
PROSPECTIVE_SHADOW_DAYS: Final = 28
PROSPECTIVE_SHADOW_SCHEDULED_EPOCHS: Final = 168

REQUIRED_EVIDENCE_FIELDS: Final = frozenset(
    {
        "source_id",
        "endpoint_host",
        "endpoint_path",
        "endpoint_version",
        "symbol",
        "interval",
        "epoch_start_utc",
        "epoch_end_utc",
        "complete",
        "gap_status",
        "raw_payload_sha256",
        "schema_version",
    }
)


class IntegrityState(StrEnum):
    """The only states admitted before a symbol can enter the score."""

    COMPLETE = "complete"
    GAP = "gap"
    MISSING = "missing"
    CONFLICT = "conflict"
    INVALID = "invalid"


class BasketEvaluationState(StrEnum):
    """A non-evaluable epoch is not silently counted as no candidate."""

    CANDIDATE = "candidate"
    NO_CANDIDATE = "no_candidate"
    NOT_EVALUABLE_INTEGRITY = "not_evaluable_integrity"
    REFERENCE_NOT_READY = "reference_not_ready"


@dataclass(frozen=True, slots=True)
class SymbolEpochInput:
    """All current and strictly-prior values needed for one signal symbol."""

    symbol: str
    integrity: IntegrityState
    current_ofi: float | None
    current_premium_close: float | None
    prior_ofi: tuple[float, ...]
    prior_premium_close: tuple[float, ...]
    prior_abs_composite: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.symbol not in SYMBOL_PRIORITY:
            raise ValueError(f"unknown DFC-2C symbol: {self.symbol!r}")
        if not isinstance(self.integrity, IntegrityState):
            raise ValueError("integrity must be an IntegrityState")
        object.__setattr__(self, "prior_ofi", tuple(self.prior_ofi))
        object.__setattr__(self, "prior_premium_close", tuple(self.prior_premium_close))
        object.__setattr__(self, "prior_abs_composite", tuple(self.prior_abs_composite))


@dataclass(frozen=True, slots=True)
class SymbolScore:
    """One deterministic score; only a tail candidate can win its epoch."""

    symbol: str
    ofi_rank: float
    premium_rank: float
    composite: float
    threshold: float
    is_candidate: bool


@dataclass(frozen=True, slots=True)
class BasketDecision:
    """One result per UTC 4h epoch, with at most one winning symbol."""

    state: BasketEvaluationState
    candidate_any: bool | None
    winner: str | None
    scores: tuple[SymbolScore, ...]


def _finite(name: str, value: float | None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite numeric value")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return numeric


def _history(
    name: str, values: Sequence[float], *, nonnegative: bool = False
) -> tuple[float, ...]:
    if len(values) != PIT_LOOKBACK:
        raise ValueError(f"{name} must contain exactly {PIT_LOOKBACK} prior values")
    materialized = tuple(
        _finite(f"{name}[{index}]", value) for index, value in enumerate(values)
    )
    if nonnegative and any(value < 0 for value in materialized):
        raise ValueError(f"{name} must contain absolute, nonnegative values")
    return materialized


def ofi_from_base_volumes(
    total_base_volume: float, taker_buy_base_volume: float
) -> float:
    """Return ``log(buy_base / sell_base)`` from one complete 4h Kline.

    Quote-volume inputs, epsilons, and zero-volume substitutions are absent by
    design.  A nonpositive buy or sell side makes the epoch invalid instead of
    manufactured by imputation.
    """

    total = _finite("total_base_volume", total_base_volume)
    buy = _finite("taker_buy_base_volume", taker_buy_base_volume)
    sell = total - buy
    if total <= 0 or buy <= 0 or sell <= 0:
        raise ValueError("complete Kline requires strictly positive base buy and sell")
    return math.log(buy / sell)


def premium_index_close_from_complete_4h(
    premium_index_close: float, *, is_complete: bool
) -> float:
    """Accept exactly one completed 4h premium-index candle close.

    The function deliberately accepts a scalar rather than a five-minute
    series, so it cannot average or otherwise aggregate a lower-frequency
    proxy.
    """

    if is_complete is not True:
        raise ValueError("premium-index candle must be complete")
    return _finite("premium_index_close", premium_index_close)


def pit_rank(current: float, prior_values: Sequence[float]) -> float:
    """Current-excluded empirical PIT rank using the fixed <= tie rule."""

    current_value = _finite("current", current)
    history = _history("prior_values", prior_values)
    less_or_equal = sum(value <= current_value for value in history)
    return 2.0 * (less_or_equal / PIT_LOOKBACK) - 1.0


def tail_threshold_q75(prior_abs_composites: Sequence[float]) -> float:
    """The only permitted tail threshold: linear Q0.75 of prior 252 |C| values.

    There is intentionally no quantile argument.  With 252 observations,
    ``(252 - 1) * 0.75 == 188.25``, so the rule is exactly
    ``a[188] + 0.25 * (a[189] - a[188])`` after sorting.
    """

    history = _history("prior_abs_composites", prior_abs_composites, nonnegative=True)
    ordered = sorted(history)
    return ordered[188] + 0.25 * (ordered[189] - ordered[188])


def _has_reference_history(inputs: SymbolEpochInput) -> bool:
    return (
        len(inputs.prior_ofi) == PIT_LOOKBACK
        and len(inputs.prior_premium_close) == PIT_LOOKBACK
        and len(inputs.prior_abs_composite) == TAIL_LOOKBACK
    )


def score_symbol(inputs: SymbolEpochInput) -> SymbolScore:
    """Score one complete symbol epoch using only its prior 252 observations."""

    if inputs.integrity is not IntegrityState.COMPLETE:
        raise ValueError("only complete symbol epochs can be scored")
    if not _has_reference_history(inputs):
        raise ValueError("reference history is not ready")
    if inputs.current_ofi is None or inputs.current_premium_close is None:
        raise ValueError("complete symbol epoch is missing a current feature")

    ofi_rank = pit_rank(inputs.current_ofi, inputs.prior_ofi)
    premium_value = premium_index_close_from_complete_4h(
        inputs.current_premium_close, is_complete=True
    )
    premium_rank = pit_rank(premium_value, inputs.prior_premium_close)
    composite = (ofi_rank + premium_rank) / 2.0
    threshold = tail_threshold_q75(inputs.prior_abs_composite)
    return SymbolScore(
        symbol=inputs.symbol,
        ofi_rank=ofi_rank,
        premium_rank=premium_rank,
        composite=composite,
        threshold=threshold,
        is_candidate=abs(composite) >= threshold,
    )


def _require_inner_aligned_symbols(inputs: Mapping[str, SymbolEpochInput]) -> None:
    if set(inputs) != set(SIGNAL_SYMBOLS):
        raise ValueError(
            "basket requires exactly the three inner-aligned signal symbols"
        )
    for symbol in SIGNAL_SYMBOLS:
        if inputs[symbol].symbol != symbol:
            raise ValueError("symbol mapping key must match SymbolEpochInput.symbol")


def evaluate_basket(inputs: Mapping[str, SymbolEpochInput]) -> BasketDecision:
    """Evaluate a UTC 4h inner-aligned basket and deterministically select one.

    The winner rule is fixed before any data: choose the candidate with largest
    ``abs(C)``; exact ties use the immutable ``SIGNAL_SYMBOLS`` order.  Missing,
    gapped, conflicting, invalid, or reference-not-ready inputs yield ``None``
    for ``candidate_any`` rather than a fabricated false observation.
    """

    _require_inner_aligned_symbols(inputs)
    ordered_inputs = tuple(inputs[symbol] for symbol in SIGNAL_SYMBOLS)
    if any(item.integrity is not IntegrityState.COMPLETE for item in ordered_inputs):
        return BasketDecision(
            state=BasketEvaluationState.NOT_EVALUABLE_INTEGRITY,
            candidate_any=None,
            winner=None,
            scores=(),
        )
    if any(not _has_reference_history(item) for item in ordered_inputs):
        return BasketDecision(
            state=BasketEvaluationState.REFERENCE_NOT_READY,
            candidate_any=None,
            winner=None,
            scores=(),
        )

    scores = tuple(score_symbol(item) for item in ordered_inputs)
    candidates = tuple(score for score in scores if score.is_candidate)
    if not candidates:
        return BasketDecision(
            state=BasketEvaluationState.NO_CANDIDATE,
            candidate_any=False,
            winner=None,
            scores=scores,
        )
    winner = min(
        candidates,
        key=lambda score: (-abs(score.composite), SYMBOL_PRIORITY[score.symbol]),
    )
    return BasketDecision(
        state=BasketEvaluationState.CANDIDATE,
        candidate_any=True,
        winner=winner.symbol,
        scores=scores,
    )


def validate_evidence_manifest(record: Mapping[str, Any]) -> None:
    """Reject an evidence record that omits provenance required by this contract."""

    missing = sorted(
        field for field in REQUIRED_EVIDENCE_FIELDS if record.get(field) in (None, "")
    )
    if missing:
        raise ValueError(
            f"missing required DFC-2C evidence fields: {', '.join(missing)}"
        )
    if record["complete"] is not True:
        raise ValueError("only complete evidence can enter DFC-2C scoring")
    if record["gap_status"] != "none":
        raise ValueError("gapped evidence cannot enter DFC-2C scoring")
    raw_hash = record["raw_payload_sha256"]
    if (
        not isinstance(raw_hash, str)
        or len(raw_hash) != 64
        or any(character not in "0123456789abcdef" for character in raw_hash)
    ):
        raise ValueError("raw_payload_sha256 must be lowercase 64-character hex")


def contract_as_machine_data() -> dict[str, Any]:
    """Return the sealed, JSON-ready registration data without performing I/O."""

    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "registration_mode": "independent_new_registration",
        "predecessor_preservation": {
            "old_identity": "dfc-4h",
            "old_row_mutation": "forbidden",
            "old_seal_mutation": "forbidden",
            "old_supersede_or_hide": "forbidden",
        },
        "universe": {
            "signal_symbols": list(SIGNAL_SYMBOLS),
            "decision_epoch": "UTC 4h boundary",
            "source_interval": "[e-4h,e)",
            "basket_alignment": "all three symbols inner-aligned at one epoch",
        },
        "features": {
            "ofi": {
                "source": "Binance USD-M Kline",
                "endpoint": {
                    "host": "fapi.binance.com",
                    "path": "/fapi/v1/klines",
                    "version": "v1",
                    "method": "GET",
                    "interval": "4h",
                },
                "formula": "log(taker_buy_base_volume / (total_base_volume - taker_buy_base_volume))",
                "complete_only": True,
                "zero_or_nonpositive_side": "invalid_not_imputed",
            },
            "premium": {
                "source": "Binance USD-M premium-index Kline",
                "endpoint": {
                    "host": "fapi.binance.com",
                    "path": "/fapi/v1/premiumIndexKlines",
                    "version": "v1",
                    "method": "GET",
                    "interval": "4h",
                },
                "value": "complete_4h_candle_close",
                "complete_only": True,
            },
            "deprecated": [
                "quote_volume_proxy",
                "five_minute_premium_average",
            ],
        },
        "score": {
            "feature_pit_rank": {
                "prior_observations": PIT_LOOKBACK,
                "current_excluded": True,
                "formula": "2 * mean(prior_value <= current_value) - 1",
                "tie_rule": "inclusive_less_or_equal",
            },
            "composite": "(U_OFI + U_PREMIUM) / 2",
            "tail_threshold": {
                "prior_abs_composites": TAIL_LOOKBACK,
                "current_excluded": True,
                "quantile": {
                    "numerator": FIXED_QUANTILE_NUMERATOR,
                    "denominator": FIXED_QUANTILE_DENOMINATOR,
                },
                "interpolation": "a[188] + 0.25 * (a[189] - a[188])",
                "comparator": "abs(C) >= threshold",
                "runtime_quantile_parameter": False,
                "quantile_sweep": "forbidden",
            },
        },
        "basket": {
            "candidate": "any(symbol_tail_candidate)",
            "maximum_events_per_epoch": 1,
            "winner_rule": "largest_abs_C_then_SIGNAL_SYMBOLS_order",
            "tie_order": list(SIGNAL_SYMBOLS),
        },
        "data_integrity": {
            "complete_only": True,
            "imputation": "forbidden",
            "missing_or_gap": "not_evaluable_and_reported_not_false",
            "required_evidence_fields": sorted(REQUIRED_EVIDENCE_FIELDS),
            "publish": [
                "gap_status",
                "endpoint_host",
                "endpoint_path",
                "endpoint_version",
                "schema_version",
                "raw_payload_sha256",
            ],
        },
        "exploration_isolation": {
            "artifact": "dfc-retro-probe-v1",
            "artifact_window": "2023-08-04T00:00:00Z/2026-08-03T00:00:00Z",
            "label": "design_only_exploration",
            "forbidden_uses": [
                "v2_performance_claim",
                "v2_promotion_evidence",
                "v2_incidence_adjudication",
            ],
        },
        "estimand": {
            "name": "winner_conditional_next_4h_absolute_log_return_delta",
            "outcome": "abs(log(close[e+4h] / close[e])) * 10000",
            "candidate_term": "mean(outcome of the sole pre-fixed winner where candidate_any is true)",
            "control_term": "mean(cross-sectional mean outcome of the three symbols where candidate_any is false)",
            "estimate": "candidate_term - control_term",
            "outcome_source": "complete 4h Binance USD-M Kline close",
            "not_a_claim_of": [
                "directional_alpha",
                "trading_pnl",
                "execution_readiness",
            ],
            "no_alternate_horizon_or_control": True,
        },
        "promotion_budget": {
            "legacy_608_effective_outcomes_inherited": False,
            "legacy_365_day_cap_inherited": False,
            "historical_backtest": {
                "runs": 1,
                "warmup_start_utc": HISTORICAL_WARMUP_START_UTC,
                "holdout_start_utc": HISTORICAL_HOLDOUT_START_UTC,
                "holdout_end_utc": HISTORICAL_HOLDOUT_END_UTC,
                "scheduled_epochs": HISTORICAL_HOLDOUT_SCHEDULED_EPOCHS,
                "calendar_days": 180,
                "purpose": "bounded_exact_feature_backtest",
            },
            "prospective_no_order_shadow": {
                "runs": 1,
                "calendar_days": PROSPECTIVE_SHADOW_DAYS,
                "scheduled_epochs": PROSPECTIVE_SHADOW_SCHEDULED_EPOCHS,
                "operational_t0": "unassigned_requires_new_pilot_manifest",
            },
            "orders": 0,
            "binance_demo_contact": 0,
            "account_assignments": 0,
            "automatic_promotion": False,
            "rationale": "A single six-month historical implementation check and four-week no-order pilot bound effort without tuning to a desired firing rate or inheriting the prior long-run budget.",
        },
        "binance_demo_boundary": {
            "automatic_account_release": False,
            "automatic_successor_assignment": False,
            "required_before_any_future_assignment": [
                "v2_historical_holdout_report",
                "separate_operator_assignment_submission",
                "fresh_read_only_account_wide_flatness_check",
                "new_pilot_manifest_and_t0",
            ],
            "forbidden_reuse": [
                "old_t0",
                "old_correlation_id",
                "old_ledger_identity",
                "old_order_authority",
            ],
        },
        "oi_collector_disposition": {
            "recommendation": "stop_subject_to_operator_execution",
            "execution_performed": False,
            "rationale": "v2 is fixed to OFI plus premium only; retaining OI for this lane would require a different three-component contract and formal long-history source or prospective accumulation, which this proxy cannot shorten.",
        },
        "implementation": {
            "module": "research_contracts.dfc_2c_4h_v2",
            "algorithm_version": "dfc-2c-4h-v2-algorithm.v1",
            "canonicalization": "research_contracts.canonical_hash",
            "io": "none",
        },
    }


def canonical_contract_hash() -> str:
    """Return the canonical SHA-256 over the complete JSON-ready contract."""

    return canonical_sha256(contract_as_machine_data())


CANONICAL_HASH: Final = canonical_contract_hash()
