"""Immutable, offline-only DFC-2C-4H v2.1 re-registration.

This is a new registration.  ``dfc_2c_4h_v2`` is intentionally not imported
or modified: the 180-day v2 seal remains a historical predecessor.
"""

from __future__ import annotations

import hashlib
import inspect
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from research_contracts.canonical_hash import canonical_sha256

__all__ = [
    "CANONICAL_HASH",
    "CONTRACT_ID",
    "CONTRACT_SCHEMA_VERSION",
    "MODULE_SOURCE_SHA256",
    "B6_CONTROL",
    "B7_RULE",
    "IntegrityState",
    "SymbolScore",
    "BasketDecision",
    "score_symbol",
    "evaluate_basket",
    "pit_rank",
    "tail_threshold_q75",
    "ofi_from_base_volumes",
    "premium_index_close_from_complete_4h",
    "select_universe",
    "contract_as_machine_data",
    "canonical_contract_hash",
    "validate_evidence_manifest",
    "OutcomeObservation",
    "AdjudicationResult",
    "absolute_log_return_bps",
    "make_outcome_observation",
    "stationary_block_bootstrap",
    "adjudicate_outcomes",
]

# The literal is replaced by the source digest itself.  The verifier below
# hashes the source with this one literal normalized, so the digest is not a
# circular input.  Exactly one declaration is required.
MODULE_SOURCE_SHA256: Final = (
    "dbb8d9ec5c1004face8865b7324f946093084c4cb5be4aa99d8f62fc240c54c2"
)
HARNESS_SOURCE_SHA256: Final = (
    "c081080e20b5a2d79e557f50d3fcd909c2a055e37f8e5291bf929c70b0fe46f4"
)
_SOURCE_DECLARATION = re.compile(
    r'MODULE_SOURCE_SHA256: Final = \(\s*"([0-9a-f]{64})"\s*\)', re.DOTALL
)

CONTRACT_ID: Final = "DFC-2C-4H-v2.1"
CONTRACT_SCHEMA_VERSION: Final = "dfc-2c-4h-v2.1.contract.v1"
EPOCH_HOURS: Final = 4
PIT_LOOKBACK: Final = 252
TAIL_LOOKBACK: Final = 252
TAIL_CONTEXT: Final = PIT_LOOKBACK + TAIL_LOOKBACK
FIXED_QUANTILE_NUMERATOR: Final = 3
FIXED_QUANTILE_DENOMINATOR: Final = 4
EXPLORATION_WINDOW: Final = "2023-08-04T00:00:00Z/2026-08-03T00:00:00Z"
BACKTEST_WINDOW: Final = "2021-05-02T00:00:00Z/2023-08-04T00:00:00Z"
BACKTEST_CALENDAR_DAYS: Final = 824
BACKTEST_SCHEDULED_EPOCHS: Final = 4_944
WARMUP_START: Final = "2021-02-02T00:00:00Z"
UNIVERSE_LOOKBACK_DAYS: Final = 30
UNIVERSE_TOP_K: Final = 3
SYMBOL_PRIORITY = MappingProxyType({})
B6_CONTROL: Final = "A"
B7_RULE: Final = {
    "delta_threshold_bps": 5.0,
    "bootstrap": "stationary_block_percentile",
    "block_length_epochs": 24,
    "repetitions": 10_000,
    "confidence_level": 0.95,
    "alpha": 0.05,
    "minimum_candidate_epochs": 400,
    "minimum_control_epochs": 400,
    "power": {
        "target": 0.80,
        "planning_effect_bps": 5.0,
        "planning_sd_bps": 25.0,
        "groups": 2,
        "epochs_per_group": 400,
        "two_sided_alpha": 0.05,
        "normal_approximation": True,
    },
    "success": "candidate_minus_matched_control CI lower bound > 5 bps and two-sided p < 0.05",
    "failure": "CI upper bound <= 5 bps or two-sided p >= 0.05",
    "indeterminate": "either arm has fewer than its minimum epochs",
}


