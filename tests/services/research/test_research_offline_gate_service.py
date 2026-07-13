from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
    TrialAccounting,
)
from app.services import research_offline_gate_service as service
from app.services.strategy_experiment_registry import record_trial, register_experiment
from research.nautilus_scalping.honest_offline_gate import (
    HonestGateConfig,
    PITEvidence,
    SealedOOS,
    SelectionCandidate,
    select_parameters,
)


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, run, existing_candidate=None):
        self.run = run
        self.existing_candidate = existing_candidate
        self.get = AsyncMock(return_value=run)
        self.scalar = AsyncMock(return_value=existing_candidate)

    def begin_nested(self):
        return _NestedTransaction()


def _inputs(config: HonestGateConfig | None = None) -> dict:
    config = config or HonestGateConfig(
        dsr_min_observations=6,
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "experiment_id": "experiment-id",
        "expected_config_hash": service.registry_config_hash(config),
        "expected_data_hash": "data-hash",
        "selection": select_parameters(
            [
                SelectionCandidate("slow", 1.0),
                SelectionCandidate("fast", 2.0),
            ]
        ),
        "sealed_oos": SealedOOS(
            returns=(0.01, 0.03, -0.01, 0.02, 0.04, -0.005, 0.015, 0.025),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": PITEvidence(
            manifest_hash="data-hash",
            manifest_timestamp=cutoff - timedelta(days=1),
            max_observation_timestamp=cutoff,
            information_cutoff=cutoff,
        ),
        "pbo_candidate_returns": {
            "slow": (0.01,) * 8,
            "fast": (0.02,) * 8,
        },
        "p_values": {"slow": 0.9, "fast": 0.001},
        "candidate_p_value_key": "fast",
        "economic_edge_bps": 2.0,
        "fold_metrics": ({"fold": "validation", "net_return": 0.04},),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "cost_stress": {"net_return": 0.04},
        "observed_mdd_pct": 4.0,
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


def test_finalize_signature_has_no_caller_trial_count() -> None:
    parameters = inspect.signature(service.finalize_offline_gate).parameters
    assert "total_trials" not in parameters
    assert "outcome_counts" not in parameters


@pytest.mark.asyncio
async def test_finalize_reads_complete_accounting_and_links_exact_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id=7, run_id="run-7", strategy_experiment_id=3)
    session = _Session(run)
    accounting = TrialAccounting(
        experiment_id="experiment-id",
        total_trials=5,
        outcome_counts={
            "completed": 2,
            "rejected": 1,
            "crashed": 1,
            "timeout": 1,
        },
    )
    monkeypatch.setattr(
        service, "get_trial_accounting", AsyncMock(return_value=accounting)
    )
    monkeypatch.setattr(
        service,
        "list_trials",
        AsyncMock(
            return_value=[
                SimpleNamespace(trial_status="completed", raw_payload={"sharpe": 0.2}),
                SimpleNamespace(trial_status="completed", raw_payload={"sharpe": 0.5}),
            ]
        ),
    )
    candidate = SimpleNamespace(id=11, status="eligible")
    link = AsyncMock(return_value=candidate)
    monkeypatch.setattr(service, "link_promotion_candidate", link)

    result = await service.finalize_offline_gate(
        session, backtest_run_id=7, **_inputs()
    )

    assert result is candidate
    request = link.await_args.kwargs["request"]
    assert request.expected_experiment_id == "experiment-id"
    assert request.expected_config_hash == _inputs()["expected_config_hash"]
    assert request.expected_data_hash == "data-hash"
    assert request.status == "eligible"
    assert request.metrics["accounting"] == accounting.model_dump()
    assert request.metrics["artifact_hash"]


