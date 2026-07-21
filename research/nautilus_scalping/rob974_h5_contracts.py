"""ROB-983 (H5, CP1) -- exact input domain, envelope verification, and the
H6-A exact-48 accounting gate.

H5 is a pure consumer: it never opens a DB session, never reimplements H6-A's
trial-accounting reconstruction, and never scores a subset when H6-A's seal
is anything less than the full, exact-48, ``performance_usable`` report.
A typed actual report containing retries remains publishable as an explicit
degraded/incomplete scorecard, but its gates are never evaluated. Any AC34
provenance violation (missing/future/lookahead bar,
signal-close fill, unpriced gap trade, double-charged basket cost, single-leg
notional denominator, a fabricated atomic fill/flatten price, a nonfinite or
type-invalid required value, or missing PBO/accounting/evidence) is always
``incomplete`` -- never ``historical_fail``, regardless of how profitable the
reported economics look.

Strategy: every constant/type here is exact and closed. ``type(x) is float``/
``type(x) is int`` (never ``isinstance``) reject ``bool``/``Decimal``/
subclass masquerade, mirroring the ROB-945/946 pattern (``bool`` is an
``int`` subclass in Python -- ``isinstance(True, int)`` is ``True`` but
``type(True) is int`` is ``False``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from rob974_h4_runner import (
    H4AttributionError,
    S3SelectedOOSAttribution,
    S4SelectedOOSAttribution,
    SelectedOOSAttributionEnvelope,
    validate_attribution_envelope,
)
from rob974_h6a_accounting import CombinedAccountingReport

__all__ = [
    "CONFIGS_PER_STRATEGY",
    "FOLD_IDS",
    "PATH_SCENARIOS",
    "S3_EXIT_REASONS",
    "S3_SYMBOLS",
    "S4_EXIT_REASONS",
    "S4_PAIRS",
    "STRATEGIES",
    "STRUCTURAL_INCOMPLETE_REASONS",
    "CampaignEnvelope",
    "EnvelopeValidationResult",
    "H5InputError",
    "H6AAccountingSeal",
    "H6AAccountingInput",
    "H6AAccountingContractResult",
    "H6A_CONTRACT_STATUSES",
    "FAKE_FREE_EMPIRICAL_CLOSURE",
    "MARKET_RETURN_SEMANTIC",
    "MetricTrade",
    "TradeAttribution",
    "H4AttributionContractResult",
    "authoritative_market_return",
    "classify_provenance_violation",
    "config_ids_for",
    "consume_h4_attribution",
    "fixture_h4_attribution_result",
    "resolve_h6a_accounting_contract",
    "validate_envelope_and_accounting",
]

# ---------------------------------------------------------------------------
# Exact, closed input domain (H5 spec AC1-3).
# ---------------------------------------------------------------------------

STRATEGIES: tuple[str, ...] = ("S3", "S4")
CONFIGS_PER_STRATEGY = 24
FOLD_IDS: tuple[str, ...] = tuple(f"fold-{i:02d}" for i in range(8))
PATH_SCENARIOS: tuple[str, ...] = ("base13", "primary_stress17", "upward_stress22")
S3_SYMBOLS: tuple[str, ...] = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")
S4_PAIRS: tuple[str, ...] = ("XRP-DOGE", "XRP-SOL", "DOGE-SOL")
S3_EXIT_REASONS: tuple[str, ...] = ("TP", "SL", "THESIS_EXIT", "TIMEOUT")
S4_EXIT_REASONS: tuple[str, ...] = ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT")

_EXPECTED_TOTAL_EXPERIMENTS = 48
_CLOSED_STATUSES = ("completed", "rejected", "crashed", "timeout")
_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
H6A_CONTRACT_STATUSES = ("PASS", "FIXTURE_ONLY", "NOT_EVALUATED")


class H5InputError(ValueError):
    """A sealed H5 input failed a fail-closed boundary check."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise H5InputError(reason)


def _require_hex64(value: Any, reason: str) -> str:
    _require(isinstance(value, str) and bool(_LOWERCASE_HEX_64.match(value)), reason)
    return value


def _require_exact_int(value: Any, reason: str, *, min_value: int = 0) -> int:
    _require(type(value) is int and value >= min_value, reason)
    return value


def _require_exact_float(value: Any, reason: str) -> float:
    _require(type(value) is float and math.isfinite(value), reason)
    return value


