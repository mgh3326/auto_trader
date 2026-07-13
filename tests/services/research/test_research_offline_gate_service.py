from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.core.db import AsyncSessionLocal
from app.models.research_backtest import ResearchPromotionCandidate
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
)
from app.services import research_offline_gate_service as service
from app.services.research_canonical_hash import canonical_sha256
from app.services.strategy_experiment_registry import record_trial, register_experiment
from research.nautilus_scalping.honest_offline_gate import (
    HonestGateConfig,
    PITEvidence,
    SealedOOS,
    SelectionCandidate,
    select_parameters,
)
from research.nautilus_scalping.trial_evidence import build_trial_evidence
from research_contracts.evaluation_windows import ClosedWindow


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, run, experiment=None, existing_candidate=None, artifact=None):
        if run is not None and not hasattr(run, "trial_status"):
            run.trial_status = "completed"
        if artifact is None and experiment is not None:
            artifact = _sealed_artifact(experiment)
        self.run = run
        self.experiment = experiment
        self.existing_candidate = existing_candidate
        self.get = AsyncMock(side_effect=[run, experiment, artifact])
        self.scalar = AsyncMock(side_effect=[existing_candidate, None])
        self.execute = AsyncMock()

    def begin_nested(self):
        return _NestedTransaction()


def _config() -> HonestGateConfig:
    return HonestGateConfig(
        dsr_min_observations=6,
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )


def _experiment(config: HonestGateConfig, *, params_hash: str = "fast"):
    return SimpleNamespace(
        id=3,
        experiment_id="experiment-id",
        strategy_key="ROB-847-unit",
        params_hash=params_hash,
        frozen_config_hash=config.config_hash(),
        dataset_manifest_hash="data-hash",
        universe_hash="universe-hash",
        pit_hash="pit-hash",
        policy_hash=canonical_sha256(config.policy_identity()),
        benchmark_hash=canonical_sha256(config.benchmark_identity()),
        cost_hash=canonical_sha256(config.cost_identity()),
        mdd_hash=canonical_sha256(config.mdd_identity()),
    )


def _sealed_oos() -> SealedOOS:
    return SealedOOS(
        returns=(0.01, 0.03, -0.01, 0.02, 0.04, -0.005, 0.015, 0.025),
        metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
    )


def _sealed_artifact(experiment, *, artifact_id: int = 91):
    payload = service.build_sealed_oos_payload(
        experiment_id=experiment.experiment_id,
        config_hash=experiment.frozen_config_hash,
        data_hash=experiment.dataset_manifest_hash,
        window=_config().evaluation_windows.sealed_oos.to_dict(),
        sealed_oos=_sealed_oos(),
    )
    return SimpleNamespace(
        id=artifact_id,
        strategy_experiment_id=experiment.id,
        runner=service.SEALED_OOS_RUNNER,
        timeframe=service.SEALED_OOS_TIMEFRAME,
        trial_status="completed",
        artifact_path=service.SEALED_OOS_ARTIFACT_PATH,
        artifact_hash=canonical_sha256(payload),
        raw_payload=payload,
    )


def _evidence(
    config: HonestGateConfig,
    key: str,
    sharpe: float,
    p_value: float,
    validation_score: float,
):
    return build_trial_evidence(
        parameter_key=key,
        config_hash=config.config_hash(),
        execution_cost={
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        sharpe=sharpe,
        p_value=p_value,
        sample_size=4,
        validation_score=validation_score,
    )


def _campaign_trials(config: HonestGateConfig):
    cutoff = datetime(2026, 1, 1)
    slow_experiment = dataclasses.replace(
        service.CampaignExperiment.from_orm(_experiment(config)),
        experiment_id="slow-experiment",
        params_hash="slow",
    )
    fast_experiment = service.CampaignExperiment.from_orm(_experiment(config))
    slow = SimpleNamespace(
        id=6,
        run_id="run-slow",
        runner=config.trial_runner,
        timeframe=config.trial_timeframe,
        trial_status="rejected",
        information_cutoff=cutoff,
        raw_payload={"trial_evidence": _evidence(config, "slow", 0.2, 0.9, 1.0)},
    )
    fast = SimpleNamespace(
        id=7,
        run_id="run-fast",
        runner=config.trial_runner,
        timeframe=config.trial_timeframe,
        trial_status="completed",
        information_cutoff=cutoff,
        raw_payload={"trial_evidence": _evidence(config, "fast", 0.5, 0.001, 2.0)},
    )
    return [
        service.CampaignTrial(slow_experiment, slow),
        service.CampaignTrial(fast_experiment, fast),
    ]


def _inputs(config: HonestGateConfig | None = None) -> dict:
    config = config or _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "experiment_id": "experiment-id",
        "selection": select_parameters(
            [
                SelectionCandidate("slow-experiment", 1.0),
                SelectionCandidate("experiment-id", 2.0),
            ]
        ),
        "sealed_oos_artifact_id": 91,
        "pit_evidence": PITEvidence(
            manifest_hash="data-hash",
            manifest_timestamp=cutoff - timedelta(days=1),
            max_observation_timestamp=cutoff,
            information_cutoff=cutoff,
        ),
        "pbo_candidate_returns": {
            "slow-experiment": (0.01,) * 8,
            "experiment-id": (0.02,) * 8,
        },
        "economic_edge_bps": 2.0,
        "fold_metrics": ({"fold": "validation", "net_return": 0.04},),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "config": config,
    }


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


def _registry_identity(
    config: HonestGateConfig,
    *,
    strategy_key: str,
    version: str,
    params: dict,
    candidate: str,
) -> StrategyExperimentIdentity:
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version=version,
        strategy={"name": "fixture", "candidate": candidate},
        code={"sha": candidate},
        params=params,
        dataset_manifest={"bars": "fixture"},
        universe=["BTC", "ETH"],
        pit={"policy": "cutoff_required"},
        frozen_config=config.to_dict(),
        policy=config.policy_identity(),
        benchmark=config.benchmark_identity(),
        cost=config.cost_identity(),
        mdd=config.mdd_identity(),
    )


