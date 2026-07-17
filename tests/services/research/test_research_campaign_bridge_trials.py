"""ROB-946 (H6) — hardened trial idempotency + scenario evidence + campaign
completeness: RED-first coverage.

Covers ROB-946 §5/§6/§8: independent 13/17/22bp scenario evidence, replay
idempotency hardening (exact-evidence replay vs fail-closed mismatch), all 4
terminal statuses, explicit-retry next index, and the campaign completeness
report (24/23/25/missing/duplicate/retry, no winner-only filter).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.research_backtest import ResearchBacktestRun
from app.schemas.research_backtest import StrategyExperimentIdentity
from app.schemas.research_campaign_bridge import (
    AttemptEvidence,
    AttemptKey,
    ScenarioEvidence,
)
from app.services import strategy_experiment_registry as reg
from app.services.research_campaign_bridge import (
    CampaignSpecCountError,
    RunnerNameTooLongError,
    TerminalEvidenceMismatch,
    campaign_completeness_report,
    record_attempt,
)
from app.services.research_db_write_guard import ResearchWriteDisabled

_ALLOWLIST = frozenset({"test_db"})


def _identity(**overrides) -> StrategyExperimentIdentity:
    base = {
        "strategy_key": "ROB946-TRIALS-TEST-" + uuid.uuid4().hex[:8],
        "strategy_version": "v1",
        "hypothesis": "trial bridge test",
        "strategy": {"slug": "S1"},
        "code": {"source_sha256": "0" * 64},
        "params": {"config_id": "S1-00"},
        "dataset_manifest": {"corpus": "fixture"},
        "universe": {"symbols": ["XRPUSDT"]},
        "pit": {"window": "fixture"},
        "frozen_config": {"timeframe": "15m"},
        "policy": {"selection": "fixture"},
        "benchmark": {},
        "cost": {"primary_stress": 17.0},
        "mdd": {"role": "report_only"},
    }
    base.update(overrides)
    return StrategyExperimentIdentity(**base)


def _scenario_evidence(base=3, primary=3, upward=2) -> tuple:
    return (
        ScenarioEvidence(
            scenario_name="base", trade_count=base, artifact_hash="h-base"
        ),
        ScenarioEvidence(
            scenario_name="primary_stress",
            trade_count=primary,
            artifact_hash="h-primary",
        ),
        ScenarioEvidence(
            scenario_name="upward_stress", trade_count=upward, artifact_hash="h-upward"
        ),
    )


def _evidence(
    campaign_run_id: str,
    experiment_id: str,
    retry_index: int = 0,
    status: str = "completed",
    reason_code: str | None = None,
    scenario_evidence=None,
    run_identity: str | None = None,
    fold_evidence_hash: str | None = "fold-hash-1",
) -> AttemptEvidence:
    return AttemptEvidence(
        attempt_key=AttemptKey(
            campaign_run_id=campaign_run_id,
            experiment_id=experiment_id,
            retry_index=retry_index,
        ),
        status=status,
        reason_code=reason_code if status != "completed" else reason_code,
        fold_evidence_hash=fold_evidence_hash,
        run_identity=run_identity or f"run-{uuid.uuid4().hex[:8]}",
        scenario_evidence=scenario_evidence or _scenario_evidence(),
    )


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


async def _register(session) -> str:
    exp = await reg.register_experiment(session, _identity())
    await session.flush()
    return exp.experiment_id


# --------------------------------------------------------------------------- #
# AttemptEvidence schema validation (pure, no DB)                             #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_reason_code_required_for_non_completed_status() -> None:
    with pytest.raises(ValidationError):
        _evidence("camp1", "e" * 64, status="rejected", reason_code=None)


@pytest.mark.unit
def test_scenario_evidence_must_cover_exactly_three_named_scenarios() -> None:
    only_two = (
        ScenarioEvidence(scenario_name="base", trade_count=1),
        ScenarioEvidence(scenario_name="primary_stress", trade_count=1),
    )
    with pytest.raises(ValidationError):
        AttemptEvidence(
            attempt_key=AttemptKey(
                campaign_run_id="c", experiment_id="e" * 64, retry_index=0
            ),
            status="completed",
            run_identity="run-1",
            scenario_evidence=only_two,
        )


@pytest.mark.unit
def test_duplicate_scenario_name_rejected() -> None:
    duped = (
        ScenarioEvidence(scenario_name="base", trade_count=1),
        ScenarioEvidence(scenario_name="base", trade_count=2),
        ScenarioEvidence(scenario_name="upward_stress", trade_count=1),
    )
    with pytest.raises(ValidationError):
        AttemptEvidence(
            attempt_key=AttemptKey(
                campaign_run_id="c", experiment_id="e" * 64, retry_index=0
            ),
            status="completed",
            run_identity="run-1",
            scenario_evidence=duped,
        )


# --------------------------------------------------------------------------- #
# Independent 13/17/22bp scenario evidence preservation                       #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_divergent_scenario_trade_counts_are_preserved_not_collapsed(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    evidence = _evidence(
        "camp1",
        experiment_id,
        scenario_evidence=_scenario_evidence(base=3, primary=3, upward=2),
    )

    row = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )

    stored = row.raw_payload["scenario_evidence"]
    by_name = {s["scenario_name"]: s for s in stored}
    assert by_name["base"]["trade_count"] == 3
    assert by_name["primary_stress"]["trade_count"] == 3
    assert by_name["upward_stress"]["trade_count"] == 2
    assert len(by_name) == 3  # no collapse into a single reference count


# --------------------------------------------------------------------------- #
# Idempotency hardening: exact replay vs fail-closed mismatch                 #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_identical_evidence_replays_the_original_row(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-fixed")

    first = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )

    assert first.id == second.id
    assert second.trial_status == "completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_mismatched_status_fails_closed(registry_tables) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    key = AttemptKey(
        campaign_run_id="camp1", experiment_id=experiment_id, retry_index=0
    )
    original = _evidence("camp1", experiment_id, run_identity="run-fixed")
    original = original.model_copy(update={"attempt_key": key})

    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=original,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )

    mismatched = _evidence(
        "camp1",
        experiment_id,
        status="crashed",
        reason_code="boom",
        run_identity="run-fixed",
    ).model_copy(update={"attempt_key": key})

    with pytest.raises(TerminalEvidenceMismatch):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=mismatched,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    trials = await reg.list_trials(session, experiment_id)
    matching = [t for t in trials if t.trial_idempotency_key == key.idempotency_key()]
    assert len(matching) == 1
    assert matching[0].trial_status == "completed"  # original untouched


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_mismatched_scenario_trade_count_fails_closed(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    key = AttemptKey(
        campaign_run_id="camp1", experiment_id=experiment_id, retry_index=0
    )
    original = _evidence("camp1", experiment_id, run_identity="run-fixed").model_copy(
        update={"attempt_key": key}
    )
    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=original,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )

    diverged = _evidence(
        "camp1",
        experiment_id,
        run_identity="run-fixed",
        scenario_evidence=_scenario_evidence(base=3, primary=3, upward=99),
    ).model_copy(update={"attempt_key": key})

    with pytest.raises(TerminalEvidenceMismatch):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=diverged,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_retry_uses_new_key_and_next_monotonic_trial_index(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)

    first = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=_evidence(
            "camp1", experiment_id, retry_index=0, status="crashed", reason_code="oom"
        ),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )
    retry = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=_evidence("camp1", experiment_id, retry_index=1, status="completed"),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )

    assert retry.trial_index == first.trial_index + 1
    assert retry.trial_idempotency_key != first.trial_idempotency_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_four_terminal_statuses_recorded(registry_tables) -> None:
    session = registry_tables
    experiment_id = await _register(session)

    statuses = [
        ("completed", None),
        ("rejected", "insufficient_symbol_evidence"),
        ("crashed", "boom"),
        ("timeout", "budget_exceeded"),
    ]
    for retry_index, (status, reason) in enumerate(statuses):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence(
                "camp1",
                experiment_id,
                retry_index=retry_index,
                status=status,
                reason_code=reason,
            ),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    accounting = await reg.get_trial_accounting(session, experiment_id)
    assert accounting.outcome_counts == {
        "completed": 1,
        "rejected": 1,
        "crashed": 1,
        "timeout": 1,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stable_reason_codes_survive_round_trip_unmutated(
    registry_tables,
) -> None:
    # ROB-946 §5/§8: these exact strings must never be paraphrased/altered.
    session = registry_tables
    experiment_id = await _register(session)
    reason_codes = [
        "insufficient_symbol_evidence",
        "rejected:insufficient_train_evidence",
        "rejected:data_gap_in_position",
    ]
    for retry_index, reason_code in enumerate(reason_codes):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence(
                "camp-reason",
                experiment_id,
                retry_index=retry_index,
                status="rejected",
                reason_code=reason_code,
            ),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    trials = await reg.list_trials(session, experiment_id)
    stored_reasons = {
        t.raw_payload["reason_code"]
        for t in trials
        if t.trial_idempotency_key
        and t.trial_idempotency_key.startswith("camp-reason:")
    }
    assert stored_reasons == set(reason_codes)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_name_over_16_chars_rejected_before_any_write(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    before = await session.scalar(select(func.count()).select_from(ResearchBacktestRun))

    with pytest.raises(RunnerNameTooLongError):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp1", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="this-runner-name-is-way-too-long",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    after = await session.scalar(select(func.count()).select_from(ResearchBacktestRun))
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_disabled_rejects_attempt_recording_before_any_write(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_id = await _register(session)
    before = await session.scalar(select(func.count()).select_from(ResearchBacktestRun))

    with pytest.raises(ResearchWriteDisabled):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp1", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=False,
            guard_allowlist=_ALLOWLIST,
        )

    after = await session.scalar(select(func.count()).select_from(ResearchBacktestRun))
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_identical_replay_never_creates_a_duplicate_row(
    registry_tables,
) -> None:
    from app.core.db import engine

    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_maker() as setup:
        experiment_id = await _register(setup)
        await setup.commit()

    evidence = _evidence("camp1", experiment_id, run_identity="run-fixed")

    async def worker() -> int:
        async with session_maker() as s:
            row = await record_attempt(
                s,
                experiment_id=experiment_id,
                evidence=evidence,
                strategy_name="S1",
                timeframe="15m",
                runner="pytest",
                guard_opt_in_enabled=True,
                guard_allowlist=_ALLOWLIST,
            )
            index = row.trial_index
            await s.commit()
            return index

    left, right = await asyncio.gather(worker(), worker())
    assert left == right

    async with session_maker() as check:
        count = await check.scalar(
            select(func.count())
            .select_from(ResearchBacktestRun)
            .where(
                ResearchBacktestRun.trial_idempotency_key
                == evidence.attempt_key.idempotency_key()
            )
        )
    assert count == 1


# --------------------------------------------------------------------------- #
# Campaign completeness report                                                #
# --------------------------------------------------------------------------- #


async def _register_n(session, n: int) -> list[str]:
    ids = []
    for _ in range(n):
        exp = await reg.register_experiment(session, _identity())
        await session.flush()
        ids.append(exp.experiment_id)
    return ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_complete_when_all_24_have_any_terminal_status(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_ids = await _register_n(session, 24)
    statuses = ["completed", "rejected", "crashed", "timeout"]
    for i, experiment_id in enumerate(experiment_ids):
        status = statuses[i % 4]
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence(
                "camp-complete",
                experiment_id,
                status=status,
                reason_code=None if status == "completed" else "reason",
            ),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-complete", expected_experiment_ids=experiment_ids
    )

    assert report.verdict == "complete"
    assert report.expected_total == 24
    assert report.experiments_with_attempts == 24
    assert sum(report.status_counts.values()) == 24
    assert report.missing_experiment_ids == []
    assert report.duplicate_logical_attempts == []
    # No winner-only filter: an all-crashed/rejected/timeout campaign is still
    # "complete" evidence — completed != PASS.
    assert report.status_counts["crashed"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_incomplete_when_one_experiment_has_no_attempt(
    registry_tables,
) -> None:
    session = registry_tables
    experiment_ids = await _register_n(session, 24)
    for experiment_id in experiment_ids[:-1]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-missing", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-missing", expected_experiment_ids=experiment_ids
    )

    assert report.verdict == "incomplete"
    assert report.missing_experiment_ids == [experiment_ids[-1]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_rejects_wrong_expected_count_23(registry_tables) -> None:
    session = registry_tables
    experiment_ids = await _register_n(session, 23)
    with pytest.raises(CampaignSpecCountError):
        await campaign_completeness_report(
            session, campaign_run_id="camp-23", expected_experiment_ids=experiment_ids
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_rejects_wrong_expected_count_25(registry_tables) -> None:
    session = registry_tables
    experiment_ids = await _register_n(session, 24)
    with pytest.raises(CampaignSpecCountError):
        await campaign_completeness_report(
            session,
            campaign_run_id="camp-25",
            expected_experiment_ids=[*experiment_ids, experiment_ids[0]],
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_distinguishes_retry_from_duplicate(registry_tables) -> None:
    session = registry_tables
    experiment_ids = await _register_n(session, 24)
    retried_id = experiment_ids[0]

    await record_attempt(
        session,
        experiment_id=retried_id,
        evidence=_evidence(
            "camp-retry", retried_id, retry_index=0, status="crashed", reason_code="oom"
        ),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )
    await record_attempt(
        session,
        experiment_id=retried_id,
        evidence=_evidence("camp-retry", retried_id, retry_index=1, status="completed"),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_allowlist=_ALLOWLIST,
    )
    for experiment_id in experiment_ids[1:]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-retry", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_allowlist=_ALLOWLIST,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-retry", expected_experiment_ids=experiment_ids
    )

    assert report.verdict == "complete"
    assert report.duplicate_logical_attempts == []
    assert report.experiments_with_attempts == 24
    # both the retried crash AND the eventual completion are counted — 25 rows
    # total across 24 experiments, not treated as a duplicate.
    assert sum(report.status_counts.values()) == 25