def config_ids_for(strategy: str) -> tuple[str, ...]:
    _require(strategy in STRATEGIES, "unknown_strategy")
    return tuple(f"{strategy}-{i:02d}" for i in range(CONFIGS_PER_STRATEGY))


_CANONICAL_EXPERIMENT_IDS: tuple[str, ...] = tuple(
    cid for strategy in STRATEGIES for cid in config_ids_for(strategy)
)

# ---------------------------------------------------------------------------
# AC34 structural-incomplete provenance violations. Closed set; classifying
# an unknown code raises rather than silently accepting/ignoring it.
# ---------------------------------------------------------------------------

STRUCTURAL_INCOMPLETE_REASONS: frozenset[str] = frozenset(
    {
        "missing_bar",
        "future_bar",
        "incomplete_bar",
        "lookahead_bar",
        "signal_close_fill",
        "unpriced_gap_trade",
        "basket_cost_double_charge",
        "single_leg_notional_denominator",
        "fabricated_atomic_fill_price",
        "fabricated_flatten_price",
        "nonfinite_required_value",
        "type_invalid_required_value",
        "missing_pbo_evidence",
        "missing_accounting_evidence",
        "missing_evidence",
    }
)


def classify_provenance_violation(violation: str) -> str:
    """A pure function of the violation code alone: every registered AC34
    violation classifies as ``"incomplete"``, never ``"historical_fail"`` --
    no caller-supplied economics can change this."""
    _require(violation in STRUCTURAL_INCOMPLETE_REASONS, "unknown_provenance_violation")
    return "incomplete"


# ---------------------------------------------------------------------------
# Campaign envelope (full campaign / lineage verification, AC4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CampaignEnvelope:
    full_campaign_hash: str
    campaign_run_id: str
    parent_corpus_hash: str
    parent_projection_hash: str
    feature_contract_hash: str
    strategy_contract_hashes: dict[str, str]
    h4_runner_source_hash: str
    h4_pbo_source_hash: str
    h2_engine_source_hash: str
    h3_generator_source_hash: str
    run_schema_version: str
    generator_version: str
    expected_experiment_ids: tuple[str, ...]
    h6a_trial_accounting_hash: str

    def __post_init__(self) -> None:
        for hash_field in (
            self.full_campaign_hash,
            self.parent_corpus_hash,
            self.parent_projection_hash,
            self.feature_contract_hash,
            self.h4_runner_source_hash,
            self.h4_pbo_source_hash,
            self.h2_engine_source_hash,
            self.h3_generator_source_hash,
            self.h6a_trial_accounting_hash,
        ):
            _require_hex64(hash_field, "envelope_hash_not_lowercase_64_hex")

        _require(
            set(self.strategy_contract_hashes.keys()) == set(STRATEGIES),
            "envelope_strategy_contract_hash_keys_mismatch",
        )
        for value in self.strategy_contract_hashes.values():
            _require_hex64(value, "envelope_strategy_contract_hash_not_hex64")

        _require(
            isinstance(self.run_schema_version, str) and self.run_schema_version != "",
            "envelope_run_schema_version_malformed",
        )
        _require(
            isinstance(self.generator_version, str) and self.generator_version != "",
            "envelope_generator_version_malformed",
        )

        ids = self.expected_experiment_ids
        _require(
            isinstance(ids, tuple) and all(isinstance(i, str) for i in ids),
            "envelope_expected_experiment_ids_malformed",
        )
        _require(
            len(ids) == _EXPECTED_TOTAL_EXPERIMENTS,
            "envelope_expected_experiment_ids_count_not_48",
        )
        _require(
            len(set(ids)) == _EXPECTED_TOTAL_EXPERIMENTS,
            "envelope_expected_experiment_ids_has_duplicate",
        )
        _require(
            ids == _CANONICAL_EXPERIMENT_IDS,
            "envelope_expected_experiment_ids_not_canonical_order",
        )