@pytest.mark.asyncio
async def test_finalize_rejects_identityless_run_before_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        SimpleNamespace(id=7, run_id="legacy", strategy_experiment_id=None)
    )
    accounting = AsyncMock()
    monkeypatch.setattr(service, "get_trial_accounting", accounting)

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs())

    assert exc_info.value.reason_code == "missing_experiment_identity"
    accounting.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_rejects_second_oos_read_before_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(
        SimpleNamespace(id=7, run_id="run-7", strategy_experiment_id=3),
        existing_candidate=SimpleNamespace(id=11),
    )
    accounting = AsyncMock()
    monkeypatch.setattr(service, "get_trial_accounting", accounting)

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs())

    assert exc_info.value.reason_code == "sealed_oos_already_finalized"
    accounting.assert_not_awaited()


@pytest.mark.asyncio
async def test_hash_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = SimpleNamespace(id=7, run_id="run-7", strategy_experiment_id=3)
    session = _Session(run)
    monkeypatch.setattr(
        service,
        "get_trial_accounting",
        AsyncMock(
            return_value=TrialAccounting(
                experiment_id="experiment-id",
                total_trials=1,
                outcome_counts={
                    "completed": 1,
                    "rejected": 0,
                    "crashed": 0,
                    "timeout": 0,
                },
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "list_trials",
        AsyncMock(
            return_value=[
                SimpleNamespace(trial_status="completed", raw_payload={"sharpe": 0.5})
            ]
        ),
    )
    monkeypatch.setattr(
        service,
        "link_promotion_candidate",
        AsyncMock(side_effect=service.PromotionHashMismatch("config_hash")),
    )

    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(session, backtest_run_id=7, **_inputs())

    assert exc_info.value.reason_code == "promotion_hash_mismatch"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finalize_creates_one_exact_registry_link(registry_tables) -> None:
    session = registry_tables
    config = HonestGateConfig(
        dsr_min_observations=6,
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    identity = StrategyExperimentIdentity(
        strategy_key="ROB-847-integration",
        strategy_version=datetime.now(UTC).isoformat(),
        strategy={"name": "fixture"},
        code={"sha": "fixture"},
        params={"lookback": 2},
        dataset_manifest={"bars": "fixture"},
        universe=["BTC", "ETH"],
        pit={"policy": "cutoff_required"},
        frozen_config=config.to_dict(),
        policy={"gate": "honest_offline_gate.v1"},
        benchmark={"names": list(config.baseline_names)},
        cost={"fee_bps": config.taker_bps},
        mdd={"target_pct": config.mdd_target_pct},
    )
    experiment = await register_experiment(session, identity)
    await session.flush()
    first = await record_trial(
        session,
        experiment_id=experiment.experiment_id,
        request=BacktestTrialRequest(
            status="completed",
            strategy_name="fixture",
            timeframe="1d",
            runner="pytest",
            idempotency_key="rob847-first-" + experiment.experiment_id[:12],
            raw_payload={"sharpe": 0.2},
        ),
    )
    final = await record_trial(
        session,
        experiment_id=experiment.experiment_id,
        request=BacktestTrialRequest(
            status="completed",
            strategy_name="fixture",
            timeframe="1d",
            runner="pytest",
            idempotency_key="rob847-final-" + experiment.experiment_id[:12],
            raw_payload={"sharpe": 0.5},
        ),
    )
    assert first.id != final.id

    inputs = _inputs(config)
    inputs["experiment_id"] = experiment.experiment_id
    inputs["expected_config_hash"] = experiment.frozen_config_hash
    inputs["expected_data_hash"] = experiment.dataset_manifest_hash
    inputs["pit_evidence"] = dataclasses.replace(
        inputs["pit_evidence"], manifest_hash=experiment.dataset_manifest_hash
    )
    candidate = await service.finalize_offline_gate(
        session,
        backtest_run_id=final.id,
        **inputs,
    )

    assert candidate.status == "eligible"
    assert candidate.experiment_id == experiment.experiment_id
    assert candidate.run_config_hash == experiment.frozen_config_hash
    assert candidate.run_data_hash == experiment.dataset_manifest_hash
    with pytest.raises(service.OfflineGateFinalizeError) as exc_info:
        await service.finalize_offline_gate(
            session,
            backtest_run_id=final.id,
            **inputs,
        )
    assert exc_info.value.reason_code == "sealed_oos_already_finalized"
