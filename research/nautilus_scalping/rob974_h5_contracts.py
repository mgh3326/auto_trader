"""ROB-983 (H5, CP1) -- exact input domain, envelope verification, and the
H6-A exact-48 accounting gate.

H5 is a pure consumer: it never opens a DB session, never reimplements H6-A's
trial-accounting reconstruction, and never scores a subset when H6-A's seal
is anything less than the full, exact-48, retry-0, ``performance_usable``
report. Any AC34 provenance violation (missing/future/lookahead bar,
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
    "MetricTrade",
    "classify_provenance_violation",
    "config_ids_for",
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
        _require(status_sum <= _EXPECTED_TOTAL_EXPERIMENTS, "h6a_status_sum_exceeds_48")
        # A NORMAL, accounting-complete primary run always tallies exactly
        # 48 across the four closed statuses (a hidden/missing row can never
        # sit "under" a status_sum of 48 while claiming completeness). A
        # genuinely incomplete run (e.g. one config never registered/
        # attempted at all) may report fewer than 48 -- that gap is exactly
        # what makes it incomplete.
        if self.accounting_complete:
            _require(status_sum == _EXPECTED_TOTAL_EXPERIMENTS, "h6a_status_sum_not_48")
        _require(
            self.registered_total <= _EXPECTED_TOTAL_EXPERIMENTS
            or not self.accounting_complete,
            "h6a_registered_total_exceeds_48_but_claims_complete",
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


@dataclass(frozen=True, slots=True)
class EnvelopeValidationResult:
    ok: bool
    incomplete_reasons: tuple[str, ...]


def validate_envelope_and_accounting(
    envelope: CampaignEnvelope, seal: H6AAccountingSeal
) -> EnvelopeValidationResult:
    """Cross-check the envelope's H6-A hash pin against the sealed report and
    gate on H6-A's ``performance_usable`` flag. Never defaults metrics or
    scores a subset -- anything short of exact-48/retry-0/complete/usable
    makes the campaign structurally ``incomplete``."""
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
            if self.gross_notional is not None:
                _require_exact_float(
                    self.gross_notional, "trade_gross_notional_malformed"
                )
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
