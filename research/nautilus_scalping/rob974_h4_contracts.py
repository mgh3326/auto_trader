"""ROB-982 H4 immutable, pure walk-forward contract primitives.

This module deliberately contains only values that are part of the H4
semantic identity.  It neither discovers source files nor loads a corpus.
"""

from __future__ import annotations

from dataclasses import dataclass

from rob944_folds import Fold, generate_frozen_fold_schedule

WINDOW_START_MS = 1_751_328_000_000  # 2025-07-01T00:00:00Z
WINDOW_END_MS = 1_782_864_000_000  # 2026-07-01T00:00:00Z

FOLD_COUNT = 8
SCENARIOS: tuple[str, ...] = ("base13", "primary_stress17", "upward_stress22")
PBO_SCENARIO = "primary_stress17"
PBO_SLICES = 4
PBO_DAYS = 365
STRATEGIES: tuple[str, ...] = ("S3", "S4")

ATTRIBUTION_SCHEMA_VERSION = "rob974.h4.selected_oos_attribution.v1"
CONTRACT_PROVENANCE: tuple[str, ...] = ("actual", "fixture", "deferred")
MARKET_RETURN_SEMANTIC = "M_t_24h_median_log_return"
TERCILE_METHOD = "fold_train_all_complete_M_midrank_v1"
TERCILE_BINS: tuple[str, ...] = ("lower", "middle", "top")


def _sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be built-in str")
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    if value == "0" * 64:
        raise ValueError(f"{name} must not be a zero placeholder")
    return value


@dataclass(frozen=True, slots=True)
class H4SourcePins:
    """Trusted source-audit inputs; CP9 recomputes these from raw final bytes."""

    runner_bundle_sha256: str
    pbo_source_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.runner_bundle_sha256, "runner_bundle_sha256")
        _sha256(self.pbo_source_sha256, "pbo_source_sha256")
        if self.runner_bundle_sha256 == self.pbo_source_sha256:
            raise ValueError("runner and PBO source pins must be distinct")


def exact_h4_folds() -> tuple[Fold, ...]:
    """Return the registered eight complete folds and reject schedule drift."""
    folds = generate_frozen_fold_schedule(WINDOW_START_MS, WINDOW_END_MS)
    if len(folds) != FOLD_COUNT:
        raise ValueError("ROB-974 requires exactly eight complete folds")
    if tuple(fold.fold_id for fold in folds) != tuple(
        f"fold-{i:02d}" for i in range(8)
    ):
        raise ValueError("registered fold identifiers drifted")
    return folds


def validate_exact_config_ids(strategy: object, config_ids: object) -> tuple[str, ...]:
    if type(strategy) is not str or strategy not in STRATEGIES:
        raise ValueError("strategy must be S3 or S4")
    if type(config_ids) is not tuple:
        raise TypeError("config_ids must be a built-in tuple")
    expected = tuple(f"{strategy}-{index:02d}" for index in range(24))
    if config_ids != expected:
        raise ValueError("config IDs must be the exact ordered 24-row roster")
    return config_ids


def scorecard_contract() -> dict[str, object]:
    """Literal AC3 scorecard semantics, committed by the H4 payload only."""
    return {
        "pBE": "(SL_bps+17)/(TP_bps+SL_bps)",
        "pBE_weighting": {"S3": "equal_weight", "S4": "basket_G_weighted"},
        "win_margin": "observed_win_rate-weighted_pBE",
        "common": {
            "E0_min_bps": 25.0,
            "win_margin_min_fraction": 0.03,
            "E17_min_bps": 5.0,
            "PF_min": 1.15,
            "positive_folds_min": 5,
            "fold_count": 8,
            "concentration_max": 0.50,
            "E22_strictly_positive": True,
            "every_fold_trades_min": 5,
        },
        "S3": {
            "pooled_timeout_max": 0.15,
            "fold_timeout_max": 0.25,
            "bullish_long_E22_strictly_positive": True,
            "first_4h_sl_absolute_loss_share_max": 0.50,
            "symbol_dependence_fail": "exactly_one_positive_symbol_and_other_two_pooled_E17_lte_0",
        },
        "S4": {
            "pooled_timeout_max": 0.20,
            "fold_timeout_max": 0.30,
            "bullish_bin_E22_strictly_positive": True,
            "abs_market_correlation_max": 0.15,
            "pair_dependence_fail": "positive_pair_concentration_gt_0.70_and_other_two_pooled_E17_lte_0",
            "slow_only_fail": "half_life_8_32h_E17_lte_0_and_half_life_32_48h_E17_gt_0",
        },
        "attribution": attribution_contract(),
    }


