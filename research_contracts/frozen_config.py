"""ROB-351 (eng-review ex-ante enforcement) — frozen campaign config (pure, stdlib).

The ex-ante gate thresholds and the achievable-execution envelope are committed
HERE in PR1, before any PR2 OOS read exists. Splitting the commit (PR1) from the
run (PR2) makes "ex-ante" structurally enforceable: the run records
``config_hash()`` and any later tweak changes the hash, so an ex-post adjustment
to admit a near-miss candidate is detectable, not a promise.

Envelope numbers are the real Binance USDⓈ-M Futures Demo schedule (maker 2.0 /
taker 4.0 bps); the fill model is the queue-loss/adverse-selection scenario in
``maker_fill.py`` (not a hand-picked constant — eng-review Issue 3 / fill defensibility).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from .canonical_hash import canonical_sha256
from .evaluation_windows import CANONICAL_EVALUATION_WINDOWS, EvaluationWindows
from .trial_evidence import (
    PRODUCER,
    PRODUCER_VERSION,
    SELECTION_SCORE_METHOD,
)
from .trial_evidence import (
    SCHEMA_VERSION as TRIAL_EVIDENCE_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class CampaignConfig:
    evaluation_windows: EvaluationWindows = CANONICAL_EVALUATION_WINDOWS
    # significance / multiple-testing
    target_t: float = 2.0
    fdr_alpha: float = 0.05
    block_size: int = 10
    dsr_probability_threshold: float = 0.95
    dsr_min_observations: int = 30
    trial_sharpe_method: str = "mean_cv_fold_sharpe"
    trial_p_value_method: str = "one_sided_normal_cv_fold_sharpe"
    selection_score_method: str = SELECTION_SCORE_METHOD
    trial_runner: str = PRODUCER
    trial_timeframe: str = "1d"
    trial_evidence_schema_version: str = TRIAL_EVIDENCE_SCHEMA_VERSION
    trial_evidence_producer: str = PRODUCER
    trial_evidence_producer_version: str = PRODUCER_VERSION
    trial_min_folds: int = 2
    pbo_slices: int = 4
    pbo_max: float = 0.5
    # economic-triviality floor (Codex: sign>0 is too low)
    economic_triviality_floor_bps: float = 0.5
    # achievable-execution envelope (Binance USDⓈ-M demo)
    achievable_maker_bps: float = 2.0
    taker_bps: float = 4.0
    # PIT seasoning (raw units; see pit_universe)
    min_seasoning: int = 0
    # analytic taker fee grid
    fee_grid_bps: tuple[float, ...] = (10.0, 7.5, 5.0, 2.0, 0.0)
    baseline_names: tuple[str, ...] = (
        "cash",
        "btc_eth_equal_weight",
        "same_turnover_random",
    )
    random_baseline_seed: int = 847
    random_baseline_repetitions: int = 100
    half_spread_bps: float = 0.0
    slippage_bps: float = 2.0
    cost_stress_multipliers: tuple[float, ...] = (1.0, 1.5, 2.0)
    mdd_target_pct: float = 20.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fee_grid_bps"] = list(self.fee_grid_bps)
        d["baseline_names"] = list(self.baseline_names)
        d["cost_stress_multipliers"] = list(self.cost_stress_multipliers)
        d["evaluation_windows"] = self.evaluation_windows.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CampaignConfig:
        allowed = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in allowed}
        for name in ("fee_grid_bps", "baseline_names", "cost_stress_multipliers"):
            if name in kw:
                kw[name] = tuple(kw[name])
        if "evaluation_windows" in kw:
            kw["evaluation_windows"] = EvaluationWindows.from_dict(
                kw["evaluation_windows"]
            )
        return cls(**kw)

    def config_hash(self) -> str:
        """ROB-846 typed-canonical SHA-256 over the complete frozen config."""
        return canonical_sha256(self.to_dict())

    def benchmark_identity(self) -> dict:
        """Exact ROB-846 benchmark component required for this campaign."""
        return {
            "names": list(self.baseline_names),
            "same_turnover_random": {
                "seed": self.random_baseline_seed,
                "repetitions": self.random_baseline_repetitions,
            },
        }

    def cost_identity(self) -> dict:
        """Exact ROB-846 cost component required for this campaign."""
        return {
            "taker_bps": self.taker_bps,
            "half_spread_bps": self.half_spread_bps,
            "slippage_bps": self.slippage_bps,
            "stress_multipliers": list(self.cost_stress_multipliers),
        }

    def policy_identity(self) -> dict:
        """Exact ROB-846 statistical, selection, and gate-policy component."""
        return {
            "schema_version": "honest_offline_gate.v1",
            "evaluation_windows": self.evaluation_windows.to_dict(),
            "selection": {
                "evidence": "validation_only",
                "score_method": self.selection_score_method,
                "tie_break": "parameter_key_ascending",
                "ties": "non_promotable",
                "sealed_oos": "finalize_only",
            },
            "trial_statistics": {
                "runner": self.trial_runner,
                "timeframe": self.trial_timeframe,
                "evidence_schema_version": self.trial_evidence_schema_version,
                "producer": self.trial_evidence_producer,
                "producer_version": self.trial_evidence_producer_version,
                "sharpe_method": self.trial_sharpe_method,
                "p_value_method": self.trial_p_value_method,
                "min_folds": self.trial_min_folds,
            },
            "dsr": {
                "probability_threshold": self.dsr_probability_threshold,
                "min_observations": self.dsr_min_observations,
            },
            "pbo": {"slices": self.pbo_slices, "maximum": self.pbo_max},
            "fdr": {"alpha": self.fdr_alpha},
            "economic_edge": {
                "minimum_bps": self.economic_triviality_floor_bps,
            },
            "pit": {
                "manifest_required": True,
                "information_cutoff_required": True,
                "campaign_cutoff_match": "exact",
            },
            "finalization": {
                "one_time_per_run": True,
                "invalid_evidence": "non_promotable",
            },
        }

    def mdd_identity(self) -> dict:
        """Exact ROB-846 maximum-drawdown component required for this campaign."""
        return {"target_pct": self.mdd_target_pct}


# The frozen default committed in PR1. PR2 reads THIS; it must not be edited to
# admit a result after an OOS read (the hash would change — that is the guard).
FROZEN_CONFIG = CampaignConfig()
