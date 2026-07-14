from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260714_rob849_paper_cohort.py"
ROB870_MIGRATION = REPO / "alembic/versions/20260714_rob870_approval_batches.py"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cohort() -> PaperValidationCohort:
    nonce = uuid4().hex
    return PaperValidationCohort(
        cohort_id=f"cohort-{nonce}",
        cohort_hash=_hash(nonce),
        venues=["binance", "alpaca"],
        symbols=["BTCUSDT", "ETHUSDT"],
        market="spot",
        leverage="1",
        interval="1m",
        required_lookback=30,
        max_capture_skew_ms=5_000,
        max_ticker_age_ms=5_000,
        capital_notional_usd="100",
        assignment_count=1,
        activated_at=datetime.now(UTC),
    )


def test_migration_descends_from_merged_rob870_and_is_the_single_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    rob870_source = ROB870_MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "20260714_rob849_paper_cohort"' in source
    assert 'down_revision = "20260714_rob870_approval_batches"' in source
    assert 'down_revision = "20260713_rob848_paper_validation"' in rob870_source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    lineage = {
        revision.revision for revision in script.iterate_revisions(heads[0], "base")
    }
    assert "20260714_rob849_paper_cohort" in lineage


def test_migration_defines_composition_and_immutable_triggers() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "validate_paper_cohort_composition" in source
    assert "reject_paper_cohort_audit_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source


def test_migration_defines_full_lineage_reservation_fence_and_claim_states() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "paper_cohort_target_reservations",
        "paper_cohort_terminal_fences",
        "fk_paper_cohort_decision_assignment_lineage",
        "fk_paper_cohort_decision_snapshot_lineage",
        "fk_paper_cohort_intent_decision_lineage",
        "fk_paper_run_order_link_intent_lineage",
        "fk_paper_cohort_target_reservation_intent_lineage",
        "ck_paper_run_order_link_venue_ledger",
        "ck_paper_cohort_run_claim_state_consistency",
        "ck_paper_cohort_terminal_fence_text_bounds",
        "reconciliation_required",
    ):
        assert required in source


@pytest.mark.asyncio
async def test_real_postgresql_upgrade_downgrade_upgrade_single_head() -> None:
    base_url = make_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-849 migration acceptance requires PostgreSQL")
    database = f"rob849_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        user=base_url.username,
        password=base_url.password,
        host=base_url.host,
        port=base_url.port,
        database="postgres",
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = base_url.set(database=database)
    target_url_text = target_url.render_as_string(hide_password=False)
    engine = create_async_engine(target_url_text)
    try:
        async with engine.begin() as connection:
            for schema in ("paper", "research", "review"):
                await connection.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                )
            await connection.run_sync(Base.metadata.create_all)
            # Base metadata represents the current application head. Rebuild
            # the ROB-849 boundary so later migrations are exercised instead
            # of colliding with tables that create_all already materialized.
            await connection.execute(
                text("DROP TABLE review.trade_retrospective_action_control")
            )
            await connection.execute(
                text("DROP TABLE review.trade_retrospective_actions")
            )

        env = {**os.environ, "DATABASE_URL": target_url_text}

        def alembic(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(REPO / ".venv/bin/alembic"), *args],
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        commands = (
            ("stamp", "20260714_rob849_paper_cohort"),
            ("downgrade", "20260713_rob848_paper_validation"),
            ("upgrade", "head"),
            ("downgrade", "20260713_rob848_paper_validation"),
            ("upgrade", "head"),
        )
        for command in commands:
            completed = await asyncio.to_thread(alembic, *command)
            assert completed.returncode == 0, completed.stdout + completed.stderr
        current = await asyncio.to_thread(alembic, "current")
        assert current.returncode == 0, current.stdout + current.stderr
        config = Config(str(REPO / "alembic.ini"))
        config.set_main_option("script_location", str(REPO / "alembic"))
        expected_head = ScriptDirectory.from_config(config).get_current_head()
        assert expected_head is not None
        assert f"{expected_head} (head)" in current.stdout

        async with engine.connect() as connection:
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS t "
                    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
                    "WHERE p.proname = 'reject_paper_cohort_audit_mutation' "
                    "AND NOT t.tgisinternal"
                )
            )
            assert triggers == 16
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()


