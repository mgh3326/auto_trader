"""Immutable, offline-only DFC-2C-4H v2.2 re-registration.

Closes the v2.1 R3 outcome-layer BLOCKER (free bool/price inputs) and unifies
adjudication literals per NW-F7.  ``dfc_2c_4h_v21`` / ``dfc_2c_4h_v2`` are
intentionally not imported or modified: both remain provenance predecessors.
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
    "OutcomeEpochRecord",
    "AdjudicationResult",
    "absolute_log_return_bps",
    "extract_kline_close_evidence",
    "make_outcome_epoch_record",
    "stationary_block_bootstrap",
    "adjudicate_outcomes",
    "INTERVAL_MS",
    "RUN_INVALID_OUTCOME_EVIDENCE",
    "STATUS_PASS",
    "STATUS_INCONCLUSIVE",
    "STATUS_FALSIFIED",
    "STATUS_RUN_INVALID",
]

# The literal is replaced by the source digest itself.  The verifier below
# hashes the source with this one literal normalized, so the digest is not a
# circular input.  Exactly one declaration is required.
MODULE_SOURCE_SHA256: Final = (
    "86efaf3db506f77981622b490465ab91b13c08825ff25465c81af72c687d26e9"
)
HARNESS_SOURCE_SHA256: Final = (
    "762b5884e0e1bba4dbd4b6270b17dfe695047b38d6bd1c5b3717f9fba25d9386"
)
_SOURCE_DECLARATION = re.compile(
    r'MODULE_SOURCE_SHA256: Final = \(\s*"([0-9a-f]{64})"\s*\)', re.DOTALL
)

CONTRACT_ID: Final = "DFC-2C-4H-v2.2"
CONTRACT_SCHEMA_VERSION: Final = "dfc-2c-4h-v2.2.contract.v1"
EPOCH_HOURS: Final = 4
INTERVAL_MS: Final = 4 * 60 * 60 * 1000
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

# NW-F7 verdict literals (exact).
STATUS_PASS: Final = "PASS"
STATUS_INCONCLUSIVE: Final = "INCONCLUSIVE"
STATUS_FALSIFIED: Final = "FALSIFIED"
STATUS_RUN_INVALID: Final = "RUN_INVALID"
RUN_INVALID_OUTCOME_EVIDENCE: Final = "RUN_INVALID_OUTCOME_EVIDENCE"

# NW-F7: power claim removed — planning_sd cannot be validated without data
# contact; do not ship an unverifiable power claim.
B7_RULE: Final = {
    "delta_threshold_bps": 5.0,
    "bootstrap": "stationary_block_percentile",
    "block_length_epochs": 24,
    "repetitions": 10_000,
    "confidence_level": 0.95,
    "alpha": 0.05,
    "minimum_candidate_epochs": 400,
    "minimum_control_epochs": 400,
    "historical_validation_runs": 1,
    "pass": (
        "candidate_minus_control 95% CI lower bound > 5 bps AND two-sided p < 0.05"
    ),
    "falsified": "negation of PASS (when not INCONCLUSIVE and not RUN_INVALID)",
    "inconclusive": "either arm has fewer than its minimum epochs",
    "run_invalid": "input/evidence violation — highest precedence",
    "pass_falsified_unification": "PASS condition negation = FALSIFIED",
    "post_result_threshold_horizon_universe_change": "forbidden",
}

V21_PROVENANCE: Final = {
    "identity": "DFC-2C-4H-v2.1",
    "module": "research_contracts.dfc_2c_4h_v21",
    "source_commit": "9f605139044605a5f31e5ee3da77133924126197",
    "source_branch": "origin/feature/DFC-2C-4H-v21-pr",
    "blob_port": "byte-identical; not whole-branch merge",
    "mutation": "forbidden",
}


def _assert_module_source_frozen() -> None:
    source = inspect.getsource(inspect.getmodule(_assert_module_source_frozen))
    matches = list(_SOURCE_DECLARATION.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("v2.2 source digest declaration count must equal one")
    declared = matches[0].group(1)
    normalized = source[: matches[0].start(1)] + "0" * 64 + source[matches[0].end(1) :]
    actual = hashlib.sha256(normalized.encode()).hexdigest()
    if declared != actual:
        raise RuntimeError(
            f"DFC v2.2 implementation source hash mismatch: declared={declared}, actual={actual}"
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
class _KlineCloseEvidence:
    """Close price bound to a validated raw kline payload (not a free float)."""

    symbol: str
    epoch_start_ms: int
    close: float
    payload_sha256: str
    source_id: str
    complete: bool


@dataclass(frozen=True, slots=True)
class _OutcomeBinding:
    """Proves an outcome was derived from BasketDecision + raw kline evidence."""

    winner_symbol: str
    signal_epoch_start_ms: int
    entry_payload_sha256: str
    exit_payload_sha256: str
    decision_candidate_any: bool
    decision_winner: str


@dataclass(frozen=True, slots=True)
class OutcomeObservation:
    """Arm label and bps are never free inputs — only factory-produced bindings."""

    candidate: bool
    outcome_bps: float
    binding: _OutcomeBinding

    def __post_init__(self) -> None:
        if not isinstance(self.binding, _OutcomeBinding):
            raise TypeError(
                "OutcomeObservation requires an evidence binding from make_outcome_epoch_record"
            )
        object.__setattr__(
            self, "outcome_bps", _finite("outcome_bps", self.outcome_bps)
        )
        if self.candidate != self.binding.decision_candidate_any:
            raise ValueError("arm label must equal BasketDecision.candidate_any")
        if self.binding.decision_winner != self.binding.winner_symbol:
            raise ValueError("winner symbol must equal BasketDecision.winner")


@dataclass(frozen=True, slots=True)
class OutcomeEpochRecord:
    """One signal-epoch outcome slot.

    Missing/incomplete next bar is recorded as RUN_INVALID_OUTCOME_EVIDENCE —
    rows must not be silently deleted.
    """

    status: str
    observation: OutcomeObservation | None = None

    def __post_init__(self) -> None:
        if self.status == "ok":
            if self.observation is None:
                raise ValueError("ok outcome record requires an observation")
            if not isinstance(self.observation, OutcomeObservation):
                raise TypeError("observation must be OutcomeObservation")
        elif self.status == RUN_INVALID_OUTCOME_EVIDENCE:
            if self.observation is not None:
                raise ValueError("invalid outcome record must not carry an observation")
        else:
            raise ValueError(f"unknown outcome epoch status: {self.status}")


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    status: str
    delta_bps: float | None
    ci_lower_bps: float | None
    ci_upper_bps: float | None
    p_value: float | None
    bootstrap_deltas_bps: tuple[float, ...]
    reason_code: str | None = None


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


def _payload_sha256(payload: Sequence[Any] | Mapping[str, Any]) -> str:
    import json

    if isinstance(payload, Mapping):
        canonical_payload: Any = {
            str(key): payload[key] for key in sorted(payload, key=str)
        }
    else:
        canonical_payload = list(payload)
    encoded = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _numeric_payload_value(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"raw candle payload field {name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"raw candle payload field {name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"raw candle payload field {name} must be finite")
    return parsed


def extract_kline_close_evidence(
    *,
    symbol: str,
    epoch_start_ms: int,
    payload: Sequence[Any] | Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> _KlineCloseEvidence:
    """Bind a close price to raw kline evidence. Free float close is not accepted."""
    validate_evidence_manifest(manifest)
    if manifest.get("symbol") != symbol:
        raise ValueError("kline close evidence symbol does not match manifest")
    if manifest.get("source_id") != "binance_usdm.klines_4h":
        raise ValueError("outcome close evidence requires binance_usdm.klines_4h")
    digest = _payload_sha256(payload)
    if manifest.get("raw_payload_sha256") != digest:
        raise ValueError("raw payload hash does not match evidence manifest")
    if isinstance(payload, Mapping):
        close_raw = payload.get("close")
        open_time_raw = payload.get("open_time_ms")
    else:
        if len(payload) < 12:
            raise ValueError(
                "raw candle payload has fewer than the required 12 Binance fields"
            )
        close_raw = payload[4]
        open_time_raw = payload[0]
    close = _numeric_payload_value(close_raw, name="close")
    open_time = int(_numeric_payload_value(open_time_raw, name="open_time_ms"))
    if open_time != epoch_start_ms:
        raise ValueError("raw candle open time does not match epoch start")
    if close <= 0:
        raise ValueError("close prices must be strictly positive")
    complete = manifest.get("complete") is True and manifest.get("gap_status") == "none"
    return _KlineCloseEvidence(
        symbol=symbol,
        epoch_start_ms=epoch_start_ms,
        close=close,
        payload_sha256=digest,
        source_id=str(manifest["source_id"]),
        complete=complete,
    )


def make_outcome_epoch_record(
    decision: BasketDecision,
    *,
    entry: _KlineCloseEvidence,
    next_bar: _KlineCloseEvidence | None,
) -> OutcomeEpochRecord:
    """NW-F4: arm from decision.candidate_any, symbol from decision.winner.

    Outcome bps = absolute log return of winner's completed t kline close to the
    immediately next completed 4h kline close.  Both prices come only from
    extract_kline_close_evidence (raw payload).  Free bool / free price inputs
    are not accepted.  Missing or incomplete next bar → RUN_INVALID_OUTCOME_EVIDENCE
    (row deletion is forbidden).
    """
    if not isinstance(decision, BasketDecision):
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if not isinstance(entry, _KlineCloseEvidence):
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if decision.candidate_any is None or decision.winner is None:
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if not entry.complete or entry.source_id != "binance_usdm.klines_4h":
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if entry.symbol != decision.winner:
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if next_bar is None:
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if not isinstance(next_bar, _KlineCloseEvidence):
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    if (
        not next_bar.complete
        or next_bar.source_id != "binance_usdm.klines_4h"
        or next_bar.symbol != decision.winner
        or next_bar.epoch_start_ms != entry.epoch_start_ms + INTERVAL_MS
    ):
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    try:
        bps = absolute_log_return_bps(entry.close, next_bar.close)
    except ValueError:
        return OutcomeEpochRecord(status=RUN_INVALID_OUTCOME_EVIDENCE)
    observation = OutcomeObservation(
        candidate=bool(decision.candidate_any),
        outcome_bps=bps,
        binding=_OutcomeBinding(
            winner_symbol=decision.winner,
            signal_epoch_start_ms=entry.epoch_start_ms,
            entry_payload_sha256=entry.payload_sha256,
            exit_payload_sha256=next_bar.payload_sha256,
            decision_candidate_any=bool(decision.candidate_any),
            decision_winner=decision.winner,
        ),
    )
    return OutcomeEpochRecord(status="ok", observation=observation)


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
    records: Sequence[OutcomeEpochRecord],
) -> AdjudicationResult:
    """NW-F7 adjudication with RUN_INVALID highest precedence.

    Order (code-enforced):
      1. any input/evidence violation → RUN_INVALID
      2. sample shortfall → INCONCLUSIVE
      3. PASS iff CI lower > 5bp AND two-sided p < 0.05; else FALSIFIED
         (PASS condition negation = FALSIFIED)
    """
    # 1) RUN_INVALID first — type/evidence violations before any other verdict.
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return AdjudicationResult(
            STATUS_RUN_INVALID, None, None, None, None, (), "RUN_INVALID_INPUT"
        )
    materialized = tuple(records)
    for item in materialized:
        if not isinstance(item, OutcomeEpochRecord):
            return AdjudicationResult(
                STATUS_RUN_INVALID,
                None,
                None,
                None,
                None,
                (),
                "RUN_INVALID_INPUT",
            )
        if item.status != "ok":
            return AdjudicationResult(
                STATUS_RUN_INVALID,
                None,
                None,
                None,
                None,
                (),
                item.status
                if item.status.startswith("RUN_INVALID")
                else RUN_INVALID_OUTCOME_EVIDENCE,
            )
        if item.observation is None or not isinstance(
            item.observation, OutcomeObservation
        ):
            return AdjudicationResult(
                STATUS_RUN_INVALID,
                None,
                None,
                None,
                None,
                (),
                RUN_INVALID_OUTCOME_EVIDENCE,
            )
        if not isinstance(item.observation.binding, _OutcomeBinding):
            return AdjudicationResult(
                STATUS_RUN_INVALID,
                None,
                None,
                None,
                None,
                (),
                RUN_INVALID_OUTCOME_EVIDENCE,
            )

    observations = tuple(item.observation for item in materialized)
    assert all(obs is not None for obs in observations)
    candidate = tuple(item.outcome_bps for item in observations if item.candidate)
    control = tuple(item.outcome_bps for item in observations if not item.candidate)
    rule = B7_RULE

    # 2) INCONCLUSIVE on sample shortfall.
    if (
        len(candidate) < rule["minimum_candidate_epochs"]
        or len(control) < rule["minimum_control_epochs"]
    ):
        return AdjudicationResult(
            STATUS_INCONCLUSIVE, None, None, None, None, (), "SAMPLE_SHORTFALL"
        )

    # 3) PASS vs FALSIFIED (FALSIFIED = negation of PASS).
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
    pass_condition = lower > rule["delta_threshold_bps"] and p_value < rule["alpha"]
    status = STATUS_PASS if pass_condition else STATUS_FALSIFIED
    return AdjudicationResult(status, delta, lower, upper, p_value, bootstrap, None)


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
        raise ValueError("evidence source_id is not in the v2.2 allowlist")
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
            "v21_identity": V21_PROVENANCE["identity"],
            "v21_module": V21_PROVENANCE["module"],
            "v21_source_commit": V21_PROVENANCE["source_commit"],
            "v21_source_branch": V21_PROVENANCE["source_branch"],
            "v21_blob_port": V21_PROVENANCE["blob_port"],
            "v21_row_mutation": V21_PROVENANCE["mutation"],
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
            "outcome": (
                "absolute_log_return_bps of BasketDecision.winner from completed "
                "signal-epoch kline close to the immediately next completed 4h kline close"
            ),
            "arm_label": "BasketDecision.candidate_any (not a free bool)",
            "symbol": "BasketDecision.winner (not a free symbol)",
            "candidate_term": "mean outcome of argmax absolute composite on candidate epochs",
            "control_term": "mean outcome of argmax absolute composite on non-candidate epochs",
            "control_choice": "A",
            "selection_symmetric": True,
            "selection_implementation": "evaluate_basket argmax over all three scores",
            "outcome_implementation": "make_outcome_epoch_record",
            "outcome_free_bool_price": "forbidden",
            "missing_next_bar": RUN_INVALID_OUTCOME_EVIDENCE,
            "row_deletion_on_missing_next_bar": "forbidden",
            "bootstrap_implementation": "stationary_block_bootstrap",
            "adjudication_implementation": "adjudicate_outcomes",
            "pnl_claim": False,
        },
        "adjudication": B7_RULE,
        "implementation": {
            "module": "research_contracts.dfc_2c_4h_v22",
            "algorithm_version": "dfc-2c-4h-v2.2-algorithm.v1",
            "module_source_sha256": MODULE_SOURCE_SHA256,
            "harness_source_sha256": HARNESS_SOURCE_SHA256,
            "io": "none",
            "enforcement": "import-time source digest for contract and harness; edit causes RuntimeError",
        },
        "harness": {
            "module": "research_contracts.dfc_2c_4h_v22_harness",
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