def _assert_module_source_frozen() -> None:
    source = inspect.getsource(inspect.getmodule(_assert_module_source_frozen))
    matches = list(_SOURCE_DECLARATION.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("v2.1 source digest declaration count must equal one")
    declared = matches[0].group(1)
    normalized = source[: matches[0].start(1)] + "0" * 64 + source[matches[0].end(1) :]
    actual = hashlib.sha256(normalized.encode()).hexdigest()
    if declared != actual:
        raise RuntimeError(
            f"DFC v2.1 implementation source hash mismatch: declared={declared}, actual={actual}"
        )


_assert_module_source_frozen()


class IntegrityState(str):
    COMPLETE = "complete"
    GAP = "gap"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _EvidenceBinding:
    symbol: str
    current_epoch_start_ms: int
    kline_payload_hashes: tuple[str, ...]
    premium_payload_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _EpochFeatures:
    symbol: str
    integrity: str
    current_ofi: float | None
    current_premium_close: float | None
    prior_ofi: tuple[float, ...]
    prior_premium_close: tuple[float, ...]
    prior_quote_volume_30d: float
    evidence: _EvidenceBinding

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be a non-empty canonical uppercase symbol")
        object.__setattr__(self, "prior_ofi", tuple(self.prior_ofi))
        object.__setattr__(self, "prior_premium_close", tuple(self.prior_premium_close))
        if not isinstance(self.evidence, _EvidenceBinding):
            raise ValueError("features must carry an evidence binding")
        if self.evidence.symbol != self.symbol or len(self.prior_ofi) != 504:
            raise ValueError(
                "features evidence binding does not match the feature window"
            )


@dataclass(frozen=True, slots=True)
class SymbolScore:
    symbol: str
    composite: float
    threshold: float
    is_candidate: bool


@dataclass(frozen=True, slots=True)
class BasketDecision:
    candidate_any: bool | None
    winner: str | None
    scores: tuple[SymbolScore, ...]


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    candidate: bool
    outcome_bps: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "outcome_bps", _finite("outcome_bps", self.outcome_bps)
        )


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    status: str
    delta_bps: float | None
    ci_lower_bps: float | None
    ci_upper_bps: float | None
    p_value: float | None
    bootstrap_deltas_bps: tuple[float, ...]