async def _record_registry_trial(
    session,
    *,
    experiment,
    config: HonestGateConfig,
    status: str,
    cutoff: datetime,
    key: str,
    validation_score: float | None,
    raw_payload: dict | None = None,
):
    if raw_payload is None and validation_score is not None:
        raw_payload = {
            "trial_evidence": _evidence(
                config,
                experiment.params_hash,
                0.5 if validation_score > 1 else 0.2,
                0.001 if validation_score > 1 else 0.9,
                validation_score,
            )
        }
    return await record_trial(
        session,
        experiment_id=experiment.experiment_id,
        request=BacktestTrialRequest(
            status=status,
            strategy_name="fixture",
            timeframe=config.trial_timeframe,
            runner=config.trial_runner,
            information_cutoff=cutoff,
            idempotency_key=key,
            raw_payload=raw_payload,
        ),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_sealed_oos_writer_is_hash_bound_and_excluded_from_trials(
    registry_tables,
) -> None:
    session = registry_tables
    config = _config()
    strategy_key = f"ROB-847-sealed-writer-{datetime.now(UTC).isoformat()}"
    experiment = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="target",
            params={"lookback": 3},
            candidate="target",
        ),
    )
    target = await _record_registry_trial(
        session,
        experiment=experiment,
        config=config,
        status="completed",
        cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        key=f"{strategy_key}-trial",
        validation_score=2.0,
    )
    sealed_oos = _sealed_oos()

    artifact_row = await service.record_sealed_oos_artifact(
        session,
        experiment_id=experiment.experiment_id,
        sealed_oos=sealed_oos,
        config=config,
        idempotency_key=f"{strategy_key}-sealed",
    )
    await session.flush()

    assert artifact_row.runner == "sealed-oos-v1"
    assert artifact_row.timeframe == "sealed-oos"
    assert artifact_row.trial_status == "completed"
    assert artifact_row.artifact_path == "sealed-oos://honest-offline-gate/v1"
    assert canonical_sha256(artifact_row.raw_payload) == artifact_row.artifact_hash
    assert json.loads(json.dumps(artifact_row.raw_payload, allow_nan=False)) == (
        artifact_row.raw_payload
    )
    loaded = await service._load_sealed_oos_artifact(
        session,
        artifact_id=artifact_row.id,
        target=service.CampaignExperiment.from_orm(experiment),
        config=config,
    )
    assert loaded == sealed_oos

    campaign = await service._load_campaign_trials(
        session, service.CampaignExperiment.from_orm(experiment)
    )
    assert [item.run.id for item in campaign if item.run is not None] == [target.id]
    assert service._campaign_accounting(campaign)["total_trials"] == 1

    replay = await service.record_sealed_oos_artifact(
        session,
        experiment_id=experiment.experiment_id,
        sealed_oos=sealed_oos,
        config=config,
        idempotency_key=f"{strategy_key}-sealed",
    )
    assert replay.id == artifact_row.id

    changed = dataclasses.replace(
        sealed_oos,
        metrics={**sealed_oos.metrics, "net_return": 0.09},
    )
    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.record_sealed_oos_artifact(
            session,
            experiment_id=experiment.experiment_id,
            sealed_oos=changed,
            config=config,
            idempotency_key=f"{strategy_key}-sealed",
        )
    assert exc_info.value.reason_code == "sealed_oos_artifact_conflict"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-0.0, 1e20], ids=["negative-zero", "float-to-int"])
async def test_postgres_sealed_oos_writer_rejects_jsonb_unstable_numbers(
    registry_tables,
    value: float,
) -> None:
    session = registry_tables
    config = _config()
    strategy_key = f"ROB-847-jsonb-number-{value.hex()}-{datetime.now(UTC).isoformat()}"
    experiment = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="target",
            params={"lookback": 3},
            candidate="target",
        ),
    )
    sealed_oos = dataclasses.replace(
        _sealed_oos(),
        metrics={"net_return": value, "max_drawdown_pct": 4.0},
    )

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.record_sealed_oos_artifact(
            session,
            experiment_id=experiment.experiment_id,
            sealed_oos=sealed_oos,
            config=config,
            idempotency_key=f"{strategy_key}-sealed",
        )

    assert exc_info.value.reason_code == "invalid_sealed_oos_artifact"


async def _registry_finalize_inputs(session, config, *, target, candidates) -> dict:
    values = _inputs(config)
    artifact = await service.record_sealed_oos_artifact(
        session,
        experiment_id=target.experiment_id,
        sealed_oos=_sealed_oos(),
        config=config,
        idempotency_key=f"sealed-{target.experiment_id}",
    )
    values["sealed_oos_artifact_id"] = artifact.id
    values["experiment_id"] = target.experiment_id
    values["selection"] = select_parameters(
        [
            SelectionCandidate(experiment.experiment_id, score)
            for experiment, score in candidates
        ]
    )
    values["pbo_candidate_returns"] = {
        experiment.experiment_id: ((0.02 if score > 1 else 0.01),) * 8
        for experiment, score in candidates
    }
    values["pit_evidence"] = dataclasses.replace(
        values["pit_evidence"],
        manifest_hash=target.dataset_manifest_hash,
    )
    return values


def test_finalize_signature_has_no_caller_statistical_or_hash_authority() -> None:
    parameters = inspect.signature(service.finalize_offline_gate).parameters
    for forbidden in (
        "total_trials",
        "outcome_counts",
        "p_values",
        "candidate_p_value_key",
        "expected_config_hash",
        "expected_data_hash",
        "sealed_oos",
        "observed_mdd_pct",
    ):
        assert forbidden not in parameters
    assert "sealed_oos_artifact_id" in parameters