# ---------------------------------------------------------------------------
# H6-A exact-48 accounting seal (AC5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class H6AAccountingSeal:
    expected_total: int
    registered_total: int
    primary_attempts: int
    status_counts: dict[str, int]
    retry_attempts: int
    accounting_complete: bool
    performance_usable: bool
    trial_accounting_hash: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Fix (adversarial verify R1, finding 7): exact-int type check, not
        # `==48` alone -- `Decimal(48)==48` and `48.0==48` are both True in
        # Python, so equality alone let a non-int type slip past.
        _require_exact_int(self.expected_total, "h6a_expected_total_malformed")
        _require(
            self.expected_total == _EXPECTED_TOTAL_EXPERIMENTS,
            "h6a_expected_total_not_48",
        )
        _require_exact_int(self.registered_total, "h6a_registered_total_malformed")
        _require_exact_int(self.primary_attempts, "h6a_primary_attempts_malformed")
        _require_exact_int(self.retry_attempts, "h6a_retry_attempts_malformed")
        _require(
            set(self.status_counts.keys()) == set(_CLOSED_STATUSES),
            "h6a_status_counts_keys_malformed",
        )
        for count in self.status_counts.values():
            _require_exact_int(count, "h6a_status_count_value_malformed")
        status_sum = sum(self.status_counts.values())
        # H6-A counts every clean attempt, including retries, in the closed
        # status histogram. The only exact total available on this projection
        # is therefore primary_attempts + retry_attempts; capping at 48 made a
        # valid degraded actual report impossible to represent (ROB-1005).
        _require(
            status_sum == self.primary_attempts + self.retry_attempts,
            "h6a_status_sum_does_not_match_primary_plus_retry",
        )
        _require(
            self.primary_attempts <= _EXPECTED_TOTAL_EXPERIMENTS,
            "h6a_primary_attempts_exceeds_48",
        )
        _require(
            self.registered_total <= _EXPECTED_TOTAL_EXPERIMENTS
            or not self.accounting_complete,
            "h6a_registered_total_exceeds_48_but_claims_complete",
        )
        if self.accounting_complete:
            _require(
                self.registered_total == _EXPECTED_TOTAL_EXPERIMENTS
                and self.primary_attempts == _EXPECTED_TOTAL_EXPERIMENTS,
                "h6a_complete_without_exact_48_primary_surface",
            )
        _require_hex64(
            self.trial_accounting_hash, "h6a_trial_accounting_hash_malformed"
        )
        _require(
            type(self.accounting_complete) is bool, "h6a_accounting_complete_not_bool"
        )
        _require(
            type(self.performance_usable) is bool, "h6a_performance_usable_not_bool"
        )
        _require(
            isinstance(self.reason_codes, tuple)
            and all(isinstance(r, str) for r in self.reason_codes),
            "h6a_reason_codes_malformed",
        )
        # performance_usable=True requires exact-48/retry-0/all-completed --
        # a caller cannot claim usable=true while the underlying counters
        # disagree (this alone rejects the "complete=true with usable=false
        # allowed, but usable=true with bad counters" asymmetric mutant).
        exact_48_all_completed = (
            self.registered_total == _EXPECTED_TOTAL_EXPERIMENTS
            and self.primary_attempts == _EXPECTED_TOTAL_EXPERIMENTS
            and self.status_counts.get("completed") == _EXPECTED_TOTAL_EXPERIMENTS
            and self.retry_attempts == 0
            and self.accounting_complete
        )
        if self.performance_usable:
            _require(exact_48_all_completed, "h6a_usable_true_with_incomplete_counters")


H6AAccountingInput = CombinedAccountingReport | H6AAccountingSeal | None


@dataclass(frozen=True, slots=True)
class H6AAccountingContractResult:
    """Normalized H6-A provenance plus the H5 accounting projection.

    ``PASS`` can only be produced from H6-A's own ``CombinedAccountingReport``
    type.  A directly constructed H5 ``H6AAccountingSeal`` is useful for
    hermetic predecessor tests but is always ``FIXTURE_ONLY`` even when its
    digest happens to equal a production digest.  ``NOT_EVALUATED`` carries
    no seal and therefore cannot reach canonical scorecard assembly.
    """

    actual_h6a_contract: str
    seal: H6AAccountingSeal | None
    campaign_run_id: str | None

    def __post_init__(self) -> None:
        _require(
            self.actual_h6a_contract in H6A_CONTRACT_STATUSES,
            "h6a_contract_status_unknown",
        )
        if self.actual_h6a_contract == "PASS":
            _require(
                type(self.seal) is H6AAccountingSeal,
                "actual_h6a_contract_seal_missing",
            )
            _require(
                type(self.campaign_run_id) is str and self.campaign_run_id != "",
                "actual_h6a_contract_campaign_run_id_missing",
            )
        elif self.actual_h6a_contract == "FIXTURE_ONLY":
            _require(
                type(self.seal) is H6AAccountingSeal,
                "fixture_h6a_contract_seal_missing",
            )
            _require(
                self.campaign_run_id is None,
                "fixture_h6a_contract_cannot_claim_campaign_run_id",
            )
        else:
            _require(
                self.seal is None and self.campaign_run_id is None,
                "not_evaluated_h6a_contract_must_be_empty",
            )