def _finite(name: str, value: float | None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{name} must be finite numeric")
    return float(value)


def _history(name: str, values: Sequence[float], expected: int) -> tuple[float, ...]:
    if len(values) != expected:
        raise ValueError(f"{name} must contain exactly {expected} values")
    return tuple(_finite(f"{name}[{i}]", value) for i, value in enumerate(values))


def ofi_from_base_volumes(
    total_base_volume: float, taker_buy_base_volume: float
) -> float:
    total = _finite("total_base_volume", total_base_volume)
    buy = _finite("taker_buy_base_volume", taker_buy_base_volume)
    sell = total - buy
    if total <= 0 or buy <= 0 or sell <= 0:
        raise ValueError("complete Kline requires strictly positive base buy and sell")
    return math.log(buy / sell)


def premium_index_close_from_complete_4h(value: float, *, is_complete: bool) -> float:
    if is_complete is not True:
        raise ValueError("premium-index candle must be complete")
    return _finite("premium_index_close", value)


def pit_rank(current: float, prior_values: Sequence[float]) -> float:
    current_value = _finite("current", current)
    history = _history("prior_values", prior_values, 252)
    return 2.0 * (sum(value <= current_value for value in history) / 252.0) - 1.0


def _derived_prior_abs_composites(
    prior_ofi: Sequence[float], prior_premium: Sequence[float]
) -> tuple[float, ...]:
    ofi = _history("prior_ofi", prior_ofi, 504)
    premium = _history("prior_premium_close", prior_premium, 504)
    result: list[float] = []
    for index in range(252, 504):
        result.append(
            abs(
                (
                    pit_rank(ofi[index], ofi[index - 252 : index])
                    + pit_rank(premium[index], premium[index - 252 : index])
                )
                / 2.0
            )
        )
    return tuple(result)


def tail_threshold_q75(
    prior_ofi: Sequence[float], prior_premium_close: Sequence[float]
) -> float:
    ordered = sorted(_derived_prior_abs_composites(prior_ofi, prior_premium_close))
    return ordered[188] + 0.25 * (ordered[189] - ordered[188])


def score_symbol(inputs: _EpochFeatures) -> SymbolScore:
    if not isinstance(inputs, _EpochFeatures) or not isinstance(
        inputs.evidence, _EvidenceBinding
    ):
        raise TypeError("score_symbol accepts only evidence-bound epoch features")
    if inputs.integrity != IntegrityState.COMPLETE:
        raise ValueError("only complete symbol epochs can be scored")
    ofi = _history("prior_ofi", inputs.prior_ofi, 504)
    premium = _history("prior_premium_close", inputs.prior_premium_close, 504)
    if inputs.current_ofi is None or inputs.current_premium_close is None:
        raise ValueError("complete epoch is missing a current feature")
    current_ofi = _finite("current_ofi", inputs.current_ofi)
    current_premium = premium_index_close_from_complete_4h(
        inputs.current_premium_close, is_complete=True
    )
    composite = (
        pit_rank(current_ofi, ofi[-252:]) + pit_rank(current_premium, premium[-252:])
    ) / 2.0
    threshold = tail_threshold_q75(ofi, premium)
    return SymbolScore(inputs.symbol, composite, threshold, abs(composite) >= threshold)


def select_universe(prior_30d_quote_volume: Mapping[str, float]) -> tuple[str, ...]:
    """Select top-K symbols using only the immediately prior 30 calendar days."""
    if len(prior_30d_quote_volume) < 3:
        raise ValueError("PIT universe requires at least three eligible symbols")
    ranked = sorted(
        (
            (_finite(f"quote_volume[{symbol}]", volume), symbol)
            for symbol, volume in prior_30d_quote_volume.items()
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(symbol for _, symbol in ranked[:3])


@dataclass(frozen=True, slots=True)
class _PITBasket:
    selected_symbols: tuple[str, ...]
    inputs: tuple[_EpochFeatures, ...]


def evaluate_basket(inputs: _PITBasket) -> BasketDecision:
    if not isinstance(inputs, _PITBasket):
        raise TypeError("evaluate_basket accepts only an evidence-bound PIT basket")
    if len(inputs.selected_symbols) != 3 or len(inputs.inputs) != 3:
        raise ValueError("basket requires exactly the three PIT-selected symbols")
    if tuple(sorted(inputs.selected_symbols)) != tuple(
        sorted(item.symbol for item in inputs.inputs)
    ):
        raise ValueError("basket symbols do not equal the PIT-selected universe")
    if any(item.integrity != IntegrityState.COMPLETE for item in inputs.inputs):
        return BasketDecision(None, None, ())
    scores = tuple(
        score_symbol(item)
        for item in sorted(inputs.inputs, key=lambda item: item.symbol)
    )
    candidates = tuple(score for score in scores if score.is_candidate)
    # Selection is identical in both arms: all symbols are eligible for the
    # argmax, while candidate_any remains an arm label for the outcome layer.
    winner = min(scores, key=lambda score: (-abs(score.composite), score.symbol))
    return BasketDecision(bool(candidates), winner.symbol, scores)


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("outcome arm must not be empty")
    return sum(values) / len(values)


def absolute_log_return_bps(entry_close: float, exit_close: float) -> float:
    entry = _finite("entry_close", entry_close)
    exit_value = _finite("exit_close", exit_close)
    if entry <= 0 or exit_value <= 0:
        raise ValueError("close prices must be strictly positive")
    return abs(math.log(exit_value / entry)) * 10_000.0


def make_outcome_observation(
    candidate: bool, entry_close: float, exit_close: float
) -> OutcomeObservation:
    return OutcomeObservation(
        candidate, absolute_log_return_bps(entry_close, exit_close)
    )


def stationary_block_bootstrap(
    candidate_outcomes: Sequence[float],
    control_outcomes: Sequence[float],
    *,
    repetitions: int = 10_000,
    block_length_epochs: int = 24,
    seed: int = 2_021_0502,
) -> tuple[float, ...]:
    """Deterministic circular stationary-block bootstrap of mean deltas."""
    candidate = tuple(
        _finite("candidate_outcome", value) for value in candidate_outcomes
    )
    control = tuple(_finite("control_outcome", value) for value in control_outcomes)
    if not candidate or not control or repetitions <= 0 or block_length_epochs <= 0:
        raise ValueError("bootstrap inputs must be positive and both arms non-empty")
    import random

    rng = random.Random(seed)
    result: list[float] = []
    probability = 1.0 / block_length_epochs
    for _ in range(repetitions):
        samples: list[float] = []
        index = rng.randrange(len(candidate))
        while len(samples) < len(candidate):
            samples.append(candidate[index])
            index = (
                (index + 1) % len(candidate)
                if rng.random() >= probability
                else rng.randrange(len(candidate))
            )
        candidate_mean = _mean(samples)
        samples = []
        index = rng.randrange(len(control))
        while len(samples) < len(control):
            samples.append(control[index])
            index = (
                (index + 1) % len(control)
                if rng.random() >= probability
                else rng.randrange(len(control))
            )
        result.append(candidate_mean - _mean(samples))
    return tuple(result)


def adjudicate_outcomes(
    observations: Sequence[OutcomeObservation],
) -> AdjudicationResult:
    candidate = tuple(item.outcome_bps for item in observations if item.candidate)
    control = tuple(item.outcome_bps for item in observations if not item.candidate)
    rule = B7_RULE
    if (
        len(candidate) < rule["minimum_candidate_epochs"]
        or len(control) < rule["minimum_control_epochs"]
    ):
        return AdjudicationResult("indeterminate", None, None, None, None, ())
    delta = _mean(candidate) - _mean(control)
    bootstrap = stationary_block_bootstrap(
        candidate,
        control,
        repetitions=rule["repetitions"],
        block_length_epochs=rule["block_length_epochs"],
    )
    ordered = sorted(bootstrap)
    lower = ordered[int((1.0 - rule["confidence_level"]) / 2.0 * len(ordered))]
    upper = ordered[
        int((1.0 - (1.0 - rule["confidence_level"]) / 2.0) * len(ordered)) - 1
    ]
    lower_tail = (sum(value <= 0.0 for value in bootstrap) + 1) / (len(bootstrap) + 1)
    upper_tail = (sum(value >= 0.0 for value in bootstrap) + 1) / (len(bootstrap) + 1)
    p_value = min(1.0, 2.0 * min(lower_tail, upper_tail))
    status = (
        "success"
        if lower > rule["delta_threshold_bps"] and p_value < rule["alpha"]
        else "failure"
    )
    return AdjudicationResult(status, delta, lower, upper, p_value, bootstrap)


REQUIRED_EVIDENCE_FIELDS = frozenset(
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
ALLOWED_SOURCE_IDS = frozenset(
    {
        "binance_usdm.klines_4h",
        "binance_usdm.premium_index_klines_4h",
    }
)


def validate_evidence_manifest(record: Mapping[str, Any]) -> None:
    missing = sorted(
        field for field in REQUIRED_EVIDENCE_FIELDS if record.get(field) in (None, "")
    )
    if missing:
        raise ValueError(f"missing required evidence fields: {', '.join(missing)}")
    if record["complete"] is not True or record["gap_status"] != "none":
        raise ValueError("only complete, gap-free evidence is admissible")
    if record["source_id"] not in ALLOWED_SOURCE_IDS:
        raise ValueError("evidence source_id is not in the v2.1 allowlist")
    digest = record["raw_payload_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("raw_payload_sha256 must be lowercase 64-character hex")


def contract_as_machine_data() -> dict[str, Any]:
    return {
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "registration_mode": "independent_new_registration",
        "predecessor_preservation": {
            "v2_identity": "DFC-2C-4H-v2",
            "v2_row_mutation": "forbidden",
            "v2_seal_mutation": "forbidden",
            "v2_supersede_or_hide": "forbidden",
        },
        "backtest_window": {
            "warmup_start_utc": WARMUP_START,
            "holdout_start_utc": BACKTEST_WINDOW.split("/")[0],
            "holdout_end_utc_exclusive": BACKTEST_WINDOW.split("/")[1],
            "calendar_days": BACKTEST_CALENDAR_DAYS,
            "scheduled_epochs": BACKTEST_SCHEDULED_EPOCHS,
            "selection_basis": "exclude the 1,632-epoch exploration overlap to remove the design-validation feedback loop",
        },
        "exploration_isolation": {
            "window": EXPLORATION_WINDOW,
            "relationship": "no overlap remains in the adopted backtest window",
            "excluded_overlap": "2023-08-04T00:00:00Z/2024-05-02T00:00:00Z",
            "excluded_overlap_epochs": 1_632,
            "excluded_overlap_percent_of_original_three_year_window": 24.817518248175183,
            "exclusion_reason": "intentionally excluded to remove the design-validation feedback loop",
            "artifact_use": "design_only_never_primary_or_promotion_evidence",
        },
        "universe": {
            "rule": "At each UTC 4h epoch, rank all eligible USD-M perpetual symbols by quote volume summed over the immediately preceding 30 calendar days; select the top 3, ties by canonical uppercase symbol; use no future or exploration-selected symbol list",
            "lookback_days": 30,
            "top_k": 3,
            "hardcoded_symbols": False,
            "pit": True,
        },
        "features": {
            "ofi": "log(taker_buy_base_volume / (total_base_volume - taker_buy_base_volume)) from complete Binance 4h Kline",
            "premium": "complete Binance 4h premium-index candle close",
            "deprecated": [
                "quote_volume_proxy_as_signal",
                "five_minute_premium_average",
            ],
            "complete_only": True,
            "imputation": "forbidden",
        },
        "score": {
            "pit_rank": "current-excluded 252-observation inclusive <= empirical rank",
            "composite": "(ofi_rank + premium_rank) / 2",
            "tail": "linear Q0.75 of 252 derived prior |C| values",
            "tail_context_observations": 504,
            "comparator": "abs(C) >= threshold",
            "runtime_parameterization": "forbidden",
        },
        "basket": {
            "candidate": "any",
            "winner": "largest abs(C), ties by canonical symbol",
            "maximum_events_per_epoch": 1,
        },
        "estimand": {
            "name": "matched_selection_absolute_log_return_delta",
            "outcome": "absolute_log_return_bps(entry_close, exit_close)",
            "candidate_term": "mean outcome of argmax absolute composite on candidate epochs",
            "control_term": "mean outcome of argmax absolute composite on non-candidate epochs",
            "control_choice": "A",
            "selection_symmetric": True,
            "selection_implementation": "evaluate_basket argmax over all three scores",
            "outcome_implementation": "make_outcome_observation",
            "bootstrap_implementation": "stationary_block_bootstrap",
            "adjudication_implementation": "adjudicate_outcomes",
            "pnl_claim": False,
        },
        "adjudication": B7_RULE,
        "implementation": {
            "module": "research_contracts.dfc_2c_4h_v21",
            "algorithm_version": "dfc-2c-4h-v2.1-algorithm.v1",
            "module_source_sha256": MODULE_SOURCE_SHA256,
            "harness_source_sha256": HARNESS_SOURCE_SHA256,
            "io": "none",
            "enforcement": "import-time source digest for contract and harness; edit causes RuntimeError",
        },
        "harness": {
            "module": "research_contracts.dfc_2c_4h_v21_harness",
            "read_only": True,
            "alignment": "UTC 4h inner join",
            "warmup": "504 prior observations plus current epoch",
            "manifest_validation": "every epoch, fail-closed",
            "forward_only": True,
            "raw_payload_sha256": True,
        },
        "promotion_budget": {
            "backtest_runs": 1,
            "orders": 0,
            "demo": 0,
            "account": 0,
            "automatic_promotion": False,
        },
    }


def canonical_contract_hash() -> str:
    return canonical_sha256(contract_as_machine_data())


CANONICAL_HASH: Final = canonical_contract_hash()
