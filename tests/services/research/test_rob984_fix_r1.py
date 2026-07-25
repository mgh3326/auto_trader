"""ROB-984 adversarial verification R1 closure and reproducibility gates."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)
from app.services import rob974_h6b_materializer as materializer
from research_contracts.canonical_hash import canonical_sha256

_FULL_CAMPAIGN_HASH = "70f352c3c477e27a36111f1daa584deb4ca570ec57ae9555727d6bc6c68b4248"
_CAMPAIGN_RUN_ID = "rob974h6a-wbSyHLi2OCMA167TwGlSF70ZIXd98KCRUJ88OaQG-Zo"
_INTEGRATION_HEAD = "c3c31b76e3a79e9cf9573e066b1d7e278088fc8e"
_INTEGRATION_TREE = "bc8091c50e720af86b610332714d077e7b461397"


def test_r1_closure_surface_is_explicit_and_identity_stays_pinned() -> None:
    assert getattr(materializer, "ROB984_CP10_CLOSED_PREFIX", None) == (
        "CLOSED_BY_ROB984_CP10:sha256:"
    )
    reject_absent = getattr(
        materializer, "_require_rob984_cp10_execution_evidence", None
    )
    assert callable(reject_absent), (
        "R1 requires CLOSED markers to reject absent E2E execution evidence"
    )
    with pytest.raises(materializer.H6BPlanError, match="fake-free execution evidence"):
        reject_absent(None)

    identity = materializer.build_production_identity_plan()
    assert identity.full_campaign_hash == _FULL_CAMPAIGN_HASH
    assert identity.campaign_run_id == _CAMPAIGN_RUN_ID
    runner_paths = {
        logical_path
        for logical_path, _path in materializer.h4_h6a_adapter.RUNNER_SOURCE_FILES
    }
    assert "research/nautilus_scalping/rob974_h4_smoke.py" not in runner_paths
    assert "app/services/rob974_h6b_materializer.py" in runner_paths
    engine_paths = {
        logical_path
        for logical_path, _path in materializer.h4_h6a_adapter.ENGINE_SOURCE_FILES
    }
    assert "research/nautilus_scalping/funding_oi_archive.py" in engine_paths
    assert "research/nautilus_scalping/rob944_gap_funding.py" in engine_paths


def test_r1_outer_rollback_unique_run_namespace_is_structurally_refused(
    tmp_path,
) -> None:
    """A fresh facility, not a forged run-id, is the reproducibility seam.

    Production plans are sealed to H4's canonical hash/run pair, experiment
    identities are globally unique, and trial idempotency/index constraints
    are experiment-scoped.  A proof-only run namespace would therefore need a
    new test-plan/attempt semantic contract, outside the approved R1 scope.
    """

    identity = materializer.build_production_identity_plan()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=(tmp_path / "rollback-proof").resolve(),
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    proof_hash = canonical_sha256(
        {
            "schema_version": "rob984.rollback_proof_namespace.v1",
            "production_full_campaign_hash": plan.full_campaign_hash,
            "namespace": "rollback-only",
        }
    )
    proof_run_id = materializer.derive_campaign_run_id(proof_hash)
    with pytest.raises(materializer.H6BPlanError, match="identity differs"):
        replace(
            plan,
            full_campaign_hash=proof_hash,
            campaign_run_id=proof_run_id,
        )

    experiment_constraints = {
        constraint.name
        for constraint in ResearchStrategyExperiment.__table__.constraints
    }
    trial_constraints = {
        constraint.name for constraint in ResearchBacktestRun.__table__.constraints
    }
    assert "uq_research_strategy_experiments_experiment_id" in experiment_constraints
    assert "uq_research_backtest_runs_experiment_trial_index" in trial_constraints
    assert "uq_research_backtest_runs_experiment_idempotency" in trial_constraints
