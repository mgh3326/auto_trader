"""ROB-984 CP9 real disposable project-test-DB transaction proof."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from rob974_h6b_artifacts import DirectoryAtomicArtifactPort
from rob974_h6b_cli import build_production_identity_plan
from rob974_h6b_postaudit import (
    ProductionPostAuditAuthority,
    run_production_postaudit,
)
from rob984_fake_free_support import prepare_fake_free_input
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from test_rob984_cp8_actual_h5 import _actual_attempts, _actual_attribution, _pbo

from app.services import rob974_h6b_materializer as materializer
from app.services.research_db_write_guard import default_research_db_policy

_PROJECT_TEST_DB_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"
_INTEGRATION_HEAD = "c3c31b76e3a79e9cf9573e066b1d7e278088fc8e"
_INTEGRATION_TREE = "bc8091c50e720af86b610332714d077e7b461397"
_TARGET = materializer.DatabaseTarget(
    host="localhost", port=5432, database="test_db", user="postgres"
)
_PERSISTED_OUTPUT = Path("/private/tmp/strategy-worker-rob1025-test-db-e2e-pair")


class _HarnessSession:
    """Expose reviewed engine metadata while delegating real SQLAlchemy I/O."""

    def __init__(self, session: AsyncSession, engine: object) -> None:
        self._session = session
        self._engine = engine

    def get_bind(self) -> object:
        return self._engine

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)


def _actual_result(identity: materializer.ProductionIdentityPlan):
    attribution = _actual_attribution(identity)
    return materializer.ActualH4CampaignResult(
        identity=identity,
        attempts=_actual_attempts(identity, attribution),
        attribution=attribution,
        pbo=(_pbo("S3"), _pbo("S4")),
    )


class _ActualRunner:
    provenance = "actual_merged_h4"

    def __init__(self, result: materializer.ActualH4CampaignResult) -> None:
        self.result = result
        self.calls = 0

    async def run(
        self, identity: materializer.ProductionIdentityPlan
    ) -> materializer.ActualH4CampaignResult:
        assert identity is self.result.identity
        self.calls += 1
        return self.result


def test_cp9_exact_project_test_db_authority_surface_exists() -> None:
    authority_type = getattr(materializer, "ProjectTestDbAuthority", None)
    issuer = getattr(materializer, "issue_project_test_db_authorization", None)
    assert authority_type is not None and callable(issuer), (
        "CP9 requires an exact disposable test_db authority distinct from the "
        "rob974_db production gate"
    )


def test_cp9_persisted_attempt_parser_accepts_h6a_deep_frozen_mapping() -> None:
    identity = build_production_identity_plan()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=_PERSISTED_OUTPUT,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    item = _actual_result(identity).batch_items()[0]
    assert isinstance(item.evidence_payload, Mapping)
    assert type(item.evidence_payload) is not dict
    parsed = materializer.parse_persisted_attempt_record(item, plan=plan)
    assert parsed.row_id == item.row_id
    assert parsed.fold_evidence_hash == item.fold_evidence_hash
    assert parsed.run_identity == item.run_identity


@pytest.mark.asyncio
async def test_cp9_reviewed_test_db_is_exact_and_schema_ready_read_only() -> None:
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)
    assert materializer.DatabaseTarget(
        host=engine.url.host,
        port=engine.url.port,
        database=engine.url.database,
        user=engine.url.username,
    ) == materializer.DatabaseTarget(
        host="localhost", port=5432, database="test_db", user="postgres"
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                row = (
                    await connection.execute(
                        text(
                            "SELECT current_database(), current_user, "
                            "to_regclass('research.strategy_experiments'), "
                            "to_regclass('research.backtest_runs')"
                        )
                    )
                ).one()
                assert tuple(row) == (
                    "test_db",
                    "postgres",
                    "research.strategy_experiments",
                    "research.backtest_runs",
                )
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cp9_actual_48_writer_commit_is_contained_by_outer_rollback(
    tmp_path,
) -> None:
    identity = build_production_identity_plan()
    output = (tmp_path / "cp9-scorecard").resolve()
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=output,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    campaign = materializer.ProductionCampaignInput(
        plan=plan,
        guard_policy=default_research_db_policy(),
        strategy_name=materializer.ROB974_R2_PRODUCTION_STRATEGY_NAME,
        timeframe=materializer.ROB974_R2_PRODUCTION_TIMEFRAME,
        runner=materializer.ROB974_R2_PRODUCTION_RUNNER,
    )
    result = _actual_result(identity)
    runner = _ActualRunner(result)
    inspector = materializer.ActualCampaignStateInspector()
    artifacts = DirectoryAtomicArtifactPort()
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)
    try:
        async with engine.connect() as probe_connection:
            probe_session = AsyncSession(bind=probe_connection, expire_on_commit=False)
            await probe_session.begin()
            await probe_session.execute(text("SET TRANSACTION READ ONLY"))
            existing = await inspector.inspect(probe_session, plan=plan)
            await probe_session.rollback()
            await probe_session.close()
        if not existing.is_absent():
            assert existing.registered_mapping == plan.ordered_mapping
            assert len(existing.attempts) == 48
            pytest.skip(
                "ONE_TIME_FRESH_DISPOSABLE_DB_OBSERVATION: the exact committed "
                "campaign is immutable and canonical experiment/trial keys forbid "
                "a proof-only run-id namespace; orch must release a fresh approved "
                "whole test_db facility, run this outer-rollback proof first, then "
                "run the committed READ ONLY audit, and dispose only through the "
                "approved whole-facility lifecycle (no row cleanup)"
            )

        async with engine.connect() as connection:
            outer = await connection.begin()
            application_session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            harness_session = _HarnessSession(application_session, engine)
            ports = materializer.ProductionExecutionPorts(
                session_factory=lambda: harness_session,
                h4_runner=runner,
                artifacts=artifacts,
                state_inspector=inspector,
            )
            authorization = materializer.issue_project_test_db_authorization(
                plan,
                materializer.ProjectTestDbAuthority(
                    expected_full_campaign_hash=plan.full_campaign_hash,
                    expected_campaign_run_id=plan.campaign_run_id,
                    expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
                    expected_target=_TARGET,
                    observed_target=_TARGET,
                    inherited_target=_TARGET,
                    write_opt_in=True,
                    expected_output_root=output,
                    requested_output_root=output,
                    expected_source_pins=plan.source_pins,
                    observed_source_pins=plan.source_pins,
                    one_shot_approval="cp9-outer-rollback",
                ),
            )
            outcome = await materializer.materialize_production(
                plan=plan,
                authorization=authorization,
                campaign=campaign,
                ports=ports,
            )
            assert outcome.exit_code == 0, repr(outcome.primary_error)
            assert outcome.disposition == "MATERIALIZED"
            assert outcome.counters.commit == 1
            assert outcome.counters.rollback == 0
            assert outer.is_active is True
            assert runner.calls == 1

            inspection_session = AsyncSession(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            await inspection_session.begin()
            visible = await inspector.inspect(inspection_session, plan=plan)
            await inspection_session.rollback()
            await inspection_session.close()
            assert visible.campaign_run_id == plan.campaign_run_id
            assert visible.registered_mapping == plan.ordered_mapping
            assert len(visible.attempts) == 48
            accounting = ports.h6a_accounting.reconstruct(
                plan=plan,
                registered_total=len(visible.registered_mapping),
                attempts=visible.attempts,
            )
            assert accounting.registered_total == 48
            assert accounting.primary_attempts == accounting.total_attempts == 48
            assert accounting.retry_attempts == 0
            assert sum(accounting.status_counts.values()) == 48
            assert accounting.missing_row_ids == ()
            assert accounting.extra_experiment_ids == ()
            assert accounting.mismatch_row_ids == ()
            assert accounting.duplicate_or_gap_row_ids == ()
            assert accounting.trial_accounting_hash == (
                outcome.accounting.trial_accounting_hash
            )
            for item in visible.attempts:
                evidence = item.evidence_payload
                assert tuple(row["fold_id"] for row in evidence["unique_evidence"]) == (
                    "fold-00",
                    "fold-01",
                    "fold-02",
                    "fold-03",
                    "fold-04",
                    "fold-05",
                    "fold-06",
                    "fold-07",
                )
                assert tuple(
                    row["path_scenario"] for row in evidence["path_scenario_evidence"]
                ) == ("base13", "primary_stress17", "upward_stress22")

            await outer.rollback()

        async with engine.connect() as verification_connection:
            verification_session = AsyncSession(
                bind=verification_connection,
                expire_on_commit=False,
            )
            await verification_session.begin()
            await verification_session.execute(text("SET TRANSACTION READ ONLY"))
            residue = await inspector.inspect(verification_session, plan=plan)
            await verification_session.rollback()
            await verification_session.close()
            assert residue.is_absent()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cp9_actual_runner_commits_then_fresh_connection_audits_read_only(
    tmp_path,
) -> None:
    identity = build_production_identity_plan()
    assert identity.full_campaign_hash == (
        "2c47864c7ab661f16be6c414a1140944ec36832bb268e86183555b56c6f85f53"
    )
    assert identity.campaign_run_id == (
        "rob974h6a-CvcCOcAO3hRQDUPzHdVBJFmkXi_dN6NmngCOBLk82lI"
    )
    plan = materializer.build_production_execution_plan(
        identity=identity,
        output_root=_PERSISTED_OUTPUT,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    engine = create_async_engine(_PROJECT_TEST_DB_URL, poolclass=NullPool)

    def session_factory() -> AsyncSession:
        return AsyncSession(bind=engine, expire_on_commit=False)

    try:
        inspection_session = session_factory()
        await inspection_session.begin()
        await inspection_session.execute(text("SET TRANSACTION READ ONLY"))
        snapshot = await materializer.ActualCampaignStateInspector().inspect(
            inspection_session, plan=plan
        )
        await inspection_session.rollback()
        await inspection_session.close()
        artifact_state = (
            DirectoryAtomicArtifactPort().probe(output_dir=_PERSISTED_OUTPUT).state
        )

        if snapshot.is_absent() and artifact_state == "ABSENT":
            prepared = prepare_fake_free_input(tmp_path / "persisted-synthetic-corpus")
            campaign = materializer.ProductionCampaignInput(
                plan=plan,
                guard_policy=default_research_db_policy(),
                strategy_name=materializer.ROB974_R2_PRODUCTION_STRATEGY_NAME,
                timeframe=materializer.ROB974_R2_PRODUCTION_TIMEFRAME,
                runner=materializer.ROB974_R2_PRODUCTION_RUNNER,
            )
            ports = materializer.ProductionExecutionPorts(
                session_factory=session_factory,
                h4_runner=prepared.runner,
                artifacts=DirectoryAtomicArtifactPort(),
                state_inspector=materializer.ActualCampaignStateInspector(),
            )
            authorization = materializer.issue_project_test_db_authorization(
                plan,
                materializer.ProjectTestDbAuthority(
                    expected_full_campaign_hash=plan.full_campaign_hash,
                    expected_campaign_run_id=plan.campaign_run_id,
                    expected_exact_48_mapping_hash=plan.exact_48_mapping_hash,
                    expected_target=_TARGET,
                    observed_target=_TARGET,
                    inherited_target=_TARGET,
                    write_opt_in=True,
                    expected_output_root=_PERSISTED_OUTPUT,
                    requested_output_root=_PERSISTED_OUTPUT,
                    expected_source_pins=plan.source_pins,
                    observed_source_pins=plan.source_pins,
                    one_shot_approval="cp9-committed-fake-free-test-db",
                ),
            )
            outcome = await materializer.materialize_production(
                plan=plan,
                authorization=authorization,
                campaign=campaign,
                ports=ports,
            )
            assert outcome.exit_code == 0, repr(outcome.primary_error)
            assert outcome.disposition == "MATERIALIZED"
            assert outcome.counters.register == outcome.counters.record == 1
            assert outcome.counters.commit == outcome.counters.publish == 1
            assert outcome.counters.rollback == outcome.counters.delete == 0
            assert prepared.runner.last_result is not None
            assert len(prepared.runner.last_result.attempts) == 48
            assert {
                (row.strategy, row.config_count, row.day_count, row.slices)
                for row in prepared.runner.last_result.pbo
            } == {("S3", 24, 365, 4), ("S4", 24, 365, 4)}
        else:
            assert artifact_state == "PAIR_PRESENT"
            assert snapshot.campaign_run_id == plan.campaign_run_id
            assert snapshot.registered_mapping == plan.ordered_mapping
            assert len(snapshot.attempts) == 48

        audit = await run_production_postaudit(
            plan=plan,
            authority=ProductionPostAuditAuthority(
                expected_target=_TARGET,
                observed_target=_TARGET,
                inherited_target=_TARGET,
                output_dir=_PERSISTED_OUTPUT,
            ),
            session_factory=session_factory,
        )
        assert audit.exit_code == 0, repr(audit.primary_error)
        assert audit.disposition == "POSTAUDIT_VERIFIED_READ_ONLY"
        assert audit.trace[:5] == (
            "preflight",
            "session_factory",
            "begin",
            "set_transaction_read_only",
            "fetch_canonical_raw_rows",
        )
        assert audit.counters.read_only_statement == audit.counters.query == 1
        assert audit.counters.rollback == audit.counters.close == 1
        assert audit.seal is not None
        assert audit.seal.experiments == audit.seal.trials == 48
        assert audit.seal.strategy_counts == (("S3", 24), ("S4", 24))
        assert audit.seal.retry_attempts == 0
        assert sum(dict(audit.seal.status_counts).values()) == 48
    finally:
        await engine.dispose()