def _seal_actual_h6a_report(report: CombinedAccountingReport) -> H6AAccountingSeal:
    """Project H6-A's sealed report without reconstructing its trial hash."""
    _require(
        type(report.campaign_run_id) is str and report.campaign_run_id != "",
        "actual_h6a_campaign_run_id_malformed",
    )
    for value, reason in (
        (report.expected_total, "actual_h6a_expected_total_malformed"),
        (report.registered_total, "actual_h6a_registered_total_malformed"),
        (report.primary_attempts, "actual_h6a_primary_attempts_malformed"),
        (report.total_attempts, "actual_h6a_total_attempts_malformed"),
        (report.retry_attempts, "actual_h6a_retry_attempts_malformed"),
    ):
        _require_exact_int(value, reason)
    _require(
        report.expected_total == _EXPECTED_TOTAL_EXPERIMENTS,
        "actual_h6a_expected_total_not_48",
    )
    _require(
        type(report.status_counts) is dict,
        "actual_h6a_status_counts_malformed",
    )
    status_counts = dict(report.status_counts)
    _require(
        set(status_counts) == set(_CLOSED_STATUSES),
        "actual_h6a_status_counts_malformed",
    )
    for count in status_counts.values():
        _require_exact_int(count, "actual_h6a_status_count_malformed")
    _require(
        sum(status_counts.values()) == report.total_attempts,
        "actual_h6a_status_sum_forged_or_stale",
    )
    _require(
        report.total_attempts == report.primary_attempts + report.retry_attempts,
        "actual_h6a_attempt_totals_forged_or_stale",
    )
    for values, reason in (
        (report.missing_row_ids, "actual_h6a_missing_row_ids_malformed"),
        (report.extra_experiment_ids, "actual_h6a_extra_ids_malformed"),
        (report.mismatch_row_ids, "actual_h6a_mismatch_row_ids_malformed"),
        (
            report.duplicate_or_gap_row_ids,
            "actual_h6a_duplicate_or_gap_row_ids_malformed",
        ),
    ):
        _require(
            type(values) is tuple and all(type(value) is str for value in values),
            reason,
        )
    _require(
        type(report.accounting_complete) is bool
        and type(report.all_primary_completed) is bool
        and type(report.performance_usable) is bool,
        "actual_h6a_boolean_status_malformed",
    )
    structural_gaps = (
        report.missing_row_ids
        + report.extra_experiment_ids
        + report.mismatch_row_ids
        + report.duplicate_or_gap_row_ids
    )
    expected_complete = (
        report.registered_total == _EXPECTED_TOTAL_EXPERIMENTS
        and report.primary_attempts == _EXPECTED_TOTAL_EXPERIMENTS
        and not structural_gaps
    )
    _require(
        report.accounting_complete == expected_complete,
        "actual_h6a_accounting_complete_forged_or_stale",
    )
    if report.retry_attempts == 0:
        expected_all_completed = (
            expected_complete
            and status_counts["completed"] == _EXPECTED_TOTAL_EXPERIMENTS
        )
        _require(
            report.all_primary_completed == expected_all_completed,
            "actual_h6a_all_primary_completed_forged_or_stale",
        )
    else:
        # Aggregate status counts do not identify which terminal status was
        # the primary versus the retry. Preserve H6-A's typed primary-complete
        # flag, but never use it to score: any retry makes performance unusable.
        _require(
            not report.all_primary_completed or report.accounting_complete,
            "actual_h6a_all_primary_completed_without_complete_accounting",
        )
    expected_performance_usable = (
        report.accounting_complete
        and report.all_primary_completed
        and report.retry_attempts == 0
    )
    _require(
        report.performance_usable == expected_performance_usable,
        "actual_h6a_performance_usable_forged_or_stale",
    )
    reason_codes: list[str] = []
    if report.retry_attempts:
        reason_codes.append("h6_accounting_has_retries")
    if not report.accounting_complete:
        reason_codes.append("h6_accounting_incomplete")
    if not report.all_primary_completed:
        reason_codes.append("h6_primary_attempt_not_completed")
    return H6AAccountingSeal(
        expected_total=report.expected_total,
        registered_total=report.registered_total,
        primary_attempts=report.primary_attempts,
        status_counts=status_counts,
        retry_attempts=report.retry_attempts,
        accounting_complete=report.accounting_complete,
        performance_usable=report.performance_usable,
        trial_accounting_hash=report.trial_accounting_hash,
        reason_codes=tuple(sorted(reason_codes)),
    )


