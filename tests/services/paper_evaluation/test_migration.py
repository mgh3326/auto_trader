from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
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
from app.core.db import AsyncSessionLocal
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
from app.services.paper_evaluation.service import PaperEvaluationService, _request_hash
from tests.services.paper_evaluation.conftest import make_evaluation_config
from tests.services.paper_evaluation.test_integration import make_evidence

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
        "20260717_rob920_alpaca_canceled"
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
        "uq_evaluation_epoch_id",
        "uq_evaluation_epoch_identity",
        "uq_evaluation_epoch_lineage",
        "uq_evaluation_epoch_start",
        "uq_evaluation_scorecard_evaluation_view",
        "uq_evaluation_verdict_evaluation",
        "uq_evaluation_verdict_full_identity",
        "uq_evaluation_verdict_idempotency",
        "uq_paper_cohort_assignment_evaluation_identity",
        "fk_evaluation_epoch_cohort",
        "fk_evaluation_epoch_assignment",
        "fk_evaluation_epoch_assignment_identity",
        "fk_evaluation_epoch_cohort_lineage",
        "fk_evaluation_epoch_prior_lineage",
        "fk_evaluation_epoch_config",
        "fk_evaluation_scorecard_epoch_identity",
        "fk_evaluation_scorecard_verdict_identity",
        "fk_evaluation_verdict_epoch_identity",
        "ck_evaluation_scorecard_view_currency_consistency",
        "ck_evaluation_verdict_status",
        "ck_evaluation_epoch_reset_reason",
        "ck_evaluation_epoch_prior_not_self",
        "validate_evaluation_completeness",
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
        assert "20260717_rob920_alpaca_canceled (head)" in current.stdout

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
    unique_config = make_evaluation_config(
        min_observations=int(nonce[:8], 16) + 1,
        min_fills=1,
        min_calendar_days=7,
        fill_timing="canonical_close",
    )
    base_evidence = make_evidence()
    evidence_template = replace(
        base_evidence,
        config=unique_config,
        epoch=base_evidence.epoch.model_copy(
            update={
                "config_hash": unique_config.config_hash(),
                "initial_equity": unique_config.initial_equity,
            }
        ),
    )
    config_hash = evidence_template.config.config_hash()
    experiment_hash = _hash(f"{nonce}:experiment")
    cohort_hash = _hash(nonce)
    epoch_id = f"epoch-{nonce}"
    assignment_id = f"assignment-{nonce}"
    validation_id = f"validation-{nonce}"
    experiment = ResearchStrategyExperiment(
        experiment_id=experiment_hash,
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:strategy"),
        code_hash=_hash(f"{nonce}:code"),
        params_hash=_hash(f"{nonce}:params"),
        dataset_manifest_hash=_hash(f"{nonce}:dataset"),
        universe_hash=_hash(f"{nonce}:universe"),
        pit_hash=_hash(f"{nonce}:pit"),
        frozen_config_hash=config_hash,
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
        cohort_hash=cohort_hash,
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
            assignment_id=assignment_id,
            cohort_id=cohort.cohort_id,
            ordinal=0,
            role="champion",
            validation_id=validation_id,
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
        config_hash=config_hash,
        schema_id="paper_evaluation_config.v1",
        formula_version="v1",
        currency_conversion_policy="none",
        payload=json.loads(evidence_template.config.model_dump_json()),
    )
    db_session.add(config)
    await db_session.flush()

    epoch = EvaluationEpoch(
        epoch_id=epoch_id,
        assignment_id=assignment_id,
        validation_id=validation_id,
        cohort_id=cohort.cohort_id,
        config_hash=config.config_hash,
        initial_equity={
            name.value: str(amount)
            for name, amount in evidence_template.config.initial_equity.items()
        },
        started_at=evidence_template.paper_window.start,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
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

    race_evidence = replace(
        evidence_template,
        epoch=evidence_template.epoch.model_copy(
            update={
                "epoch_id": epoch_id,
                "assignment_id": assignment_id,
                "validation_id": validation_id,
                "cohort_id": cohort.cohort_id,
                "config_hash": config_hash,
                "experiment_hash": experiment_hash,
                "cohort_hash": cohort_hash,
            }
        ),
        manifest_hash=_hash(f"{nonce}:race-manifest"),
    )
    bootstrap = PaperEvaluationService(AsyncMock())  # type: ignore[arg-type]
    bootstrap._find_existing = AsyncMock(return_value=None)  # type: ignore[method-assign]
    bootstrap._persist_evaluation = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **kwargs: kwargs["verdict"]
    )
    bootstrap._evidence_reader = AsyncMock()
    bootstrap._evidence_reader.load.return_value = race_evidence
    local_verdict = await bootstrap.evaluate(
        validation_id=validation_id,
        idempotency_key=f"race-{nonce}",
        evaluated_at=race_evidence.paper_window.end,
    )
    request_hash = _request_hash(race_evidence)
    barrier = asyncio.Barrier(2)

    async def contender(label: str):
        async with AsyncSessionLocal() as session, session.begin():
            unrelated_hash = _hash(f"{nonce}:unrelated:{label}")
            session.add(
                EvaluationConfig(
                    config_hash=unrelated_hash,
                    schema_id="paper_evaluation_config.v1",
                    formula_version="v1",
                    currency_conversion_policy="none",
                    payload={"label": label},
                )
            )
            await barrier.wait()
            return await PaperEvaluationService(session)._persist_evaluation(
                verdict=local_verdict.model_copy(
                    update={"reason_text": f"persisted candidate {label}"}
                ),
                evidence=race_evidence,
                idempotency_key=f"race-{nonce}",
                request_hash=request_hash,
            )

    first_result, second_result = await asyncio.gather(
        contender("first"), contender("second")
    )
    persisted_race = await db_session.scalar(
        select(EvaluationVerdict).where(
            EvaluationVerdict.epoch_id == epoch_id,
            EvaluationVerdict.idempotency_key == f"race-{nonce}",
        )
    )
    assert persisted_race is not None
    assert (
        first_result.reason_text
        == second_result.reason_text
        == persisted_race.verdict_payload["reason_text"]
    )
    assert (
        await db_session.scalar(
            select(func.count(EvaluationScorecard.id)).where(
                EvaluationScorecard.evaluation_id == persisted_race.evaluation_id
            )
        )
        == 3
    )
    assert (
        await db_session.scalar(
            select(func.count(EvaluationConfig.id)).where(
                EvaluationConfig.config_hash.in_(
                    (
                        _hash(f"{nonce}:unrelated:first"),
                        _hash(f"{nonce}:unrelated:second"),
                    )
                )
            )
        )
        == 2
    )

    incomplete_id = _hash(f"{nonce}:incomplete")
    async with AsyncSessionLocal() as incomplete_session:
        for view_name, currency in (
            ("binance_broker", "USDT"),
            ("alpaca_broker", "USD"),
        ):
            incomplete_session.add(
                EvaluationScorecard(
                    evaluation_id=incomplete_id,
                    epoch_id=epoch_id,
                    assignment_id=assignment_id,
                    config_hash=config_hash,
                    view_name=view_name,
                    currency=currency,
                    experiment_hash=experiment_hash,
                    cohort_hash=cohort_hash,
                    metrics={},
                )
            )
        incomplete_session.add(
            EvaluationVerdict(
                evaluation_id=incomplete_id,
                epoch_id=epoch_id,
                assignment_id=assignment_id,
                config_hash=config_hash,
                idempotency_key=f"incomplete-{nonce}",
                request_hash=_hash(f"{nonce}:incomplete-request"),
                verdict_status="insufficient_evidence",
                verdict_payload={},
                experiment_hash=experiment_hash,
                cohort_hash=cohort_hash,
            )
        )
        with pytest.raises(DBAPIError, match="exactly three scorecards"):
            await incomplete_session.commit()
        await incomplete_session.rollback()

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

    other_cohort = PaperValidationCohort(
        cohort_id=f"other-cohort-{nonce}",
        cohort_hash=_hash(f"{nonce}:other-cohort"),
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
    other_assignment = PaperValidationCohortAssignment(
        assignment_id=f"other-assignment-{nonce}",
        cohort_id=other_cohort.cohort_id,
        ordinal=0,
        role="champion",
        validation_id=f"other-validation-{nonce}",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        source_backtest_run_id=run.id,
        strategy_version_id=experiment.strategy_version,
        target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
        experiment_hash=experiment.experiment_id,
        strategy_hash=experiment.strategy_hash,
        config_hash=config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=_hash(f"{nonce}:other-input"),
    )
    db_session.add_all((other_cohort, other_assignment))
    await db_session.flush()
    with pytest.raises(DBAPIError, match="fk_evaluation_epoch_prior_lineage"):
        async with db_session.begin_nested():
            db_session.add(
                EvaluationEpoch(
                    epoch_id=f"other-epoch-{nonce}",
                    assignment_id=other_assignment.assignment_id,
                    validation_id=other_assignment.validation_id,
                    cohort_id=other_cohort.cohort_id,
                    config_hash=config_hash,
                    initial_equity=epoch.initial_equity,
                    started_at=epoch.started_at + timedelta(minutes=3),
                    reset_reason="account_reset",
                    prior_epoch_id=epoch.epoch_id,
                    experiment_hash=experiment_hash,
                    cohort_hash=other_cohort.cohort_hash,
                )
            )
            await db_session.flush()

    correct_identity = {
        "config_hash": epoch.config_hash,
        "experiment_hash": epoch.experiment_hash,
        "cohort_hash": epoch.cohort_hash,
    }
    for model, field in (
        (EvaluationScorecard, "config_hash"),
        (EvaluationScorecard, "experiment_hash"),
        (EvaluationScorecard, "cohort_hash"),
        (EvaluationVerdict, "config_hash"),
        (EvaluationVerdict, "experiment_hash"),
        (EvaluationVerdict, "cohort_hash"),
    ):
        identity = {
            **correct_identity,
            field: _hash(f"{nonce}:{model.__name__}:{field}"),
        }
        common = {
            "evaluation_id": _hash(f"{nonce}:{model.__name__}:{field}:evaluation"),
            "epoch_id": epoch.epoch_id,
            "assignment_id": epoch.assignment_id,
            **identity,
        }
        row = (
            EvaluationScorecard(
                **common,
                view_name="binance_broker",
                currency="USDT",
                metrics={},
            )
            if model is EvaluationScorecard
            else EvaluationVerdict(
                **common,
                idempotency_key=f"mismatch-{model.__name__}-{field}-{nonce}",
                request_hash=_hash(f"{nonce}:{model.__name__}:{field}:request"),
                verdict_status="insufficient_evidence",
                verdict_payload={},
            )
        )
        with pytest.raises(DBAPIError, match="epoch_identity"):
            async with db_session.begin_nested():
                db_session.add(row)
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
