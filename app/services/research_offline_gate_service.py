"""Registry-backed, one-time sealed-OOS finalization for ROB-847.

The campaign universe is derived from immutable ROB-846 experiment rows, never
from caller-supplied p-values or trial counts.  In the absence of a dedicated
campaign id, comparable experiments share a strategy key and every fixed
environment hash; strategy/code/params are deliberately excluded so candidate
variants remain in the same conservative multiple-testing universe.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import (
    TRIAL_STATUSES,
    ResearchBacktestRun,
    ResearchPromotionCandidate,
    ResearchStrategyExperiment,
)
from app.schemas.research_backtest import BacktestTrialRequest, PromotionLinkRequest
from app.services.research_canonical_hash import canonical_sha256
from app.services.strategy_experiment_registry import (
    PromotionHashMismatch,
    link_promotion_candidate,
    record_trial,
)
from research_contracts.honest_offline_gate import (
    SEALED_OOS_ARTIFACT_PATH,
    SEALED_OOS_RUNNER,
    SEALED_OOS_TIMEFRAME,
    FDRResult,
    HonestGateConfig,
    PITEvidence,
    SealedOOS,
    SealedOOSArtifactError,
    SelectionCandidate,
    SelectionResult,
    StatisticResult,
    benjamini_hochberg,
    build_gate_artifact,
    build_sealed_oos_payload,
    deflated_sharpe_ratio,
    parse_sealed_oos_payload,
    probability_backtest_overfitting,
    select_parameters,
)
from research_contracts.trial_evidence import (
    TrialEvidence,
    TrialEvidenceError,
    parse_trial_evidence,
)


class OfflineGateFinalizeError(Exception):
    """Stable fail-closed finalize error for identity/sealing failures."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CampaignExperiment:
    id: int
    experiment_id: str
    strategy_key: str
    params_hash: str
    frozen_config_hash: str
    dataset_manifest_hash: str
    universe_hash: str
    pit_hash: str
    policy_hash: str
    benchmark_hash: str
    cost_hash: str
    mdd_hash: str

    @classmethod
    def from_orm(cls, row: Any) -> CampaignExperiment:
        return cls(**{name: getattr(row, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class CampaignTrial:
    experiment: CampaignExperiment
    run: ResearchBacktestRun | Any | None


@dataclass(frozen=True)
class CandidateTrialEvidence:
    experiment: CampaignExperiment
    run: ResearchBacktestRun | Any
    evidence: TrialEvidence


def registry_config_hash(config: HonestGateConfig) -> str:
    """Compatibility alias; CampaignConfig.config_hash is the sole authority."""
    return config.config_hash()


def _is_sealed_oos_artifact_run(run: Any) -> bool:
    return (
        getattr(run, "runner", None) == SEALED_OOS_RUNNER
        and getattr(run, "timeframe", None) == SEALED_OOS_TIMEFRAME
        and getattr(run, "artifact_path", None) == SEALED_OOS_ARTIFACT_PATH
        and getattr(run, "trial_status", None) == "completed"
    )


def _campaign_run_is_not_sealed_artifact() -> Any:
    """SQL NULL-safe inverse of the exact dedicated artifact metadata tuple."""
    run = ResearchBacktestRun
    return or_(
        run.runner.is_(None),
        run.runner != SEALED_OOS_RUNNER,
        run.timeframe.is_(None),
        run.timeframe != SEALED_OOS_TIMEFRAME,
        run.artifact_path.is_(None),
        run.artifact_path != SEALED_OOS_ARTIFACT_PATH,
        run.trial_status.is_(None),
        run.trial_status != "completed",
    )


async def record_sealed_oos_artifact(
    session: AsyncSession,
    *,
    experiment_id: str,
    sealed_oos: SealedOOS,
    config: HonestGateConfig,
    idempotency_key: str,
) -> ResearchBacktestRun:
    """Persist trusted sealed OOS behind an opaque, append-only registry id.

    This is an internal producer boundary, not an externally authorized write
    API.  Production promotion remains disabled until the approved sealed-OOS
    producer wires this function with its own mutation authorization.
    """
    experiment_row = await session.scalar(
        select(ResearchStrategyExperiment).where(
            ResearchStrategyExperiment.experiment_id == experiment_id
        )
    )
    if experiment_row is None:
        raise OfflineGateFinalizeError("promotion_hash_mismatch")
    experiment = CampaignExperiment.from_orm(experiment_row)
    _validate_target_identity(
        experiment,
        supplied_experiment_id=experiment_id,
        config=config,
    )
    try:
        payload = build_sealed_oos_payload(
            experiment_id=experiment.experiment_id,
            config_hash=experiment.frozen_config_hash,
            data_hash=experiment.dataset_manifest_hash,
            window=config.evaluation_windows.sealed_oos.to_dict(),
            sealed_oos=sealed_oos,
        )
    except SealedOOSArtifactError as exc:
        raise OfflineGateFinalizeError(str(exc)) from exc
    artifact_hash = canonical_sha256(payload)
    row = await record_trial(
        session,
        experiment_id=experiment.experiment_id,
        request=BacktestTrialRequest(
            status="completed",
            strategy_name=experiment.strategy_key,
            timeframe=SEALED_OOS_TIMEFRAME,
            runner=SEALED_OOS_RUNNER,
            idempotency_key=idempotency_key,
            artifact_path=SEALED_OOS_ARTIFACT_PATH,
            artifact_hash=artifact_hash,
            raw_payload=payload,
        ),
    )
    if (
        row.strategy_experiment_id != experiment.id
        or row.runner != SEALED_OOS_RUNNER
        or row.timeframe != SEALED_OOS_TIMEFRAME
        or row.trial_status != "completed"
        or row.artifact_path != SEALED_OOS_ARTIFACT_PATH
        or row.artifact_hash != artifact_hash
        or row.raw_payload != payload
    ):
        raise OfflineGateFinalizeError("sealed_oos_artifact_conflict")
    return row


async def _load_sealed_oos_artifact(
    session: AsyncSession,
    *,
    artifact_id: int,
    target: CampaignExperiment,
    config: HonestGateConfig,
) -> SealedOOS:
    if type(artifact_id) is not int or artifact_id <= 0:
        raise OfflineGateFinalizeError("invalid_sealed_oos_artifact")
    row = await session.get(ResearchBacktestRun, artifact_id)
    if row is None:
        raise OfflineGateFinalizeError("invalid_sealed_oos_artifact")
    if (
        row.strategy_experiment_id != target.id
        or row.runner != SEALED_OOS_RUNNER
        or row.timeframe != SEALED_OOS_TIMEFRAME
        or row.trial_status != "completed"
        or row.artifact_path != SEALED_OOS_ARTIFACT_PATH
        or not isinstance(row.artifact_hash, str)
        or not isinstance(row.raw_payload, dict)
        or canonical_sha256(row.raw_payload) != row.artifact_hash
    ):
        raise OfflineGateFinalizeError("invalid_sealed_oos_artifact")
    try:
        return parse_sealed_oos_payload(
            row.raw_payload,
            experiment_id=target.experiment_id,
            config_hash=target.frozen_config_hash,
            data_hash=target.dataset_manifest_hash,
            window=config.evaluation_windows.sealed_oos.to_dict(),
        )
    except SealedOOSArtifactError as exc:
        raise OfflineGateFinalizeError(str(exc)) from exc


async def _claim_sealed_oos_artifact(
    session: AsyncSession,
    *,
    artifact_id: int,
    backtest_run_id: int,
) -> None:
    """Serialize one-time consumption without adding mutable schema state."""
    if type(artifact_id) is not int or artifact_id <= 0:
        raise OfflineGateFinalizeError("invalid_sealed_oos_artifact")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(-CAST(:artifact_id AS bigint))"),
        {"artifact_id": artifact_id},
    )
    used_by = await session.scalar(
        select(ResearchPromotionCandidate.backtest_run_id).where(
            ResearchPromotionCandidate.metrics["sealed_oos_artifact_id"].as_integer()
            == artifact_id
        )
    )
    if used_by is not None:
        if used_by == backtest_run_id:
            raise OfflineGateFinalizeError("sealed_oos_already_finalized")
        raise OfflineGateFinalizeError("sealed_oos_artifact_already_used")


def _fixed_campaign_filters(
    target: CampaignExperiment,
) -> tuple[Any, ...]:
    model = ResearchStrategyExperiment
    return (
        model.strategy_key == target.strategy_key,
        model.frozen_config_hash == target.frozen_config_hash,
        model.dataset_manifest_hash == target.dataset_manifest_hash,
        model.universe_hash == target.universe_hash,
        model.pit_hash == target.pit_hash,
        model.policy_hash == target.policy_hash,
        model.benchmark_hash == target.benchmark_hash,
        model.cost_hash == target.cost_hash,
        model.mdd_hash == target.mdd_hash,
    )


async def _load_campaign_trials(
    session: AsyncSession,
    target: CampaignExperiment,
) -> list[CampaignTrial]:
    """Load every candidate experiment and terminal row in the fixed campaign."""
    result = await session.execute(
        select(ResearchStrategyExperiment, ResearchBacktestRun)
        .outerjoin(
            ResearchBacktestRun,
            and_(
                ResearchBacktestRun.strategy_experiment_id
                == ResearchStrategyExperiment.id,
                _campaign_run_is_not_sealed_artifact(),
            ),
        )
        .where(*_fixed_campaign_filters(target))
        .order_by(
            ResearchStrategyExperiment.experiment_id,
            ResearchBacktestRun.trial_index,
        )
    )
    return [
        CampaignTrial(CampaignExperiment.from_orm(experiment), run)
        for experiment, run in result.all()
    ]


def _campaign_accounting(trials: Sequence[CampaignTrial]) -> dict[str, Any]:
    counts = Counter(
        trial.run.trial_status
        for trial in trials
        if trial.run is not None
        and not _is_sealed_oos_artifact_run(trial.run)
        and trial.run.trial_status in TRIAL_STATUSES
    )
    outcome_counts = {status: int(counts[status]) for status in TRIAL_STATUSES}
    return {
        "total_trials": sum(outcome_counts.values()),
        "outcome_counts": outcome_counts,
    }


def _expected_execution_cost(config: HonestGateConfig) -> dict[str, float]:
    return {
        "fee_bps": config.taker_bps,
        "half_spread_bps": config.half_spread_bps,
        "slippage_bps": config.slippage_bps,
    }


def _trial_universe(
    trials: Sequence[CampaignTrial],
    *,
    config: HonestGateConfig,
    target_information_cutoff: datetime | None,
) -> tuple[dict[str, CandidateTrialEvidence], tuple[str, ...]]:
    """Require exactly one valid evaluated row per immutable experiment."""
    by_candidate: dict[str, list[CampaignTrial]] = defaultdict(list)
    for trial in trials:
        if trial.run is not None and _is_sealed_oos_artifact_run(trial.run):
            continue
        by_candidate[trial.experiment.experiment_id].append(trial)

    evidence_by_candidate: dict[str, CandidateTrialEvidence] = {}
    reasons: set[str] = set()
    expected_cost = _expected_execution_cost(config)
    target_cutoff_utc = _storage_cutoff_utc(target_information_cutoff)
    if target_cutoff_utc is None:
        reasons.add("missing_information_cutoff")
    for candidate_key, candidate_trials in by_candidate.items():
        evaluated = [
            item
            for item in candidate_trials
            if item.run is not None
            and item.run.trial_status in {"completed", "rejected"}
        ]
        if not evaluated:
            reasons.add("missing_candidate_trial_evidence")
            continue
        if len(evaluated) > 1:
            reasons.add("duplicate_candidate_trial_evidence")
            continue
        candidate_cutoff_utc = _storage_cutoff_utc(evaluated[0].run.information_cutoff)
        if candidate_cutoff_utc is None:
            reasons.add("missing_information_cutoff")
        elif (
            target_cutoff_utc is not None and candidate_cutoff_utc != target_cutoff_utc
        ):
            reasons.add("campaign_information_cutoff_mismatch")
        payload = evaluated[0].run.raw_payload or {}
        if not isinstance(payload, Mapping):
            reasons.add("invalid_trial_evidence")
            continue
        try:
            evidence = parse_trial_evidence(payload.get("trial_evidence"))
        except TrialEvidenceError:
            reasons.add("invalid_trial_evidence")
            continue
        experiment = candidate_trials[0].experiment
        run = evaluated[0].run
        if (
            getattr(run, "runner", None) != config.trial_runner
            or getattr(run, "timeframe", None) != config.trial_timeframe
            or evidence.schema_version != config.trial_evidence_schema_version
            or evidence.producer != config.trial_evidence_producer
            or evidence.producer_version != config.trial_evidence_producer_version
        ):
            reasons.add("trial_producer_mismatch")
            continue
        if evidence.parameter_key != experiment.params_hash:
            reasons.add("trial_parameter_key_mismatch")
            continue
        if evidence.config_hash != config.config_hash():
            reasons.add("trial_provenance_mismatch")
            continue
        if evidence.execution_cost != expected_cost:
            reasons.add("trial_provenance_mismatch")
            continue
        if (
            evidence.sharpe_method != config.trial_sharpe_method
            or evidence.p_value_method != config.trial_p_value_method
        ):
            reasons.add("trial_statistic_method_mismatch")
            continue
        if evidence.sample_size < config.trial_min_folds:
            reasons.add("insufficient_trial_sample")
            continue
        if evidence.validation_score is None or evidence.selection_score_method is None:
            reasons.add("missing_selection_evidence")
            continue
        if evidence.selection_score_method != config.selection_score_method:
            reasons.add("selection_method_mismatch")
            continue
        evidence_by_candidate[candidate_key] = CandidateTrialEvidence(
            experiment=experiment,
            run=evaluated[0].run,
            evidence=evidence,
        )
    return evidence_by_candidate, tuple(sorted(reasons))


def _storage_cutoff_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _selection_from_trial_evidence(
    evidence_by_candidate: Mapping[str, CandidateTrialEvidence],
    *,
    campaign_keys: set[str],
    target_candidate_key: str,
) -> tuple[SelectionResult, tuple[str, ...]]:
    """Reconstruct the only authoritative ranking from immutable trial rows."""
    scores = {
        key: candidate.evidence.validation_score
        for key, candidate in evidence_by_candidate.items()
        if candidate.evidence.validation_score is not None
    }
    if set(scores) != campaign_keys or not scores:
        return SelectionResult("", (), {}), ()

    selection = select_parameters(
        [
            SelectionCandidate(parameter_key=key, validation_score=score)
            for key, score in scores.items()
        ]
    )
    reasons: set[str] = set()
    if len(set(scores.values())) != len(scores):
        reasons.add("ambiguous_selection_score")
    if selection.selected_parameter != target_candidate_key:
        reasons.add("selected_trial_mismatch")
    return selection, tuple(sorted(reasons))


def _target_trial_reasons(
    run: ResearchBacktestRun | Any,
    *,
    target_candidate_key: str,
    evidence_by_candidate: Mapping[str, CandidateTrialEvidence],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if getattr(run, "trial_status", None) not in {"completed", "rejected"}:
        reasons.add("target_trial_not_evaluated")
    target_evidence = evidence_by_candidate.get(target_candidate_key)
    if target_evidence is None or getattr(target_evidence.run, "id", None) != getattr(
        run, "id", None
    ):
        reasons.add("target_trial_evidence_mismatch")
    return tuple(sorted(reasons))


def _validated_caller_selection(
    selection: Any,
    *,
    campaign_keys: set[str],
    pbo_candidate_returns: Any,
) -> tuple[SelectionResult, tuple[str, ...]]:
    reasons: set[str] = set()
    selected = getattr(selection, "selected_parameter", None)
    if not isinstance(selected, str) or not selected:
        selected = ""
        reasons.add("invalid_selection_evidence")

    raw_ranking = getattr(selection, "ranking", None)
    ranking: tuple[str, ...] = ()
    if isinstance(raw_ranking, Sequence) and not isinstance(raw_ranking, str | bytes):
        ranking_items = tuple(raw_ranking)
        if all(isinstance(key, str) and key for key in ranking_items):
            ranking = ranking_items
        else:
            reasons.add("invalid_selection_evidence")
    else:
        reasons.add("invalid_selection_evidence")

    scores: dict[str, float] = {}
    raw_scores = getattr(selection, "validation_scores", None)
    if isinstance(raw_scores, Mapping):
        for key, raw_value in raw_scores.items():
            if (
                not isinstance(key, str)
                or not key
                or type(raw_value) not in {int, float}
            ):
                reasons.add("invalid_selection_evidence")
                continue
            try:
                finite = math.isfinite(raw_value)
            except (TypeError, ValueError, OverflowError):
                reasons.add("invalid_selection_evidence")
                continue
            if not finite:
                reasons.add("invalid_selection_evidence")
                continue
            scores[key] = raw_value
    else:
        reasons.add("invalid_selection_evidence")

    normalized = SelectionResult(
        selected_parameter=selected,
        ranking=ranking,
        validation_scores=scores,
    )
    selection_keys = set(scores)
    if (
        len(ranking) != len(campaign_keys)
        or set(ranking) != campaign_keys
        or selection_keys != campaign_keys
        or not ranking
        or ranking[0] != selected
    ):
        reasons.add("selection_trial_universe_mismatch")
    if scores and ranking != tuple(sorted(scores, key=lambda key: (-scores[key], key))):
        reasons.add("selection_ranking_mismatch")
    try:
        pbo_keys = set(pbo_candidate_returns)
    except TypeError:
        pbo_keys = set()
    if pbo_keys != campaign_keys or any(not isinstance(key, str) for key in pbo_keys):
        reasons.add("pbo_trial_universe_mismatch")
    return normalized, tuple(sorted(reasons))


def _validate_target_identity(
    experiment: CampaignExperiment,
    *,
    supplied_experiment_id: str,
    config: HonestGateConfig,
) -> None:
    if experiment.experiment_id != supplied_experiment_id:
        raise OfflineGateFinalizeError("promotion_hash_mismatch")
    if experiment.frozen_config_hash != config.config_hash():
        raise OfflineGateFinalizeError("frozen_config_hash_mismatch")
    if experiment.policy_hash != canonical_sha256(config.policy_identity()):
        raise OfflineGateFinalizeError("policy_identity_mismatch")
    if experiment.benchmark_hash != canonical_sha256(config.benchmark_identity()):
        raise OfflineGateFinalizeError("benchmark_identity_mismatch")
    if experiment.cost_hash != canonical_sha256(config.cost_identity()):
        raise OfflineGateFinalizeError("cost_identity_mismatch")
    if experiment.mdd_hash != canonical_sha256(config.mdd_identity()):
        raise OfflineGateFinalizeError("mdd_identity_mismatch")


async def finalize_offline_gate(
    session: AsyncSession,
    *,
    backtest_run_id: int,
    experiment_id: str,
    selection: SelectionResult,
    sealed_oos_artifact_id: int,
    pit_evidence: PITEvidence,
    pbo_candidate_returns: Mapping[str, Sequence[float]],
    economic_edge_bps: float,
    fold_metrics: Sequence[Mapping[str, Any]],
    baselines: Mapping[str, float],
    execution_cost: Mapping[str, float],
    random_baseline: Mapping[str, int],
    cost_stress: Mapping[str, float],
    config: HonestGateConfig,
) -> ResearchPromotionCandidate:
    """Consume sealed OOS once and link an exact, campaign-complete artifact."""
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
    if _is_sealed_oos_artifact_run(run):
        raise OfflineGateFinalizeError("promotion_hash_mismatch")
    if run.strategy_experiment_id is None:
        raise OfflineGateFinalizeError("missing_experiment_identity")
    experiment_row = await session.get(
        ResearchStrategyExperiment, run.strategy_experiment_id
    )
    if experiment_row is None:
        raise OfflineGateFinalizeError("promotion_hash_mismatch")
    experiment = CampaignExperiment.from_orm(experiment_row)
    _validate_target_identity(
        experiment,
        supplied_experiment_id=experiment_id,
        config=config,
    )
    await _claim_sealed_oos_artifact(
        session,
        artifact_id=sealed_oos_artifact_id,
        backtest_run_id=backtest_run_id,
    )
    sealed_oos = await _load_sealed_oos_artifact(
        session,
        artifact_id=sealed_oos_artifact_id,
        target=experiment,
        config=config,
    )

    campaign_trials = await _load_campaign_trials(session, experiment)
    accounting = _campaign_accounting(campaign_trials)
    campaign_keys = {item.experiment.experiment_id for item in campaign_trials}
    trial_evidence, evidence_reasons = _trial_universe(
        campaign_trials,
        config=config,
        target_information_cutoff=run.information_cutoff,
    )
    server_selection, server_selection_reasons = _selection_from_trial_evidence(
        trial_evidence,
        campaign_keys=campaign_keys,
        target_candidate_key=experiment.experiment_id,
    )
    target_trial_reasons = _target_trial_reasons(
        run,
        target_candidate_key=experiment.experiment_id,
        evidence_by_candidate=trial_evidence,
    )
    caller_selection, caller_selection_reasons = _validated_caller_selection(
        selection,
        campaign_keys=campaign_keys,
        pbo_candidate_returns=pbo_candidate_returns,
    )
    reasons: set[str] = set()
    if caller_selection != server_selection:
        reasons.add("selection_evidence_mismatch")
    comparison_reasons = tuple(sorted(reasons))
    selection_reasons = tuple(
        sorted(
            {
                *server_selection_reasons,
                *target_trial_reasons,
                *caller_selection_reasons,
                *comparison_reasons,
            }
        )
    )
    precondition_reasons = tuple(sorted({*evidence_reasons, *selection_reasons}))

    if evidence_reasons:
        dsr = StatisticResult(None, evidence_reasons)
        pbo = StatisticResult(None, evidence_reasons)
        fdr = FDRResult((), evidence_reasons)
    else:
        dsr = deflated_sharpe_ratio(
            sealed_oos.returns,
            [item.evidence.sharpe for item in trial_evidence.values()],
            total_trials=accounting["total_trials"],
            min_observations=config.dsr_min_observations,
        )
        if "pbo_trial_universe_mismatch" in selection_reasons:
            pbo = StatisticResult(None, ("pbo_trial_universe_mismatch",))
        else:
            pbo = probability_backtest_overfitting(
                pbo_candidate_returns,
                slices=config.pbo_slices,
            )
        fdr = benjamini_hochberg(
            {key: item.evidence.p_value for key, item in trial_evidence.items()},
            alpha=config.fdr_alpha,
        )

    artifact = build_gate_artifact(
        experiment_id=experiment.experiment_id,
        run_id=run.run_id,
        config_hash=experiment.frozen_config_hash,
        data_hash=experiment.dataset_manifest_hash,
        sealed_oos_artifact_id=sealed_oos_artifact_id,
        selection=server_selection,
        parameter_provenance={
            key: item.experiment.params_hash for key, item in trial_evidence.items()
        },
        sealed_oos=sealed_oos,
        pit_evidence=pit_evidence,
        accounting=accounting,
        dsr=dsr,
        pbo=pbo,
        fdr=fdr,
        economic_edge_bps=economic_edge_bps,
        fold_metrics=fold_metrics,
        baselines=baselines,
        execution_cost=execution_cost,
        random_baseline=random_baseline,
        cost_stress=cost_stress,
        registered_information_cutoff=run.information_cutoff,
        precondition_reasons=precondition_reasons,
        config=config,
    )
    request = PromotionLinkRequest(
        expected_experiment_id=experiment.experiment_id,
        expected_config_hash=experiment.frozen_config_hash,
        expected_data_hash=experiment.dataset_manifest_hash,
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
