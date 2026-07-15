from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.models.base import Base
from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_evaluation import (
    EvaluationConfig,
    EvaluationEpoch,
    EvaluationScorecard,
    EvaluationVerdict,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260714_rob850_paper_evaluation.py"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_migration_descends_from_latest_main_head_and_is_the_single_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "20260714_rob850_paper_evaluation"' in source
    assert 'down_revision = "20260714_rob878_shadow"' in source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260714_rob850_paper_evaluation"
    ]


def test_migration_defines_immutable_triggers() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "reject_evaluation_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source


def test_migration_defines_all_tables_and_key_constraints() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "evaluation_configs",
        "evaluation_epochs",
        "evaluation_scorecards",
        "evaluation_verdicts",
        "uq_evaluation_config_hash",
        "uq_evaluation_epoch_identity",
        "uq_evaluation_epoch_lineage",
        "uq_evaluation_epoch_start",
        "uq_evaluation_scorecard_evaluation_view",
        "uq_evaluation_verdict_evaluation",
        "uq_evaluation_verdict_idempotency",
        "fk_evaluation_epoch_cohort",
        "fk_evaluation_epoch_assignment",
        "fk_evaluation_epoch_cohort_lineage",
        "fk_evaluation_epoch_prior_lineage",
        "fk_evaluation_epoch_config",
        "fk_evaluation_scorecard_epoch_identity",
        "fk_evaluation_verdict_epoch_identity",
        "ck_evaluation_scorecard_view_currency_consistency",
        "ck_evaluation_verdict_status",
        "ck_evaluation_epoch_reset_reason",
        "ck_evaluation_epoch_prior_not_self",
    ):
        assert required in source