@pytest.mark.asyncio
async def test_finalize_uses_only_hash_bound_sealed_oos_drawdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    experiment = _experiment(config)
    high_drawdown = dataclasses.replace(
        _sealed_oos(),
        metrics={"net_return": 0.08, "max_drawdown_pct": 99.0},
    )
    payload = service.build_sealed_oos_payload(
        experiment_id=experiment.experiment_id,
        config_hash=experiment.frozen_config_hash,
        data_hash=experiment.dataset_manifest_hash,
        window=config.evaluation_windows.sealed_oos.to_dict(),
        sealed_oos=high_drawdown,
    )
    artifact = SimpleNamespace(
        id=91,
        strategy_experiment_id=experiment.id,
        runner=service.SEALED_OOS_RUNNER,
        timeframe=service.SEALED_OOS_TIMEFRAME,
        trial_status="completed",
        artifact_path=service.SEALED_OOS_ARTIFACT_PATH,
        artifact_hash=canonical_sha256(payload),
        raw_payload=payload,
    )
    session = _Session(run, experiment, artifact=artifact)
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    candidate = SimpleNamespace(id=11, status="non_promotable")
    link = AsyncMock(return_value=candidate)
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    inputs = _inputs(config)
    result = await service.finalize_offline_gate(
        session,
        backtest_run_id=7,
        **inputs,
    )

    assert result is candidate
    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert request.metrics["mdd"] == {"target_pct": 20.0, "observed_pct": 99.0}
    assert "mdd_target_exceeded" in request.metrics["reason_codes"]


@pytest.mark.parametrize("mismatch", ["runner", "timeframe", "evidence_v2"])
def test_trial_universe_requires_frozen_producer_provenance(mismatch: str) -> None:
    config = _config()
    trials = _campaign_trials(config)
    for trial in trials:
        trial.run.runner = "autoresearch"
        trial.run.timeframe = "1d"
    if mismatch == "runner":
        trials[0].run.runner = "pytest"
    elif mismatch == "timeframe":
        trials[0].run.timeframe = "5m"
    else:
        payload = trials[0].run.raw_payload["trial_evidence"]
        payload["schema_version"] = "honest_trial.v2"
        payload.pop("producer", None)
        payload.pop("producer_version", None)

    evidence, reasons = service._trial_universe(
        trials,
        config=config,
        target_information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert "trial_producer_mismatch" in reasons
    assert "slow-experiment" not in evidence


def test_null_runner_trial_is_counted_and_fails_producer_while_artifact_is_excluded() -> (
    None
):
    config = _config()
    experiment = service.CampaignExperiment.from_orm(_experiment(config))
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    legacy = SimpleNamespace(
        id=7,
        runner=None,
        timeframe=config.trial_timeframe,
        trial_status="completed",
        information_cutoff=cutoff,
        raw_payload={"trial_evidence": _evidence(config, "fast", 0.5, 0.001, 2.0)},
    )
    artifact = _sealed_artifact(_experiment(config))
    trials = [
        service.CampaignTrial(experiment, legacy),
        service.CampaignTrial(experiment, artifact),
    ]

    accounting = service._campaign_accounting(trials)
    evidence, reasons = service._trial_universe(
        trials,
        config=config,
        target_information_cutoff=cutoff,
    )

    assert accounting["total_trials"] == 1
    assert accounting["outcome_counts"]["completed"] == 1
    assert evidence == {}
    assert reasons == ("trial_producer_mismatch",)


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("policy_hash", "policy_identity_mismatch"),
        ("mdd_hash", "mdd_identity_mismatch"),
    ],
)
def test_target_identity_rejects_false_policy_or_mdd_provenance(
    field: str, reason: str
) -> None:
    config = _config()
    experiment = service.CampaignExperiment.from_orm(_experiment(config))
    experiment = dataclasses.replace(experiment, **{field: "false-provenance"})

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        service._validate_target_identity(
            experiment,
            supplied_experiment_id=experiment.experiment_id,
            config=config,
        )

    assert exc_info.value.reason_code == reason


def test_trial_universe_keeps_distinct_experiments_with_identical_params() -> None:
    config = _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    first_experiment = dataclasses.replace(
        service.CampaignExperiment.from_orm(_experiment(config, params_hash="same")),
        id=10,
        experiment_id="candidate-a",
    )
    second_experiment = dataclasses.replace(
        first_experiment,
        id=11,
        experiment_id="candidate-b",
    )
    trials = [
        service.CampaignTrial(
            first_experiment,
            SimpleNamespace(
                id=20,
                runner=config.trial_runner,
                timeframe=config.trial_timeframe,
                trial_status="rejected",
                information_cutoff=cutoff,
                raw_payload={
                    "trial_evidence": _evidence(config, "same", 0.2, 0.9, 1.0)
                },
            ),
        ),
        service.CampaignTrial(
            second_experiment,
            SimpleNamespace(
                id=21,
                runner=config.trial_runner,
                timeframe=config.trial_timeframe,
                trial_status="completed",
                information_cutoff=cutoff,
                raw_payload={
                    "trial_evidence": _evidence(config, "same", 0.5, 0.001, 2.0)
                },
            ),
        ),
    ]

    evidence, reasons = service._trial_universe(
        trials,
        config=config,
        target_information_cutoff=cutoff,
    )

    assert reasons == ()
    assert set(evidence) == {"candidate-a", "candidate-b"}
    assert evidence["candidate-a"].experiment.params_hash == "same"
    assert evidence["candidate-b"].experiment.params_hash == "same"