def resolve_h6a_accounting_contract(
    value: H6AAccountingInput,
) -> H6AAccountingContractResult:
    """Resolve the closed H6-A provenance domain without hash heuristics."""
    if value is None:
        return H6AAccountingContractResult("NOT_EVALUATED", None, None)
    if type(value) is H6AAccountingSeal:
        return H6AAccountingContractResult("FIXTURE_ONLY", value, None)
    if type(value) is CombinedAccountingReport:
        return H6AAccountingContractResult(
            "PASS",
            _seal_actual_h6a_report(value),
            value.campaign_run_id,
        )
    raise H5InputError("h6a_accounting_contract_input_type_unknown")


@dataclass(frozen=True, slots=True)
class EnvelopeValidationResult:
    ok: bool
    incomplete_reasons: tuple[str, ...]


def validate_envelope_and_accounting(
    envelope: CampaignEnvelope, seal: H6AAccountingSeal
) -> EnvelopeValidationResult:
    """Cross-check the envelope's H6-A hash pin against the sealed report and
    gate on H6-A's ``performance_usable`` flag. Never defaults metrics or
    scores a subset -- anything short of exact-48/complete/usable makes the
    campaign structurally ``incomplete``. Retries are preserved as explicit
    degraded evidence rather than raising before a scorecard can exist."""
    _require(
        envelope.h6a_trial_accounting_hash == seal.trial_accounting_hash,
        "envelope_h6a_hash_does_not_match_sealed_report",
    )
    if seal.performance_usable:
        return EnvelopeValidationResult(ok=True, incomplete_reasons=())
    reasons = seal.reason_codes if seal.reason_codes else ("h6_accounting_incomplete",)
    return EnvelopeValidationResult(ok=False, incomplete_reasons=tuple(sorted(reasons)))


# ---------------------------------------------------------------------------
# MetricTrade -- the one metric-trade unit (AC2): S3 = one global
# single-position round trip; S4 = one two-leg pair-basket round trip. There
# is deliberately no "leg" field anywhere on this type -- leg count can never
# be summed into a trade/PF/expectancy/win-rate denominator because no such
# field exists to sum.
# ---------------------------------------------------------------------------

_S3_DIMENSION_SET = frozenset(S3_SYMBOLS)
_S4_DIMENSION_SET = frozenset(S4_PAIRS)

_H4_CONTRACT_STATUSES = ("PASS", "FIXTURE_ONLY", "DEFERRED")
_H4_CONTRACT_PROVENANCE = ("actual", "fixture", "deferred")
MARKET_RETURN_SEMANTIC = "M_t_24h_median_log_return"
FAKE_FREE_EMPIRICAL_CLOSURE = "DEFERRED_TO_H6B_INTEGRATION_E2E"


