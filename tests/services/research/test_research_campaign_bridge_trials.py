"""ROB-946 (H6) — hardened trial idempotency + scenario evidence + campaign
completeness: RED-first coverage.

Covers ROB-946 §5/§6/§8/§9: independent 13/17/22bp scenario evidence, replay
idempotency hardening (exact-evidence replay vs fail-closed mismatch on BOTH
the sequential pre-check path AND the concurrent post-delegate path — R1
Critical-2), all 4 terminal statuses, explicit-retry next index, and the
campaign completeness report (24/23/25/missing/extra/mismatch/retry-gap, no
winner-only filter — R1 Important-3/4).
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
from app.services import research_campaign_bridge as bridge
from app.services import strategy_experiment_registry as reg
from app.services.research_campaign_bridge import (
    CampaignSpecCountError,
    RunnerNameTooLongError,
    TerminalEvidenceMismatch,
    campaign_completeness_report,
    record_attempt,
)
from app.services.research_db_write_guard import (
    ResearchDbPolicy,
    ResearchDbTarget,
    ResearchWriteDisabled,
)

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)


def _identity(
    config_id: str = "S1-00", strategy_key: str | None = None, **overrides
) -> StrategyExperimentIdentity:
    base = {
        "strategy_key": strategy_key or ("ROB946-TRIALS-TEST-" + uuid.uuid4().hex[:8]),
        "strategy_version": "v1",
        "hypothesis": "trial bridge test",
        "strategy": {"slug": "S1"},
        "code": {"source_sha256": "0" * 64},
        "params": {"config_id": config_id},
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


async def _register(
    session, spec: StrategyExperimentIdentity | None = None
) -> tuple[StrategyExperimentIdentity, str]:
    spec = spec or _identity()
    exp = await reg.register_experiment(session, spec)
    await session.flush()
    return spec, exp.experiment_id


async def _register_n(session, n: int) -> list[tuple[StrategyExperimentIdentity, str]]:
    out = []
    for i in range(n):
        spec = _identity(config_id=f"S1-{i:02d}")
        out.append(await _register(session, spec))
    return out


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
    _spec, experiment_id = await _register(session)
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
        guard_policy=_POLICY,
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
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-fixed")

    first = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    assert first.id == second.id
    assert second.trial_status == "completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_same_key_mismatched_status_fails_closed_on_precheck_path(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
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
        guard_policy=_POLICY,
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
            guard_policy=_POLICY,
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
    _spec, experiment_id = await _register(session)
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
        guard_policy=_POLICY,
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
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_different_evidence_fails_closed_on_postdelegate_path(
    registry_tables, monkeypatch
) -> None:
    """R1 Critical-2 reproduction/regression.

    Forces the EXACT race window: this caller's pre-check misses (simulating
    a concurrent writer's commit not yet visible), so it proceeds to delegate
    to the raw ``record_trial`` — which, for the SAME attempt key, resolves
    to the ALREADY-COMMITTED winner row. The post-delegate fingerprint check
    must catch the divergence even though the pre-check never did.
    """
    session = registry_tables
    _spec, experiment_id = await _register(session)
    key = AttemptKey(
        campaign_run_id="camp-race", experiment_id=experiment_id, retry_index=0
    )

    winner_evidence = _evidence(
        "camp-race", experiment_id, run_identity="run-winner"
    ).model_copy(update={"attempt_key": key})
    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=winner_evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    async def _always_miss(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge, "_find_trial_by_attempt_key", _always_miss)

    loser_evidence = _evidence(
        "camp-race",
        experiment_id,
        status="crashed",
        reason_code="oom",
        run_identity="run-loser",
        scenario_evidence=_scenario_evidence(base=0, primary=0, upward=0),
    ).model_copy(update={"attempt_key": key})

    with pytest.raises(TerminalEvidenceMismatch):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=loser_evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )

    # The winner's row must remain exactly as recorded — no overwrite.
    trials = await reg.list_trials(session, experiment_id)
    matching = [t for t in trials if t.trial_idempotency_key == key.idempotency_key()]
    assert len(matching) == 1
    assert matching[0].trial_status == "completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_same_key_identical_evidence_still_replays_on_postdelegate_path(
    registry_tables, monkeypatch
) -> None:
    """The post-delegate check must NOT false-positive when the race winner's
    evidence is actually identical to this caller's own (a true duplicate
    concurrent submission of the SAME result)."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    key = AttemptKey(
        campaign_run_id="camp-race2", experiment_id=experiment_id, retry_index=0
    )
    evidence = _evidence(
        "camp-race2", experiment_id, run_identity="run-same"
    ).model_copy(update={"attempt_key": key})

    first = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    async def _always_miss(*args, **kwargs):
        return None

    monkeypatch.setattr(bridge, "_find_trial_by_attempt_key", _always_miss)

    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert second.id == first.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explicit_retry_uses_new_key_and_next_monotonic_trial_index(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)

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
        guard_policy=_POLICY,
    )
    retry = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=_evidence("camp1", experiment_id, retry_index=1, status="completed"),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    assert retry.trial_index == first.trial_index + 1
    assert retry.trial_idempotency_key != first.trial_idempotency_key


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_four_terminal_statuses_recorded(registry_tables) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)

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
            guard_policy=_POLICY,
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
    _spec, experiment_id = await _register(session)
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
            guard_policy=_POLICY,
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
    _spec, experiment_id = await _register(session)
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
            guard_policy=_POLICY,
        )

    after = await session.scalar(select(func.count()).select_from(ResearchBacktestRun))
    assert after == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_disabled_rejects_attempt_recording_before_any_write(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
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
            guard_policy=_POLICY,
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
        _spec, experiment_id = await _register(setup)
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
                guard_policy=_POLICY,
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_complete_when_all_24_have_a_primary_terminal_status(
    registry_tables,
) -> None:
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    statuses = ["completed", "rejected", "crashed", "timeout"]
    for i, (_spec, experiment_id) in enumerate(registrations):
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
            guard_policy=_POLICY,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-complete", expected_specs=specs
    )

    assert report.verdict == "complete"
    assert report.expected_total == 24
    assert report.actual_registrations == 24
    assert report.primary_attempts == 24
    assert report.total_attempts == 24
    assert report.retry_attempts == 0
    assert sum(report.status_counts.values()) == 24
    assert report.missing_experiment_ids == []
    assert report.extra_experiment_ids == []
    assert report.mismatch_experiment_ids == []
    assert report.duplicate_or_gap_experiment_ids == []
    # No winner-only filter: an all-crashed/rejected/timeout campaign is still
    # "complete" evidence — completed != PASS.
    assert report.status_counts["crashed"] >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_incomplete_when_one_experiment_has_no_attempt(
    registry_tables,
) -> None:
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    for _spec, experiment_id in registrations[:-1]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-missing", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-missing", expected_specs=specs
    )

    assert report.verdict == "incomplete"
    assert report.missing_experiment_ids == [registrations[-1][1]]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_rejects_wrong_expected_count_23(registry_tables) -> None:
    session = registry_tables
    registrations = await _register_n(session, 23)
    specs = [spec for spec, _eid in registrations]
    with pytest.raises(CampaignSpecCountError):
        await campaign_completeness_report(
            session, campaign_run_id="camp-23", expected_specs=specs
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_rejects_wrong_expected_count_25(registry_tables) -> None:
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    with pytest.raises(CampaignSpecCountError):
        await campaign_completeness_report(
            session, campaign_run_id="camp-25", expected_specs=[*specs, specs[0]]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_rejects_duplicate_expected_specs(registry_tables) -> None:
    # 23 distinct + 1 EXACT duplicate of the first -- still 24 items in length
    # but only 23 unique identities; must fail closed (never silently treat
    # the duplicate as covering a 24th slot).
    session = registry_tables
    registrations = await _register_n(session, 23)
    specs = [spec for spec, _eid in registrations]
    with pytest.raises(bridge.CampaignDuplicateSpecError):
        await campaign_completeness_report(
            session, campaign_run_id="camp-dup", expected_specs=[*specs, specs[0]]
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_distinguishes_retry_from_duplicate(registry_tables) -> None:
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    _retried_spec, retried_id = registrations[0]

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
        guard_policy=_POLICY,
    )
    await record_attempt(
        session,
        experiment_id=retried_id,
        evidence=_evidence("camp-retry", retried_id, retry_index=1, status="completed"),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    for _spec, experiment_id in registrations[1:]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-retry", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-retry", expected_specs=specs
    )

    assert report.verdict == "complete"
    assert report.duplicate_or_gap_experiment_ids == []
    assert report.primary_attempts == 24
    # both the retried crash AND the eventual completion are counted — 25 rows
    # total across 24 experiments, not treated as a duplicate.
    assert report.total_attempts == 25
    assert report.retry_attempts == 1
    assert sum(report.status_counts.values()) == 25


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_incomplete_when_only_retry_1_exists_no_primary(
    registry_tables,
) -> None:
    """R1 Important-3 reproduction/regression: an experiment with ONLY a
    retry_index=1 attempt (no retry_index=0 primary) must be treated as
    missing, never as satisfying completeness."""
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    gapped_spec, gapped_id = registrations[0]

    for _spec, experiment_id in registrations[1:]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-noprimary", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    await record_attempt(
        session,
        experiment_id=gapped_id,
        evidence=_evidence(
            "camp-noprimary", gapped_id, retry_index=1, status="completed"
        ),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-noprimary", expected_specs=specs
    )
    assert report.verdict == "incomplete"
    assert gapped_id in report.missing_experiment_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_reports_retry_gap_as_incomplete(registry_tables) -> None:
    """retry_index 0 and 2 exist, 1 is missing -- a genuinely reachable gap
    (distinct from the unreachable same-index-duplicate case)."""
    session = registry_tables
    registrations = await _register_n(session, 24)
    specs = [spec for spec, _eid in registrations]
    gapped_spec, gapped_id = registrations[0]

    for _spec, experiment_id in registrations[1:]:
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=_evidence("camp-gap", experiment_id),
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    await record_attempt(
        session,
        experiment_id=gapped_id,
        evidence=_evidence(
            "camp-gap", gapped_id, retry_index=0, status="crashed", reason_code="oom"
        ),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await record_attempt(
        session,
        experiment_id=gapped_id,
        evidence=_evidence("camp-gap", gapped_id, retry_index=2, status="completed"),
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-gap", expected_specs=specs
    )
    assert report.verdict == "incomplete"
    assert gapped_id in report.duplicate_or_gap_experiment_ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_surfaces_extra_registration(registry_tables) -> None:
    """R1 Important-4 reproduction/regression: an unexpected 25th registered
    experiment (same strategy_key scope) must surface as `extra`, not be
    silently invisible."""
    session = registry_tables
    common_key = "ROB946-I4-SHARED-" + uuid.uuid4().hex[:8]
    registrations = []
    for i in range(24):
        spec = _identity(config_id=f"S1-{i:02d}", strategy_key=common_key)
        registrations.append(await _register(session, spec))
    specs = [spec for spec, _eid in registrations]

    stray_spec = _identity(config_id="S1-99", strategy_key=common_key)
    stray_spec, stray_id = await _register(session, stray_spec)

    report = await campaign_completeness_report(
        session, campaign_run_id="camp-extra", expected_specs=specs
    )
    assert stray_id in report.extra_experiment_ids
    assert report.verdict == "incomplete"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completeness_surfaces_identity_component_drift_as_mismatch(
    registry_tables,
) -> None:
    """R1 Important-4 reproduction/regression: the SAME config_id slot (same
    params_hash) is registered, but under a DIFFERENT overall identity (some
    other component drifted) — must surface as `mismatch`, not `missing`."""
    session = registry_tables
    common_key = "ROB946-I4-DRIFT-" + uuid.uuid4().hex[:8]
    expected_spec = _identity(config_id="S1-00", strategy_key=common_key)

    # What actually got registered has the SAME params (same params_hash,
    # same logical slot) but a DIFFERENT frozen_config -- a drifted identity.
    drifted_spec = _identity(
        config_id="S1-00",
        strategy_key=common_key,
        frozen_config={"timeframe": "5m"},  # differs from expected_spec's "15m"
    )
    _spec, drifted_id = await _register(session, drifted_spec)

    other_specs = [
        _identity(config_id=f"S1-{i:02d}", strategy_key=common_key)
        for i in range(1, 24)
    ]
    for spec in other_specs:
        await _register(session, spec)

    from app.services.research_canonical_hash import (
        compute_identity_hashes,
        derive_experiment_id,
    )

    expected_id = derive_experiment_id(
        expected_spec.strategy_key,
        expected_spec.strategy_version,
        compute_identity_hashes(expected_spec.components()),
    )
    assert expected_id != drifted_id  # the drift IS the point

    report = await campaign_completeness_report(
        session,
        campaign_run_id="camp-drift",
        expected_specs=[expected_spec, *other_specs],
    )
    # mismatch_experiment_ids names the EXPECTED slot whose registration
    # drifted (symmetric with missing_experiment_ids), not the actual
    # drifted row's own id.
    assert expected_id in report.mismatch_experiment_ids
    assert expected_id not in report.missing_experiment_ids
    assert report.verdict == "incomplete"