@pytest.mark.asyncio
async def test_cohort_update_delete_and_truncate_are_rejected(
    db_session: AsyncSession,
) -> None:
    row = _cohort()
    nonce = uuid4().hex
    experiment = ResearchStrategyExperiment(
        experiment_id=_hash(f"{nonce}:experiment"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:strategy"),
        code_hash=_hash(f"{nonce}:code"),
        params_hash=_hash(f"{nonce}:params"),
        dataset_manifest_hash=_hash(f"{nonce}:dataset"),
        universe_hash=_hash(f"{nonce}:universe"),
        pit_hash=_hash(f"{nonce}:pit"),
        frozen_config_hash=_hash(f"{nonce}:config"),
        policy_hash=_hash(f"{nonce}:policy"),
        benchmark_hash=_hash(f"{nonce}:benchmark"),
        cost_hash=_hash(f"{nonce}:cost"),
        mdd_hash=_hash(f"{nonce}:mdd"),
        manifest={},
    )
    db_session.add(experiment)
    await db_session.flush()
    run = ResearchBacktestRun(
        run_id=f"run-{nonce}",
        strategy_name=experiment.strategy_key,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=1,
        profit_factor="1",
        max_drawdown="0",
        strategy_experiment_id=experiment.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"trial-{nonce}",
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(row)
    db_session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"assignment-{nonce}",
            cohort_id=row.cohort_id,
            ordinal=0,
            role="champion",
            validation_id=f"validation-{nonce}",
            validation_version=1,
            experiment_id=experiment.experiment_id,
            source_backtest_run_id=run.id,
            strategy_version_id=experiment.strategy_version,
            target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
            experiment_hash=experiment.experiment_id,
            strategy_hash=experiment.strategy_hash,
            config_hash=experiment.frozen_config_hash,
            policy_hash=experiment.policy_hash,
            input_hash=_hash(f"{nonce}:input"),
        )
    )
    await db_session.flush()
    cohort_pk = row.id
    cohort_id = row.cohort_id
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(PaperValidationCohort)
            .where(PaperValidationCohort.id == cohort_pk)
            .values(required_lookback=31)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(PaperValidationCohort).where(PaperValidationCohort.id == cohort_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.paper_validation_cohorts CASCADE")
        )
    await db_session.rollback()

    challenger = ResearchStrategyExperiment(
        experiment_id=_hash(f"{nonce}:challenger-experiment"),
        strategy_key=f"challenger-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:challenger-strategy"),
        code_hash=_hash(f"{nonce}:challenger-code"),
        params_hash=_hash(f"{nonce}:challenger-params"),
        dataset_manifest_hash=_hash(f"{nonce}:challenger-dataset"),
        universe_hash=_hash(f"{nonce}:challenger-universe"),
        pit_hash=_hash(f"{nonce}:challenger-pit"),
        frozen_config_hash=_hash(f"{nonce}:challenger-config"),
        policy_hash=_hash(f"{nonce}:challenger-policy"),
        benchmark_hash=_hash(f"{nonce}:challenger-benchmark"),
        cost_hash=_hash(f"{nonce}:challenger-cost"),
        mdd_hash=_hash(f"{nonce}:challenger-mdd"),
        manifest={},
    )
    db_session.add(challenger)
    await db_session.flush()
    challenger_run = ResearchBacktestRun(
        run_id=f"challenger-run-{nonce}",
        strategy_name=challenger.strategy_key,
        strategy_version=challenger.strategy_version,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=1,
        profit_factor="1",
        max_drawdown="0",
        strategy_experiment_id=challenger.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"challenger-trial-{nonce}",
    )
    db_session.add(challenger_run)
    await db_session.flush()
    db_session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"challenger-assignment-{nonce}",
            cohort_id=cohort_id,
            ordinal=1,
            role="challenger",
            validation_id=f"challenger-validation-{nonce}",
            validation_version=1,
            experiment_id=challenger.experiment_id,
            source_backtest_run_id=challenger_run.id,
            strategy_version_id=challenger.strategy_version,
            target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
            experiment_hash=challenger.experiment_id,
            strategy_hash=challenger.strategy_hash,
            config_hash=challenger.frozen_config_hash,
            policy_hash=challenger.policy_hash,
            input_hash=_hash(f"{nonce}:challenger-input"),
        )
    )
    with pytest.raises(DBAPIError, match="requires exactly one champion"):
        await db_session.commit()
    await db_session.rollback()