@pytest.mark.asyncio
async def test_finalize_crashed_target_cannot_borrow_completed_sibling_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    target_experiment = service.CampaignExperiment.from_orm(_experiment(config))
    other_experiment = dataclasses.replace(
        target_experiment,
        id=4,
        experiment_id="slow-experiment",
        params_hash="slow",
    )
    target = SimpleNamespace(
        id=7,
        run_id="run-fast-crashed",
        strategy_experiment_id=3,
        trial_status="crashed",
        information_cutoff=cutoff,
        raw_payload={"evaluation_failure": {"status": "crashed"}},
    )
    completed_sibling = SimpleNamespace(
        id=8,
        run_id="run-fast-sibling",
        runner=config.trial_runner,
        timeframe=config.trial_timeframe,
        trial_status="completed",
        information_cutoff=cutoff,
        raw_payload={"trial_evidence": _evidence(config, "fast", 0.5, 0.001, 2.0)},
    )
    slow = SimpleNamespace(
        id=6,
        run_id="run-slow",
        runner=config.trial_runner,
        timeframe=config.trial_timeframe,
        trial_status="rejected",
        information_cutoff=cutoff,
        raw_payload={"trial_evidence": _evidence(config, "slow", 0.2, 0.9, 1.0)},
    )
    session = _Session(target, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(
            return_value=[
                service.CampaignTrial(other_experiment, slow),
                service.CampaignTrial(target_experiment, target),
                service.CampaignTrial(target_experiment, completed_sibling),
            ]
        ),
    )
    link = AsyncMock(return_value=SimpleNamespace(status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)
    inputs = _inputs(config)
    inputs["selection"] = select_parameters(
        [
            SelectionCandidate("slow-experiment", 1.0),
            SelectionCandidate("experiment-id", 2.0),
        ]
    )
    inputs["pbo_candidate_returns"] = {
        "slow-experiment": (0.01,) * 8,
        "experiment-id": (0.02,) * 8,
    }

    await service.finalize_offline_gate(session, backtest_run_id=7, **inputs)

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "target_trial_not_evaluated" in request.metrics["reason_codes"]
    assert request.metrics["selected_parameter"] == "experiment-id"


@pytest.mark.asyncio
async def test_finalize_requires_evidence_from_exact_target_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    target = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        trial_status="completed",
        information_cutoff=cutoff,
        raw_payload={"description": "missing canonical evidence"},
    )
    session = _Session(target, _experiment(config))
    trials = _campaign_trials(config)
    trials[-1] = service.CampaignTrial(trials[-1].experiment, target)
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=trials),
    )
    link = AsyncMock(return_value=SimpleNamespace(status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)
    inputs = _inputs(config)
    inputs["selection"] = select_parameters(
        [
            SelectionCandidate("slow-experiment", 1.0),
            SelectionCandidate("experiment-id", 2.0),
        ]
    )
    inputs["pbo_candidate_returns"] = {
        "slow-experiment": (0.01,) * 8,
        "experiment-id": (0.02,) * 8,
    }

    await service.finalize_offline_gate(session, backtest_run_id=7, **inputs)

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "target_trial_evidence_mismatch" in request.metrics["reason_codes"]


@pytest.mark.asyncio
async def test_finalize_derives_completed_and_rejected_campaign_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    candidate = SimpleNamespace(id=11, status="eligible")
    link = AsyncMock(return_value=candidate)
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    result = await service.finalize_offline_gate(
        session, backtest_run_id=7, **_inputs(config)
    )

    assert result is candidate
    request = link.await_args.kwargs["request"]
    assert request.expected_experiment_id == "experiment-id"
    assert request.expected_config_hash == config.config_hash()
    assert request.expected_data_hash == "data-hash"
    assert request.status == "eligible"
    assert request.metrics["accounting"] == {
        "total_trials": 2,
        "outcome_counts": {
            "completed": 1,
            "rejected": 1,
            "crashed": 0,
            "timeout": 0,
        },
    }
    assert request.metrics["fdr"]["rejected"] == ["experiment-id"]
    assert request.metrics["selected_parameter"] == "experiment-id"
    assert request.metrics["selection"] == {
        "selected_parameter": "experiment-id",
        "ranking": ["experiment-id", "slow-experiment"],
        "validation_scores": {"experiment-id": 2.0, "slow-experiment": 1.0},
        "parameter_provenance": {
            "experiment-id": "fast",
            "slow-experiment": "slow",
        },
    }
    assert request.metrics["artifact_hash"]


@pytest.mark.asyncio
async def test_finalize_caller_score_manipulation_is_non_promotable_and_not_artifact_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    link = AsyncMock(return_value=SimpleNamespace(id=11, status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)
    inputs = _inputs(config)
    inputs["selection"] = dataclasses.replace(
        inputs["selection"],
        validation_scores={"slow-experiment": 1.0, "experiment-id": 999.0},
    )

    await service.finalize_offline_gate(session, backtest_run_id=7, **inputs)

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "selection_evidence_mismatch" in request.metrics["reason_codes"]
    assert request.metrics["selection"] == {
        "selected_parameter": "experiment-id",
        "ranking": ["experiment-id", "slow-experiment"],
        "validation_scores": {"experiment-id": 2.0, "slow-experiment": 1.0},
        "parameter_provenance": {
            "experiment-id": "fast",
            "slow-experiment": "slow",
        },
    }


@pytest.mark.parametrize(
    ("case", "reason", "selected_parameter"),
    [
        ("target_not_best", "selected_trial_mismatch", "slow-experiment"),
        ("tie", "ambiguous_selection_score", "experiment-id"),
        ("non_finite", "invalid_trial_evidence", ""),
        ("legacy_v1", "trial_producer_mismatch", ""),
    ],
)
@pytest.mark.asyncio
async def test_finalize_reconstructs_selection_from_persisted_trial_evidence(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
    selected_parameter: str,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    trials = _campaign_trials(config)
    slow_evidence = trials[0].run.raw_payload["trial_evidence"]
    if case == "target_not_best":
        slow_evidence["validation_score"] = 3.0
    elif case == "tie":
        slow_evidence["validation_score"] = 2.0
    elif case == "non_finite":
        slow_evidence["validation_score"] = float("nan")
    else:
        slow_evidence["schema_version"] = "honest_trial.v1"
        slow_evidence.pop("validation_score")
        slow_evidence.pop("selection_score_method")
        slow_evidence.pop("producer")
        slow_evidence.pop("producer_version")
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=trials),
    )
    link = AsyncMock(return_value=SimpleNamespace(id=11, status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs(config))

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert reason in request.metrics["reason_codes"]
    assert request.metrics["selected_parameter"] == selected_parameter


@pytest.mark.asyncio
async def test_finalize_binds_trial_selection_method_to_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = dataclasses.replace(
        _config(), selection_score_method="unsupported_validation_score"
    )
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    link = AsyncMock(return_value=SimpleNamespace(id=11, status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs(config))

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "selection_method_mismatch" in request.metrics["reason_codes"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trial_sharpe_method", "median_cv_fold_sharpe"),
        ("trial_p_value_method", "one_sided_t_cv_fold_sharpe"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_binds_trial_statistic_methods_to_frozen_policy(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    config = dataclasses.replace(_config(), **{field: value})
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    link = AsyncMock(return_value=SimpleNamespace(id=11, status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs(config))

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "trial_statistic_method_mismatch" in request.metrics["reason_codes"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("sharpe",), True),
        (("p_value",), "0.01"),
        (("validation_score",), True),
        (("execution_cost", "fee_bps"), "4.0"),
    ],
)
@pytest.mark.asyncio
async def test_finalize_malformed_numeric_evidence_fails_closed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
    value,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    trials = _campaign_trials(config)
    evidence = trials[0].run.raw_payload["trial_evidence"]
    if len(path) == 1:
        evidence[path[0]] = value
    else:
        evidence[path[0]][path[1]] = value
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=trials),
    )
    link = AsyncMock(return_value=SimpleNamespace(id=11, status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs(config))

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert "invalid_trial_evidence" in request.metrics["reason_codes"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda values: values.update(
                selection=select_parameters([SelectionCandidate("experiment-id", 2.0)])
            ),
            "selection_trial_universe_mismatch",
        ),
        (
            lambda values: values.update(
                pbo_candidate_returns={"experiment-id": (0.02,) * 8}
            ),
            "pbo_trial_universe_mismatch",
        ),
        (
            lambda values: values.update(
                selection=select_parameters(
                    [
                        SelectionCandidate("slow-experiment", 2.0),
                        SelectionCandidate("experiment-id", 1.0),
                    ]
                )
            ),
            "selection_evidence_mismatch",
        ),
        (
            lambda values: values.update(
                selection=dataclasses.replace(
                    values["selection"],
                    validation_scores={
                        "slow-experiment": 3.0,
                        "experiment-id": 1.0,
                    },
                )
            ),
            "selection_ranking_mismatch",
        ),
        (
            lambda values: values.update(
                selection=dataclasses.replace(
                    values["selection"],
                    validation_scores={
                        "slow-experiment": "bad",
                        "experiment-id": 1.0,
                    },
                )
            ),
            "invalid_selection_evidence",
        ),
        (
            lambda values: values.update(
                selection=dataclasses.replace(
                    values["selection"],
                    validation_scores={
                        "slow-experiment": 10**10000,
                        "experiment-id": 1.0,
                    },
                )
            ),
            "invalid_selection_evidence",
        ),
        (
            lambda values: values.update(
                pbo_candidate_returns={
                    "slow-experiment": (10**10000,) * 8,
                    "experiment-id": (0.02,) * 8,
                }
            ),
            "non_finite_pbo_input",
        ),
        (
            lambda values: values.update(
                selection=dataclasses.replace(
                    values["selection"],
                    selected_parameter=object(),
                    ranking=(["experiment-id"],),
                    validation_scores=None,
                )
            ),
            "invalid_selection_evidence",
        ),
    ],
)
@pytest.mark.asyncio
async def test_finalize_seals_exact_universe_or_selected_target_mismatch(
    monkeypatch: pytest.MonkeyPatch, mutate, reason: str
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    candidate = SimpleNamespace(id=11, status="non_promotable")
    link = AsyncMock(return_value=candidate)
    monkeypatch.setattr(service, "link_promotion_candidate", link)
    inputs = _inputs(config)
    mutate(inputs)

    result = await service.finalize_offline_gate(session, backtest_run_id=7, **inputs)

    assert result is candidate
    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert reason in request.metrics["reason_codes"]


@pytest.mark.parametrize(
    ("mutate_trials", "reason"),
    [
        (
            lambda trials: setattr(
                trials[0].run, "raw_payload", {"metrics": {"sharpe": 0.2}}
            ),
            "invalid_trial_evidence",
        ),
        (
            lambda trials: setattr(trials[0].run, "raw_payload", ["malformed"]),
            "invalid_trial_evidence",
        ),
        (
            lambda trials: setattr(trials[0].run, "trial_status", "crashed"),
            "missing_candidate_trial_evidence",
        ),
        (
            lambda trials: trials.append(trials[0]),
            "duplicate_candidate_trial_evidence",
        ),
        (
            lambda trials: setattr(
                trials[0].run, "information_cutoff", datetime(2026, 1, 2)
            ),
            "campaign_information_cutoff_mismatch",
        ),
        (
            lambda trials: setattr(trials[0].run, "information_cutoff", None),
            "missing_information_cutoff",
        ),
    ],
)
@pytest.mark.asyncio
async def test_finalize_seals_invalid_incomplete_or_duplicate_trial_evidence(
    monkeypatch: pytest.MonkeyPatch, mutate_trials, reason: str
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session = _Session(run, _experiment(config))
    trials = _campaign_trials(config)
    mutate_trials(trials)
    monkeypatch.setattr(
        service, "_load_campaign_trials", AsyncMock(return_value=trials)
    )
    candidate = SimpleNamespace(id=11, status="non_promotable")
    link = AsyncMock(return_value=candidate)
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs(config))

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert reason in request.metrics["reason_codes"]
    assert request.metrics["dsr"]["value"] is None


@pytest.mark.parametrize(
    ("run_cutoff", "supplied_cutoff", "reason"),
    [
        (None, datetime(2026, 1, 1, tzinfo=UTC), "missing_information_cutoff"),
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "information_cutoff_mismatch",
        ),
    ],
)
@pytest.mark.asyncio
async def test_finalize_seals_persisted_cutoff_missing_or_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    run_cutoff,
    supplied_cutoff,
    reason: str,
) -> None:
    config = _config()
    run = SimpleNamespace(
        id=7,
        run_id="run-fast",
        strategy_experiment_id=3,
        information_cutoff=run_cutoff,
    )
    session = _Session(run, _experiment(config))
    monkeypatch.setattr(
        service,
        "_load_campaign_trials",
        AsyncMock(return_value=_campaign_trials(config)),
    )
    link = AsyncMock(return_value=SimpleNamespace(status="non_promotable"))
    monkeypatch.setattr(service, "link_promotion_candidate", link)
    inputs = _inputs(config)
    inputs["pit_evidence"] = dataclasses.replace(
        inputs["pit_evidence"], information_cutoff=supplied_cutoff
    )

    await service.finalize_offline_gate(session, backtest_run_id=7, **inputs)

    request = link.await_args.kwargs["request"]
    assert request.status == "non_promotable"
    assert reason in request.metrics["reason_codes"]
    expected_registered = run_cutoff.isoformat() if run_cutoff else None
    assert (
        request.metrics["pit"]["registered_information_cutoff"] == expected_registered
    )
    assert request.metrics["pit"]["information_cutoff"] == supplied_cutoff.isoformat()


@pytest.mark.asyncio
async def test_finalize_rejects_identityless_or_wrong_experiment_before_campaign_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock()
    monkeypatch.setattr(service, "_load_campaign_trials", load)
    identityless = _Session(
        SimpleNamespace(id=7, run_id="legacy", strategy_experiment_id=None), None
    )
    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(
            identityless, backtest_run_id=7, **_inputs()
        )
    assert exc_info.value.reason_code == "missing_experiment_identity"

    config = _config()
    wrong = _inputs(config)
    wrong["experiment_id"] = "wrong"
    session = _Session(
        SimpleNamespace(
            id=7,
            run_id="run-fast",
            strategy_experiment_id=3,
            information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        _experiment(config),
    )
    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(session, backtest_run_id=7, **wrong)
    assert exc_info.value.reason_code == "promotion_hash_mismatch"
    load.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_rejects_second_oos_read_before_campaign_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    session = _Session(
        SimpleNamespace(id=7, run_id="run-fast", strategy_experiment_id=3),
        _experiment(config),
        existing_candidate=SimpleNamespace(id=11),
    )
    load = AsyncMock()
    monkeypatch.setattr(service, "_load_campaign_trials", load)

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(
            session, backtest_run_id=7, **_inputs(config)
        )

    assert exc_info.value.reason_code == "sealed_oos_already_finalized"
    load.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finalize_queries_full_campaign_and_creates_one_exact_link(
    registry_tables,
) -> None:
    session = registry_tables
    strategy_key = "ROB-847-integration-" + datetime.now(UTC).isoformat()
    cutoff = datetime.fromisoformat("2026-01-01T09:00:00+09:00")
    params = ({"lookback": 2}, {"lookback": 3})
    parameter_keys = tuple(canonical_sha256(value) for value in params)
    config = _config()

    experiments = []
    trials = []
    for index, (parameter, key, status, sharpe, p_value) in enumerate(
        zip(
            params,
            parameter_keys,
            ("rejected", "completed"),
            (0.2, 0.5),
            (0.9, 0.001),
            strict=True,
        )
    ):
        identity = StrategyExperimentIdentity(
            strategy_key=strategy_key,
            strategy_version=f"v{index}",
            strategy={"name": "fixture", "candidate": index},
            code={"sha": f"fixture-{index}"},
            params=parameter,
            dataset_manifest={"bars": "fixture"},
            universe=["BTC", "ETH"],
            pit={"policy": "cutoff_required"},
            frozen_config=config.to_dict(),
            policy=config.policy_identity(),
            benchmark=config.benchmark_identity(),
            cost=config.cost_identity(),
            mdd=config.mdd_identity(),
        )
        experiment = await register_experiment(session, identity)
        assert experiment.params_hash == key
        trial = await record_trial(
            session,
            experiment_id=experiment.experiment_id,
            request=BacktestTrialRequest(
                status=status,
                strategy_name="fixture",
                timeframe=config.trial_timeframe,
                runner=config.trial_runner,
                information_cutoff=cutoff,
                idempotency_key=f"rob847-{strategy_key}-{index}",
                raw_payload={
                    "trial_evidence": _evidence(
                        config,
                        key,
                        sharpe,
                        p_value,
                        float(index + 1),
                    )
                },
            ),
        )
        experiments.append(experiment)
        trials.append(trial)
    await session.flush()

    stored_cutoff = await session.scalar(
        text(
            "SELECT information_cutoff FROM research.backtest_runs WHERE id = :run_id"
        ),
        {"run_id": trials[1].id},
    )
    assert stored_cutoff == datetime(2026, 1, 1, tzinfo=UTC)

    inputs = await _registry_finalize_inputs(
        session,
        config,
        target=experiments[1],
        candidates=((experiments[0], 1.0), (experiments[1], 2.0)),
    )

    candidate = await service.finalize_offline_gate(
        session,
        backtest_run_id=trials[1].id,
        **inputs,
    )

    assert candidate.status == "eligible"
    assert candidate.experiment_id == experiments[1].experiment_id
    assert candidate.run_config_hash == experiments[1].frozen_config_hash
    assert candidate.run_data_hash == experiments[1].dataset_manifest_hash
    assert candidate.metrics["accounting"]["total_trials"] == 2
    await session.flush()
    await session.refresh(candidate)
    persisted_metrics = json.loads(json.dumps(candidate.metrics, allow_nan=False))
    assert persisted_metrics == candidate.metrics
    persisted_hash = persisted_metrics.pop("artifact_hash")
    assert canonical_sha256(persisted_metrics) == persisted_hash
    assert (
        candidate.metrics["sealed_oos_artifact_id"] == inputs["sealed_oos_artifact_id"]
    )
    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(
            session,
            backtest_run_id=trials[1].id,
            **inputs,
        )
    assert exc_info.value.reason_code == "sealed_oos_already_finalized"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_same_params_distinct_experiments_are_distinct_candidates(
    registry_tables,
) -> None:
    session = registry_tables
    config = _config()
    unique = datetime.now(UTC).isoformat()
    strategy_key = f"ROB-847-same-params-{unique}"
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    params = {"lookback": 2}
    first = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="v1",
            params=params,
            candidate="code-a",
        ),
    )
    second = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="v2",
            params=params,
            candidate="code-b",
        ),
    )
    first_run = await _record_registry_trial(
        session,
        experiment=first,
        config=config,
        status="rejected",
        cutoff=cutoff,
        key=f"{strategy_key}-first",
        validation_score=1.0,
    )
    second_run = await _record_registry_trial(
        session,
        experiment=second,
        config=config,
        status="completed",
        cutoff=cutoff,
        key=f"{strategy_key}-second",
        validation_score=2.0,
    )
    assert first.params_hash == second.params_hash

    candidate = await service.finalize_offline_gate(
        session,
        backtest_run_id=second_run.id,
        **(
            await _registry_finalize_inputs(
                session,
                config,
                target=second,
                candidates=((first, 1.0), (second, 2.0)),
            )
        ),
    )

    assert candidate.status == "eligible"
    assert candidate.metrics["selection"]["ranking"] == [
        second.experiment_id,
        first.experiment_id,
    ]
    assert candidate.metrics["selection"]["parameter_provenance"] == {
        first.experiment_id: first.params_hash,
        second.experiment_id: second.params_hash,
    }
    assert first_run.id != second_run.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_campaign_does_not_pool_different_evaluation_windows(
    registry_tables,
) -> None:
    session = registry_tables
    base = _config()
    changed = dataclasses.replace(
        base,
        evaluation_windows=dataclasses.replace(
            base.evaluation_windows,
            sealed_oos=ClosedWindow(start="2026-02-02", end="2026-03-22"),
        ),
    )
    strategy_key = f"ROB-847-window-isolation-{datetime.now(UTC).isoformat()}"
    base_experiment = await register_experiment(
        session,
        _registry_identity(
            base,
            strategy_key=strategy_key,
            version="base",
            params={"lookback": 2},
            candidate="base",
        ),
    )
    changed_experiment = await register_experiment(
        session,
        _registry_identity(
            changed,
            strategy_key=strategy_key,
            version="changed-window",
            params={"lookback": 3},
            candidate="changed-window",
        ),
    )

    loaded = await service._load_campaign_trials(
        session,
        service.CampaignExperiment.from_orm(base_experiment),
    )

    assert {item.experiment.experiment_id for item in loaded} == {
        base_experiment.experiment_id
    }
    assert changed_experiment.frozen_config_hash != base_experiment.frozen_config_hash
    assert changed_experiment.policy_hash != base_experiment.policy_hash


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_crashed_target_cannot_borrow_completed_sibling(
    registry_tables,
) -> None:
    session = registry_tables
    config = _config()
    unique = datetime.now(UTC).isoformat()
    strategy_key = f"ROB-847-crashed-target-{unique}"
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    slow = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="slow",
            params={"lookback": 2},
            candidate="slow",
        ),
    )
    target = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="target",
            params={"lookback": 3},
            candidate="target",
        ),
    )
    await _record_registry_trial(
        session,
        experiment=slow,
        config=config,
        status="rejected",
        cutoff=cutoff,
        key=f"{strategy_key}-slow",
        validation_score=1.0,
    )
    sibling = await _record_registry_trial(
        session,
        experiment=target,
        config=config,
        status="completed",
        cutoff=cutoff,
        key=f"{strategy_key}-sibling",
        validation_score=2.0,
    )
    crashed = await _record_registry_trial(
        session,
        experiment=target,
        config=config,
        status="crashed",
        cutoff=cutoff,
        key=f"{strategy_key}-crashed",
        validation_score=None,
        raw_payload={"evaluation_failure": {"status": "crashed"}},
    )

    candidate = await service.finalize_offline_gate(
        session,
        backtest_run_id=crashed.id,
        **(
            await _registry_finalize_inputs(
                session,
                config,
                target=target,
                candidates=((slow, 1.0), (target, 2.0)),
            )
        ),
    )

    assert candidate.status == "non_promotable"
    assert "target_trial_not_evaluated" in candidate.metrics["reason_codes"]
    assert "target_trial_evidence_mismatch" in candidate.metrics["reason_codes"]
    assert sibling.id != crashed.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_missing_exact_target_evidence_is_non_promotable(
    registry_tables,
) -> None:
    session = registry_tables
    config = _config()
    unique = datetime.now(UTC).isoformat()
    strategy_key = f"ROB-847-missing-target-{unique}"
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    target = await register_experiment(
        session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="target",
            params={"lookback": 3},
            candidate="target",
        ),
    )
    target_run = await _record_registry_trial(
        session,
        experiment=target,
        config=config,
        status="completed",
        cutoff=cutoff,
        key=f"{strategy_key}-target",
        validation_score=None,
        raw_payload={"description": "canonical evidence missing"},
    )

    candidate = await service.finalize_offline_gate(
        session,
        backtest_run_id=target_run.id,
        **(
            await _registry_finalize_inputs(
                session,
                config,
                target=target,
                candidates=((target, 2.0),),
            )
        ),
    )

    assert candidate.status == "non_promotable"
    assert "invalid_trial_evidence" in candidate.metrics["reason_codes"]
    assert "target_trial_evidence_mismatch" in candidate.metrics["reason_codes"]