@pytest.mark.asyncio
async def test_real_postgresql_upgrade_downgrade_upgrade_single_head() -> None:
    base_url = make_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-850 migration acceptance requires PostgreSQL")
    database = f"rob850_migration_{uuid4().hex}"
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
            ("stamp", "20260714_rob850_paper_evaluation"),
            ("downgrade", "20260714_rob878_shadow"),
            ("upgrade", "head"),
            ("downgrade", "20260714_rob878_shadow"),
            ("upgrade", "head"),
        )
        for command in commands:
            completed = await asyncio.to_thread(alembic, *command)
            assert completed.returncode == 0, completed.stdout + completed.stderr
        current = await asyncio.to_thread(alembic, "current")
        assert current.returncode == 0, current.stdout + current.stderr
        assert "20260714_rob850_paper_evaluation (head)" in current.stdout

        async with engine.connect() as connection:
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger AS t "
                    "JOIN pg_proc AS p ON p.oid = t.tgfoid "
                    "WHERE p.proname = 'reject_evaluation_mutation' "
                    "AND NOT t.tgisinternal"
                )
            )
            assert triggers == 8
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
async def test_evaluation_tables_update_delete_and_truncate_are_rejected(
    db_session: AsyncSession,
) -> None:
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

    cohort = PaperValidationCohort(
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
    db_session.add(cohort)
    db_session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"assignment-{nonce}",
            cohort_id=cohort.cohort_id,
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

    config = EvaluationConfig(
        config_hash=experiment.frozen_config_hash,
        schema_id="paper_evaluation_config.v1",
        formula_version="v1",
        currency_conversion_policy="none",
        payload={"schema_id": "paper_evaluation_config.v1"},
    )
    db_session.add(config)
    await db_session.flush()

    epoch = EvaluationEpoch(
        epoch_id=f"epoch-{nonce}",
        assignment_id=f"assignment-{nonce}",
        validation_id=f"validation-{nonce}",
        cohort_id=cohort.cohort_id,
        config_hash=config.config_hash,
        initial_equity={
            "binance_broker": "10000",
            "alpaca_broker": "10000",
            "canonical_shadow": "10000",
        },
        started_at=datetime.now(UTC),
        experiment_hash=experiment.experiment_id,
        cohort_hash=cohort.cohort_hash,
    )
    db_session.add(epoch)
    await db_session.flush()

    scorecard = EvaluationScorecard(
        evaluation_id=_hash(f"{nonce}:evaluation"),
        epoch_id=epoch.epoch_id,
        assignment_id=epoch.assignment_id,
        config_hash=config.config_hash,
        view_name="binance_broker",
        currency="USDT",
        experiment_hash=epoch.experiment_hash,
        cohort_hash=epoch.cohort_hash,
        metrics={"net_return_pct": "0.05"},
    )
    db_session.add(scorecard)
    for view_name, currency in (
        ("alpaca_broker", "USD"),
        ("canonical_shadow", "USDT"),
    ):
        db_session.add(
            EvaluationScorecard(
                evaluation_id=scorecard.evaluation_id,
                epoch_id=epoch.epoch_id,
                assignment_id=epoch.assignment_id,
                config_hash=config.config_hash,
                view_name=view_name,
                currency=currency,
                experiment_hash=epoch.experiment_hash,
                cohort_hash=epoch.cohort_hash,
                metrics={"net_return_pct": "0.05"},
            )
        )

    verdict = EvaluationVerdict(
        evaluation_id=_hash(f"{nonce}:evaluation"),
        epoch_id=epoch.epoch_id,
        assignment_id=epoch.assignment_id,
        config_hash=config.config_hash,
        idempotency_key=f"idem-{nonce}",
        request_hash=_hash(f"{nonce}:request"),
        verdict_status="insufficient_evidence",
        verdict_payload={"status": "insufficient_evidence"},
        experiment_hash=epoch.experiment_hash,
        cohort_hash=epoch.cohort_hash,
    )
    db_session.add(verdict)
    second_evaluation_id = _hash(f"{nonce}:evaluation-day-7")
    for view_name, currency in (
        ("binance_broker", "USDT"),
        ("alpaca_broker", "USD"),
        ("canonical_shadow", "USDT"),
    ):
        db_session.add(
            EvaluationScorecard(
                evaluation_id=second_evaluation_id,
                epoch_id=epoch.epoch_id,
                assignment_id=epoch.assignment_id,
                config_hash=config.config_hash,
                view_name=view_name,
                currency=currency,
                experiment_hash=epoch.experiment_hash,
                cohort_hash=epoch.cohort_hash,
                metrics={"evaluation_day": 7},
            )
        )
    db_session.add(
        EvaluationVerdict(
            evaluation_id=second_evaluation_id,
            epoch_id=epoch.epoch_id,
            assignment_id=epoch.assignment_id,
            config_hash=config.config_hash,
            idempotency_key=f"idem-day-7-{nonce}",
            request_hash=_hash(f"{nonce}:request-day-7"),
            verdict_status="gate_blocked",
            verdict_payload={"status": "gate_blocked", "evaluation_day": 7},
            experiment_hash=epoch.experiment_hash,
            cohort_hash=epoch.cohort_hash,
        )
    )
    await db_session.flush()
    assert (
        await db_session.scalar(
            select(func.count(EvaluationScorecard.id)).where(
                EvaluationScorecard.epoch_id == epoch.epoch_id
            )
        )
        == 6
    )
    assert (
        await db_session.scalar(
            select(func.count(EvaluationVerdict.id)).where(
                EvaluationVerdict.epoch_id == epoch.epoch_id
            )
        )
        == 2
    )
    config_pk = config.id
    epoch_pk = epoch.id
    scorecard_pk = scorecard.id
    verdict_pk = verdict.id
    await db_session.commit()

    for suffix, prior_epoch_id in (
        ("self", f"epoch-{nonce}-self"),
        ("dangling", f"epoch-{nonce}-missing"),
    ):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                db_session.add(
                    EvaluationEpoch(
                        epoch_id=f"epoch-{nonce}-{suffix}",
                        assignment_id=epoch.assignment_id,
                        validation_id=epoch.validation_id,
                        cohort_id=epoch.cohort_id,
                        config_hash=epoch.config_hash,
                        initial_equity=epoch.initial_equity,
                        started_at=epoch.started_at
                        + timedelta(minutes=1 if suffix == "self" else 2),
                        reset_reason="account_reset",
                        prior_epoch_id=prior_epoch_id,
                        experiment_hash=epoch.experiment_hash,
                        cohort_hash=epoch.cohort_hash,
                    )
                )
                await db_session.flush()

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                EvaluationScorecard(
                    evaluation_id=_hash(f"{nonce}:mismatch-evaluation"),
                    epoch_id=epoch.epoch_id,
                    assignment_id=epoch.assignment_id,
                    config_hash=_hash(f"{nonce}:wrong-config"),
                    view_name="binance_broker",
                    currency="USDT",
                    experiment_hash=epoch.experiment_hash,
                    cohort_hash=epoch.cohort_hash,
                    metrics={},
                )
            )
            await db_session.flush()

    # --- evaluation_configs ---
    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(EvaluationConfig)
            .where(EvaluationConfig.id == config_pk)
            .values(formula_version="v2")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(EvaluationConfig).where(EvaluationConfig.id == config_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.evaluation_configs CASCADE")
        )
    await db_session.rollback()

    # --- evaluation_epochs ---
    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(EvaluationEpoch)
            .where(EvaluationEpoch.id == epoch_pk)
            .values(prior_epoch_id="changed")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(EvaluationEpoch).where(EvaluationEpoch.id == epoch_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.evaluation_epochs CASCADE")
        )
    await db_session.rollback()

    # --- evaluation_scorecards ---
    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(EvaluationScorecard)
            .where(EvaluationScorecard.id == scorecard_pk)
            .values(currency="USD")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(EvaluationScorecard).where(EvaluationScorecard.id == scorecard_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.evaluation_scorecards CASCADE")
        )
    await db_session.rollback()

    # --- evaluation_verdicts ---
    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(EvaluationVerdict)
            .where(EvaluationVerdict.id == verdict_pk)
            .values(verdict_status="promotion_eligible")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(EvaluationVerdict).where(EvaluationVerdict.id == verdict_pk)
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            text("TRUNCATE TABLE research.evaluation_verdicts CASCADE")
        )
    await db_session.rollback()