@dataclass(frozen=True, slots=True)
class TradeAttribution:
    """Raw H4-owned attribution carried beside one H5 metric projection.

    Strategy-specific absent values are exact ``None``.  In particular S3
    has no ``entry_z`` and S4 has no volatility percentile; neither boundary
    can fill those absences with a numeric sentinel.
    """

    strategy: str
    row_id: str
    experiment_id: str
    contract_provenance: str
    market_return_semantic: str
    e13_bps: float
    e17_bps: float
    e22_bps: float
    market_return: float
    realized_holding_minutes: float
    S: float | None
    Q: float | None
    q_min: float | None
    market_return_tercile: str | None
    volatility_percentile: float | None
    entry_z: float | None
    entry_z_threshold: float | None
    D: float | None
    correlation: float | None
    half_life: float | None
    beta_stability: float | None
    realized_pair_beta: float | None

    def __post_init__(self) -> None:
        _require(self.strategy in STRATEGIES, "attribution_strategy_unknown")
        _require(
            self.row_id in config_ids_for(self.strategy),
            "attribution_row_id_unknown",
        )
        _require_hex64(self.experiment_id, "attribution_experiment_id_malformed")
        _require(
            self.contract_provenance in ("actual", "fixture"),
            "attribution_row_provenance_unknown",
        )
        _require(
            self.market_return_semantic == MARKET_RETURN_SEMANTIC,
            "attribution_market_return_semantic_drift",
        )
        for name in (
            "e13_bps",
            "e17_bps",
            "e22_bps",
            "market_return",
            "realized_holding_minutes",
        ):
            _require_exact_float(getattr(self, name), f"attribution_{name}_malformed")
        _require(
            self.realized_holding_minutes >= 0.0,
            "attribution_holding_minutes_negative",
        )
        if self.strategy == "S3":
            for name in ("S", "Q", "q_min", "volatility_percentile"):
                _require_exact_float(
                    getattr(self, name), f"attribution_s3_{name}_malformed"
                )
            _require(
                self.market_return_tercile in ("lower", "middle", "top"),
                "attribution_s3_tercile_unknown",
            )
            for name in (
                "entry_z",
                "entry_z_threshold",
                "D",
                "correlation",
                "half_life",
                "beta_stability",
                "realized_pair_beta",
            ):
                _require(
                    getattr(self, name) is None,
                    f"attribution_s3_{name}_must_be_none",
                )
        else:
            for name in (
                "entry_z",
                "entry_z_threshold",
                "D",
                "correlation",
                "half_life",
                "beta_stability",
                "realized_pair_beta",
            ):
                _require_exact_float(
                    getattr(self, name), f"attribution_s4_{name}_malformed"
                )
            for name in ("S", "Q", "q_min", "volatility_percentile"):
                _require(
                    getattr(self, name) is None,
                    f"attribution_s4_{name}_must_be_none",
                )
            _require(
                self.market_return_tercile is None,
                "attribution_s4_tercile_must_be_none",
            )


@dataclass(frozen=True, slots=True)
class MetricTrade:
    strategy: str
    config_id: str
    fold_id: str
    path_scenario: str
    dimension: str  # S3 symbol or S4 pair label -- never a per-leg split
    direction: str
    entry_ts: int
    exit_ts: int
    holding_minutes: float
    exit_reason: str
    gross_bps: float
    net_bps: float
    tp_bps: float
    sl_bps: float
    gross_notional: float | None
    market_return_4h: float | None
    volatility_percentile: float | None
    attribution: TradeAttribution | None = None

    def __post_init__(self) -> None:
        _require(self.strategy in STRATEGIES, "trade_strategy_unknown")
        _require(
            self.config_id in config_ids_for(self.strategy), "trade_config_id_unknown"
        )
        _require(self.fold_id in FOLD_IDS, "trade_fold_id_unknown")
        _require(self.path_scenario in PATH_SCENARIOS, "trade_path_scenario_unknown")

        if self.strategy == "S3":
            _require(self.dimension in _S3_DIMENSION_SET, "trade_s3_dimension_unknown")
            _require(
                self.exit_reason in S3_EXIT_REASONS, "trade_s3_exit_reason_unknown"
            )
            _require(
                self.gross_notional is None, "trade_s3_gross_notional_must_be_none"
            )
            if self.volatility_percentile is not None:
                _require_exact_float(
                    self.volatility_percentile, "trade_volatility_percentile_malformed"
                )
        else:
            _require(self.dimension in _S4_DIMENSION_SET, "trade_s4_dimension_unknown")
            _require(
                self.exit_reason in S4_EXIT_REASONS, "trade_s4_exit_reason_unknown"
            )
            _require(
                self.volatility_percentile is None,
                "trade_s4_volatility_percentile_must_be_none",
            )
            # D12 fix (adversarial verify R1, finding 2): S4's gross basket
            # notional G is REQUIRED (never None) -- a missing/None G must
            # never silently fall back to S3's equal-weight (1.0)
            # convention downstream in pBE/win-margin weighting.
            _require(
                self.gross_notional is not None, "trade_s4_gross_notional_required"
            )
            _require_exact_float(self.gross_notional, "trade_gross_notional_malformed")
            _require(self.gross_notional > 0, "trade_gross_notional_not_positive")

        _require_exact_int(self.entry_ts, "trade_entry_ts_malformed")
        _require_exact_int(self.exit_ts, "trade_exit_ts_malformed")
        _require(self.exit_ts >= self.entry_ts, "trade_exit_before_entry")
        _require_exact_float(self.holding_minutes, "trade_holding_minutes_malformed")
        _require(self.holding_minutes >= 0, "trade_holding_minutes_negative")
        _require_exact_float(self.gross_bps, "trade_gross_bps_malformed")
        _require_exact_float(self.net_bps, "trade_net_bps_malformed")
        _require_exact_float(self.tp_bps, "trade_tp_bps_malformed")
        _require_exact_float(self.sl_bps, "trade_sl_bps_malformed")
        _require(self.tp_bps > 0, "trade_tp_bps_not_positive")
        _require(self.sl_bps > 0, "trade_sl_bps_not_positive")
        if self.market_return_4h is not None:
            _require_exact_float(self.market_return_4h, "trade_market_return_malformed")
        if self.attribution is not None:
            _require(
                type(self.attribution) is TradeAttribution,
                "trade_attribution_concrete_type_mismatch",
            )
            attribution = self.attribution
            _require(
                attribution.strategy == self.strategy
                and attribution.row_id == self.config_id,
                "trade_attribution_identity_mismatch",
            )
            _require(
                self.market_return_4h is None,
                "actual_attribution_forbids_legacy_market_return_4h",
            )
            _require(
                self.holding_minutes == attribution.realized_holding_minutes,
                "trade_attribution_holding_mismatch",
            )
            selected_net = {
                "base13": attribution.e13_bps,
                "primary_stress17": attribution.e17_bps,
                "upward_stress22": attribution.e22_bps,
            }[self.path_scenario]
            _require(
                self.net_bps == selected_net,
                "trade_attribution_selected_path_economics_mismatch",
            )
            _require(
                self.volatility_percentile == attribution.volatility_percentile,
                "trade_attribution_volatility_mismatch",
            )