def attribution_contract() -> dict[str, object]:
    """Return the orch-ratified H4.5 raw-attribution semantics.

    These values are part of both the campaign policy and every row's policy
    identity.  They intentionally describe only existing H2/H3 authorities;
    no downstream fallback, zero fill, or post-hold regression is permitted.
    """
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "contract_provenance": list(CONTRACT_PROVENANCE),
        "market_return": {
            "semantic": MARKET_RETURN_SEMANTIC,
            "source": "CommonSnapshot.M_at_signal_decision_ts",
            "m_t_allowed": False,
        },
        "tercile": {
            "method": TERCILE_METHOD,
            "reference": "same_fold_train_all_complete_CommonSnapshot.M",
            "rank": "(count(v<x)+0.5*count(v==x))/N",
            "bins": {
                "lower": "p<1/3",
                "middle": "1/3<=p<2/3",
                "top": "p>=2/3",
            },
            "ties_split": False,
            "empty_or_nonfinite": "incomplete",
        },
        "realized_holding_minutes": {
            "formula": "(exit_ts-entry_ts)/60000.0",
            "materialization_owner": "H4",
            "requirements": ["non_negative", "duration_aligned_to_60000ms"],
        },
        "S3": {
            "entry_z": "absent_not_defined",
            "fields": [
                "S",
                "Q",
                "market_return",
                "market_return_tercile",
                "volatility_percentile",
                "e13_bps",
                "e17_bps",
                "e22_bps",
                "realized_holding_minutes",
                "row_id",
                "experiment_id",
            ],
        },
        "S4": {
            "entry_z": "S4Candidate.observed_z",
            "D": "S4Candidate.D_bps",
            "correlation": "S4Candidate.rho",
            "half_life": "S4Candidate.half_life_4h_bars",
            "beta_stability": "S4Candidate.beta_stability",
            "realized_pair_beta": (
                "sign(side_a)*weight_a*beta_a+sign(side_b)*weight_b*beta_b"
            ),
            "realized_pair_beta_timing": "entry_frozen_actual_H2_trade",
            "post_hold_ols": "forbidden",
            "fields": [
                "entry_z",
                "D",
                "correlation",
                "half_life",
                "beta_stability",
                "realized_pair_beta",
                "market_return",
                "e13_bps",
                "e17_bps",
                "e22_bps",
                "realized_holding_minutes",
                "row_id",
                "experiment_id",
            ],
        },
    }


def campaign_verdict_contract() -> dict[str, object]:
    return {
        "strategy_verdicts": ("historical_pass", "historical_fail", "incomplete"),
        "campaign_order": (
            "incomplete_first",
            "both_historical_fail_no_candidate",
            "s3_only_s3_demo_candidate",
            "s4_only_historical_preferred_no_demo",
            "both_pass_s3_demo_candidate_s4_comparison_report_only",
        ),
        "s4_historical_posture": {
            "volatility_percentile": None,
            "volatility_percentile_provenance": "not_defined_for_s4",
            "executor": "not_evaluated",
            "order_ids": None,
            "residual": None,
            "PAIR_EXEC_FAIL": None,
            "historical_screen_only": True,
            "demo_eligible": False,
        },
    }


__all__ = [
    "ATTRIBUTION_SCHEMA_VERSION",
    "CONTRACT_PROVENANCE",
    "FOLD_COUNT",
    "H4SourcePins",
    "MARKET_RETURN_SEMANTIC",
    "PBO_DAYS",
    "PBO_SCENARIO",
    "PBO_SLICES",
    "SCENARIOS",
    "STRATEGIES",
    "TERCILE_BINS",
    "TERCILE_METHOD",
    "WINDOW_END_MS",
    "WINDOW_START_MS",
    "exact_h4_folds",
    "attribution_contract",
    "campaign_verdict_contract",
    "scorecard_contract",
    "validate_exact_config_ids",
]
