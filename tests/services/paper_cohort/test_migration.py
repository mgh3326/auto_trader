from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260714_rob849_paper_cohort.py"


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
        activated_at=datetime.now(UTC),
    )


def test_migration_descends_from_rob848_and_is_the_single_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "20260714_rob849_paper_cohort"' in source
    assert 'down_revision = "20260713_rob848_paper_validation"' in source

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    assert ScriptDirectory.from_config(config).get_heads() == [
        "20260714_rob849_paper_cohort"
    ]


def test_migration_defines_composition_and_immutable_triggers() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "validate_paper_cohort_composition" in source
    assert "reject_paper_cohort_audit_mutation" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source


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