def authoritative_market_return(trade: MetricTrade) -> float | None:
    """Return H4-captured ``M_t`` for actual rows, legacy fixture data otherwise."""
    if type(trade) is not MetricTrade:
        raise H5InputError("market_return_trade_concrete_type_mismatch")
    if trade.attribution is not None:
        return trade.attribution.market_return
    return trade.market_return_4h


@dataclass(frozen=True, slots=True)
class H4AttributionContractResult:
    actual_h4_contract: str
    contract_provenance: str
    envelope: SelectedOOSAttributionEnvelope | None
    trades: tuple[MetricTrade, ...]
    incomplete_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(
            self.actual_h4_contract in _H4_CONTRACT_STATUSES,
            "h4_attribution_contract_status_unknown",
        )
        _require(
            self.contract_provenance in _H4_CONTRACT_PROVENANCE,
            "h4_attribution_contract_provenance_unknown",
        )
        _require(
            type(self.trades) is tuple
            and all(type(trade) is MetricTrade for trade in self.trades),
            "h4_attribution_metric_trades_malformed",
        )
        _require(
            type(self.incomplete_reasons) is tuple
            and all(
                type(reason) is str and reason for reason in self.incomplete_reasons
            ),
            "h4_attribution_incomplete_reasons_malformed",
        )
        _require(
            self.incomplete_reasons == tuple(sorted(set(self.incomplete_reasons))),
            "h4_attribution_incomplete_reasons_not_canonical",
        )
        if self.actual_h4_contract == "PASS":
            _require(
                self.contract_provenance == "actual"
                and type(self.envelope) is SelectedOOSAttributionEnvelope
                and self.envelope.contract_provenance == "actual"
                and not self.incomplete_reasons,
                "h4_attribution_pass_without_actual_typed_evidence",
            )
            _require(
                all(
                    trade.attribution is not None
                    and trade.attribution.contract_provenance == "actual"
                    for trade in self.trades
                ),
                "h4_attribution_pass_contains_nonactual_row",
            )
        elif self.actual_h4_contract == "FIXTURE_ONLY":
            _require(
                self.contract_provenance == "fixture"
                and self.envelope is None
                and not self.trades
                and bool(self.incomplete_reasons),
                "h4_attribution_fixture_status_mismatch",
            )
        else:
            _require(
                self.contract_provenance == "deferred"
                and type(self.envelope) is SelectedOOSAttributionEnvelope
                and self.envelope.contract_provenance == "deferred"
                and not self.trades
                and bool(self.incomplete_reasons),
                "h4_attribution_deferred_status_mismatch",
            )


