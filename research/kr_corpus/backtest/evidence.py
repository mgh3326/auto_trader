"""Stage-B trial evidence writer using the repository's canonical contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_contracts.trial_evidence import build_trial_evidence

try:
    from .stage_b import StageBResult, descriptive_trial_statistics
except ImportError:  # pragma: no cover - legacy flat-module test entrypoint
    from stage_b import StageBResult, descriptive_trial_statistics

__all__ = ["write_stage_b_evidence"]


def write_stage_b_evidence(path: Path, result: StageBResult) -> dict[str, Any]:
    """Write re-judgable evidence; acceptance labels stay on their own track."""
    stats = descriptive_trial_statistics(result)
    trial_evidence = build_trial_evidence(
        parameter_key="rev3_reclaim",
        config_hash=result.contract.config_hash,
        execution_cost={
            "fee_bps": float(result.contract.cost.fee_bp),
            "transaction_tax_bps": float(result.contract.cost.transaction_tax_bp),
            "half_spread_bps": 0.0,
            "slippage_bps": float(2 * result.contract.cost.slippage_bp_per_side),
        },
        sharpe=float(stats["sharpe"]),
        p_value=float(stats["p_value"]),
        sample_size=int(stats["sample_size"]),
        validation_score=float(stats["validation_score"]),
        sharpe_method="pooled_sample_sharpe",
        p_value_method="not_computed",
        selection_score_method="arithmetic_mean_net_return",
    )
    payload: dict[str, Any] = {
        "artifact_kind": "KR_STAGE_B_TRIAL_EVIDENCE",
        "track": "strategy_backtest",
        "strategy": "rev3_reclaim",
        "strategy_status": "UNTESTED_RESEARCH_SHADOW",
        "acceptance_track_separate": True,
        "signal_contract_hash": result.contract.signal_contract_hash,
        "run_contract": result.to_dict(),
        "trial_evidence": trial_evidence,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload
