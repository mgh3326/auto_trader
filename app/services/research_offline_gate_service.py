"""ROB-847 registry-backed one-time sealed-OOS finalization.

This adapter owns no experiment tables and writes no promotion row directly.
It reads complete trial accounting from ROB-846, builds the honest gate artifact,
and delegates the exact identity link to ``link_promotion_candidate``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchPromotionCandidate,
)
from app.schemas.research_backtest import PromotionLinkRequest
from app.services.strategy_experiment_registry import (
    PromotionHashMismatch,
    get_trial_accounting,
    link_promotion_candidate,
    list_trials,
)
from research.nautilus_scalping.honest_offline_gate import (
    HonestGateConfig,
    PITEvidence,
    SealedOOS,
    SelectionResult,
    benjamini_hochberg,
    build_gate_artifact,
    deflated_sharpe_ratio,
    probability_backtest_overfitting,
)


class OfflineGateFinalizeError(Exception):
    """Stable fail-closed finalize error."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _completed_trial_sharpes(
    trials: Sequence[ResearchBacktestRun],
) -> tuple[float, ...]:
    sharpes: list[float] = []
    for trial in trials:
        payload = trial.raw_payload or {}
        if trial.trial_status == "completed" and "sharpe" in payload:
            sharpes.append(float(payload["sharpe"]))
    return tuple(sharpes)


async def finalize_offline_gate(
    session: AsyncSession,
    *,
    backtest_run_id: int,
    experiment_id: str,
    expected_config_hash: str,
    expected_data_hash: str,
    selection: SelectionResult,
    sealed_oos: SealedOOS,
    pit_evidence: PITEvidence,
    pbo_candidate_returns: Mapping[str, Sequence[float]],
    p_values: Mapping[str, float],
    candidate_p_value_key: str,
    economic_edge_bps: float,
    fold_metrics: Sequence[Mapping[str, Any]],
    baselines: Mapping[str, float],
    cost_stress: Mapping[str, float],
    observed_mdd_pct: float,
    config: HonestGateConfig,
) -> ResearchPromotionCandidate:
    """Evaluate sealed OOS once and create an exact ROB-846 promotion link."""
    existing = await session.scalar(
        select(ResearchPromotionCandidate).where(
            ResearchPromotionCandidate.backtest_run_id == backtest_run_id
        )
    )
    if existing is not None:
        raise OfflineGateFinalizeError("sealed_oos_already_finalized")

    run = await session.get(ResearchBacktestRun, backtest_run_id)
    if run is None:
        raise OfflineGateFinalizeError("promotion_hash_mismatch")
    if run.strategy_experiment_id is None:
        raise OfflineGateFinalizeError("missing_experiment_identity")
    if expected_config_hash != config.config_hash():
        raise OfflineGateFinalizeError("frozen_config_hash_mismatch")

    accounting = await get_trial_accounting(session, experiment_id)
    trials = await list_trials(session, experiment_id)
    dsr = deflated_sharpe_ratio(
        sealed_oos.returns,
        _completed_trial_sharpes(trials),
        total_trials=accounting.total_trials,
        min_observations=config.dsr_min_observations,
    )
    pbo = probability_backtest_overfitting(
        pbo_candidate_returns, slices=config.pbo_slices
    )
    fdr = benjamini_hochberg(p_values, alpha=config.fdr_alpha)
    artifact = build_gate_artifact(
        experiment_id=experiment_id,
        run_id=run.run_id,
        config_hash=expected_config_hash,
        data_hash=expected_data_hash,
        selection=selection,
        sealed_oos=sealed_oos,
        pit_evidence=pit_evidence,
        accounting=accounting.model_dump(),
        dsr=dsr,
        pbo=pbo,
        fdr=fdr,
        candidate_p_value_key=candidate_p_value_key,
        economic_edge_bps=economic_edge_bps,
        fold_metrics=fold_metrics,
        baselines=baselines,
        cost_stress=cost_stress,
        observed_mdd_pct=observed_mdd_pct,
        config=config,
    )
    request = PromotionLinkRequest(
        expected_experiment_id=experiment_id,
        expected_config_hash=expected_config_hash,
        expected_data_hash=expected_data_hash,
        status="eligible" if artifact.promotable else "non_promotable",
        reason_code=artifact.primary_reason,
        thresholds=artifact.thresholds,
        metrics=artifact.to_metrics(),
    )
    try:
        async with session.begin_nested():
            return await link_promotion_candidate(
                session,
                backtest_run_id=backtest_run_id,
                request=request,
            )
    except PromotionHashMismatch as exc:
        raise OfflineGateFinalizeError("promotion_hash_mismatch") from exc
    except IntegrityError as exc:
        if "promotion_candidates_run_id" in str(exc):
            raise OfflineGateFinalizeError("sealed_oos_already_finalized") from exc
        raise