def _attribution_from_h4_row(
    row: S3SelectedOOSAttribution | S4SelectedOOSAttribution,
) -> TradeAttribution:
    common: dict[str, Any] = {
        "strategy": "S3" if type(row) is S3SelectedOOSAttribution else "S4",
        "row_id": row.lineage.row_id,
        "experiment_id": row.lineage.experiment_id,
        "contract_provenance": "actual",
        "market_return_semantic": MARKET_RETURN_SEMANTIC,
        "e13_bps": row.scenario_row.e13_bps,
        "e17_bps": row.scenario_row.e17_bps,
        "e22_bps": row.scenario_row.e22_bps,
        "market_return": row.market_return,
        "realized_holding_minutes": row.realized_holding_minutes,
    }
    if type(row) is S3SelectedOOSAttribution:
        return TradeAttribution(
            **common,
            S=row.S,
            Q=row.Q,
            q_min=row.q_min,
            market_return_tercile=row.market_return_tercile,
            volatility_percentile=row.volatility_percentile,
            entry_z=None,
            entry_z_threshold=None,
            D=None,
            correlation=None,
            half_life=None,
            beta_stability=None,
            realized_pair_beta=None,
        )
    return TradeAttribution(
        **common,
        S=None,
        Q=None,
        q_min=None,
        market_return_tercile=None,
        volatility_percentile=None,
        entry_z=row.entry_z,
        entry_z_threshold=row.entry_z_threshold,
        D=row.D,
        correlation=row.correlation,
        half_life=row.half_life,
        beta_stability=row.beta_stability,
        realized_pair_beta=row.realized_pair_beta,
    )


def _metric_trade_from_h4_row(
    row: S3SelectedOOSAttribution | S4SelectedOOSAttribution,
) -> MetricTrade:
    attribution = _attribution_from_h4_row(row)
    trade = row.scenario_row.trade
    net_bps = {
        "base13": attribution.e13_bps,
        "primary_stress17": attribution.e17_bps,
        "upward_stress22": attribution.e22_bps,
    }[row.scenario_row.path_scenario]
    if type(row) is S3SelectedOOSAttribution:
        dimension = trade.symbol
        direction = trade.side
        gross_notional = None
        volatility_percentile = row.volatility_percentile
    else:
        dimension = row.candidate.pair
        direction = row.candidate.side
        gross_notional = trade.gross_notional
        volatility_percentile = None
    return MetricTrade(
        strategy=attribution.strategy,
        config_id=attribution.row_id,
        fold_id=row.lineage.fold_id,
        path_scenario=row.scenario_row.path_scenario,
        dimension=dimension,
        direction=direction,
        entry_ts=trade.entry_ts,
        exit_ts=trade.exit_ts,
        holding_minutes=row.realized_holding_minutes,
        exit_reason=trade.exit_reason,
        gross_bps=trade.gross_bps,
        net_bps=net_bps,
        tp_bps=row.tp_bps,
        sl_bps=row.sl_bps,
        gross_notional=gross_notional,
        market_return_4h=None,
        volatility_percentile=volatility_percentile,
        attribution=attribution,
    )


def consume_h4_attribution(envelope: object) -> H4AttributionContractResult:
    """Validate the concrete H4 seam and project it without recomputation."""
    try:
        exact = validate_attribution_envelope(envelope)
    except (H4AttributionError, TypeError, ValueError) as exc:
        raise H5InputError("actual_h4_attribution_contract_invalid") from exc
    if exact.contract_provenance == "deferred":
        return H4AttributionContractResult(
            actual_h4_contract="DEFERRED",
            contract_provenance="deferred",
            envelope=exact,
            trades=(),
            incomplete_reasons=(exact.deferred_reason or "h4_attribution_deferred",),
        )
    if exact.contract_provenance == "fixture":
        return fixture_h4_attribution_result()
    trades = tuple(_metric_trade_from_h4_row(row) for row in exact.rows)
    return H4AttributionContractResult(
        actual_h4_contract="PASS",
        contract_provenance="actual",
        envelope=exact,
        trades=trades,
        incomplete_reasons=(),
    )


def fixture_h4_attribution_result() -> H4AttributionContractResult:
    """Explicit legacy/shape-test sentinel; it can never become PASS."""
    return H4AttributionContractResult(
        actual_h4_contract="FIXTURE_ONLY",
        contract_provenance="fixture",
        envelope=None,
        trades=(),
        incomplete_reasons=("fixture_h4_attribution_not_actual",),
    )