@pytest.mark.parametrize(
    ("component", "reason"),
    [
        ("policy", "policy_identity_mismatch"),
        ("mdd", "mdd_identity_mismatch"),
    ],
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_finalize_rejects_persisted_false_policy_or_mdd_provenance(
    registry_tables, component: str, reason: str
) -> None:
    session = registry_tables
    config = _config()
    identity_values = {
        "strategy_key": f"ROB-847-provenance-{component}-{datetime.now(UTC).isoformat()}",
        "strategy_version": "v1",
        "strategy": {"name": "fixture"},
        "code": {"sha": "fixture"},
        "params": {"lookback": 2},
        "dataset_manifest": {"bars": "fixture"},
        "universe": ["BTC", "ETH"],
        "pit": {"policy": "cutoff_required"},
        "frozen_config": config.to_dict(),
        "policy": config.policy_identity(),
        "benchmark": config.benchmark_identity(),
        "cost": config.cost_identity(),
        "mdd": config.mdd_identity(),
    }
    identity_values[component] = {"false": "provenance"}
    experiment = await register_experiment(
        session, StrategyExperimentIdentity(**identity_values)
    )
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    trial = await record_trial(
        session,
        experiment_id=experiment.experiment_id,
        request=BacktestTrialRequest(
            status="completed",
            strategy_name="fixture",
            timeframe=config.trial_timeframe,
            runner=config.trial_runner,
            information_cutoff=cutoff,
            idempotency_key=f"rob847-provenance-{component}-{experiment.experiment_id}",
            raw_payload={
                "trial_evidence": _evidence(
                    config,
                    experiment.params_hash,
                    0.5,
                    0.001,
                    2.0,
                )
            },
        ),
    )
    await session.flush()
    inputs = _inputs(config)
    inputs["experiment_id"] = experiment.experiment_id
    inputs["selection"] = select_parameters(
        [SelectionCandidate(experiment.experiment_id, 2.0)]
    )
    inputs["pbo_candidate_returns"] = {experiment.experiment_id: (0.02,) * 8}

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(
            session,
            backtest_run_id=trial.id,
            **inputs,
        )

    assert exc_info.value.reason_code == reason


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_distinct_runs_cannot_reuse_one_sealed_oos_artifact(
    registry_tables,
) -> None:
    seed_session = registry_tables
    config = _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_key = f"ROB-847-artifact-reuse-{datetime.now(UTC).isoformat()}"
    experiment = await register_experiment(
        seed_session,
        _registry_identity(
            config,
            strategy_key=strategy_key,
            version="target",
            params={"lookback": 3},
            candidate="target",
        ),
    )
    runs = [
        await _record_registry_trial(
            seed_session,
            experiment=experiment,
            config=config,
            status="completed",
            cutoff=cutoff,
            key=f"{strategy_key}-trial-{index}",
            validation_score=2.0,
        )
        for index in range(2)
    ]
    inputs = await _registry_finalize_inputs(
        seed_session,
        config,
        target=experiment,
        candidates=((experiment, 2.0),),
    )
    await seed_session.commit()

    async def finalize_once(run_id: int):
        async with AsyncSessionLocal() as session:
            try:
                result = await service.finalize_offline_gate(
                    session,
                    backtest_run_id=run_id,
                    **inputs,
                )
            except service.OfflineGateFinalizeError as exc:
                await session.rollback()
                return exc.reason_code
            await session.commit()
            return result.id

    outcomes = await asyncio.gather(
        *(finalize_once(run.id) for run in runs),
    )

    assert outcomes.count("sealed_oos_artifact_already_used") == 1
    assert sum(type(outcome) is int for outcome in outcomes) == 1
    async with AsyncSessionLocal() as verify_session:
        count = await verify_session.scalar(
            select(func.count())
            .select_from(ResearchPromotionCandidate)
            .where(
                ResearchPromotionCandidate.backtest_run_id.in_([run.id for run in runs])
            )
        )
    assert count == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_finalize_creates_exactly_one_row_and_keeps_sessions_usable(
    registry_tables,
) -> None:
    seed_session = registry_tables
    config = _config()
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_key = "ROB-847-concurrent-" + datetime.now(UTC).isoformat()
    parameters = ({"lookback": 2}, {"lookback": 3})
    experiments = []
    trials = []
    for index, parameter in enumerate(parameters):
        identity = StrategyExperimentIdentity(
            strategy_key=strategy_key,
            strategy_version=f"v{index}",
            strategy={"name": "fixture", "candidate": index},
            code={"sha": f"fixture-{index}"},
            params=parameter,
            dataset_manifest={"bars": "fixture"},
            universe=["BTC", "ETH"],
            pit={"policy": "cutoff_required"},
            frozen_config=config.to_dict(),
            policy=config.policy_identity(),
            benchmark=config.benchmark_identity(),
            cost=config.cost_identity(),
            mdd=config.mdd_identity(),
        )
        experiment = await register_experiment(seed_session, identity)
        trial = await record_trial(
            seed_session,
            experiment_id=experiment.experiment_id,
            request=BacktestTrialRequest(
                status="completed" if index else "rejected",
                strategy_name="fixture",
                timeframe=config.trial_timeframe,
                runner=config.trial_runner,
                information_cutoff=cutoff,
                idempotency_key=f"rob847-race-{strategy_key}-{index}",
                raw_payload={
                    "trial_evidence": _evidence(
                        config,
                        experiment.params_hash,
                        0.5 if index else 0.2,
                        0.001 if index else 0.9,
                        float(index + 1),
                    )
                },
            ),
        )
        experiments.append(experiment)
        trials.append(trial)
    inputs = await _registry_finalize_inputs(
        seed_session,
        config,
        target=experiments[1],
        candidates=((experiments[0], 1.0), (experiments[1], 2.0)),
    )
    await seed_session.commit()

    async def finalize_once():
        async with AsyncSessionLocal() as session:
            try:
                candidate = await service.finalize_offline_gate(
                    session,
                    backtest_run_id=trials[1].id,
                    **inputs,
                )
            except service.OfflineGateFinalizeError as exc:
                assert await session.scalar(text("SELECT 1")) == 1
                await session.rollback()
                return exc.reason_code
            assert await session.scalar(text("SELECT 1")) == 1
            await session.commit()
            return candidate.id

    outcomes = await asyncio.gather(finalize_once(), finalize_once())

    assert outcomes.count("sealed_oos_already_finalized") == 1
    assert sum(isinstance(outcome, int) for outcome in outcomes) == 1
    async with AsyncSessionLocal() as verify_session:
        count = await verify_session.scalar(
            select(func.count())
            .select_from(ResearchPromotionCandidate)
            .where(ResearchPromotionCandidate.backtest_run_id == trials[1].id)
        )
    assert count == 1
