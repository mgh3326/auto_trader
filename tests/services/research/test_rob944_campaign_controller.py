"""ROB-944 (H4, ROB-940) — thin --run preflight/registration controller.

Covers the H4 empirical --run fail-closed gate (RED/regression matrix item
11/12): identity/hash drift rejected BEFORE any DB call, deterministic
primary attempt-key recording with an explicit AttemptKey<->experiment_id
cross-check (captain audit item 4), and the full 24-registration + 24
primary-attempt + completeness pipeline -- all against the local disposable
test_db only, per the dispatch's "injected fakes/local disposable DB tests
only" instruction. No empirical --run, network, or broker/order/fill access
occurs anywhere in this file.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.schemas.research_campaign_bridge import (
    AttemptEvidence,
    AttemptKey,
    ScenarioEvidence,
)
from app.services import strategy_experiment_registry as reg
from app.services.research_campaign_bridge import (
    CampaignSpecCountError,
    campaign_completeness_report,
)
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget
from app.services.rob944_campaign_controller import (
    AttemptKeyExperimentMismatchError,
    CampaignBatchValidationError,
    CampaignHashDriftError,
    CampaignRunIdDerivationError,
    RunIdentityMismatchError,
    _derive_expected_campaign_run_id,
    _derive_expected_run_identity,
    _record_primary_attempt,
    _run_preflight_and_register,
    run_full_campaign,
)

_POLICY = ResearchDbPolicy.of(
    ResearchDbTarget(host="localhost", database_name="test_db")
)


def _hex64(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _identity(config_id: str, strategy_key: str) -> StrategyExperimentIdentity:
    return StrategyExperimentIdentity(
        strategy_key=strategy_key,
        strategy_version="v1",
        hypothesis="rob944 controller test",
        strategy={"slug": "S1"},
        code={"source_sha256": "0" * 64},
        params={"config_id": config_id},
        dataset_manifest={"corpus": "fixture"},
        universe={"symbols": ["XRPUSDT"]},
        pit={"window": "fixture"},
        frozen_config={"timeframe": "15m"},
        policy={"selection": "fixture"},
        benchmark={},
        cost={"primary_stress": 17.0},
        mdd={"role": "report_only"},
    )


def _campaign_specs(strategy_key: str) -> list[StrategyExperimentIdentity]:
    return [_identity(f"S1-{i:02d}", strategy_key) for i in range(24)]


def _scenario_evidence() -> tuple:
    return (
        ScenarioEvidence(
            scenario_name="base", trade_count=3, artifact_hash=_hex64("h-base")
        ),
        ScenarioEvidence(
            scenario_name="primary_stress",
            trade_count=3,
            artifact_hash=_hex64("h-primary"),
        ),
        ScenarioEvidence(
            scenario_name="upward_stress",
            trade_count=2,
            artifact_hash=_hex64("h-upward"),
        ),
    )


def _evidence(
    campaign_run_id: str, experiment_id: str, *, retry_index: int = 0
) -> AttemptEvidence:
    return AttemptEvidence(
        attempt_key=AttemptKey(
            campaign_run_id=campaign_run_id,
            experiment_id=experiment_id,
            retry_index=retry_index,
        ),
        status="completed",
        reason_code=None,
        fold_evidence_hash=_hex64("fold-hash-1"),
        run_identity=_hex64(f"run-{uuid.uuid4().hex[:8]}"),
        scenario_evidence=_scenario_evidence(),
    )


@pytest_asyncio.fixture
async def registry_tables(db_session):
    exists = await db_session.scalar(
        text("SELECT to_regclass('research.strategy_experiments')")
    )
    if exists is None:
        pytest.skip("ROB-846 registry tables are not migrated in this DB")
    return db_session


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hash_drift_is_rejected_before_any_db_call(registry_tables):
    session = registry_tables
    strategy_key = "ROB944-CTRL-DRIFT-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)

    with pytest.raises(CampaignHashDriftError):
        await _run_preflight_and_register(
            session,
            specs=specs,
            actual_full_campaign_hash="a" * 64,
            expected_full_campaign_hash="b" * 64,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )

    count = await session.scalar(
        reg.select(reg.ResearchStrategyExperiment).where(
            reg.ResearchStrategyExperiment.strategy_key == strategy_key
        )
    )
    assert count is None  # nothing was registered


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matching_hash_registers_all_24(registry_tables):
    session = registry_tables
    strategy_key = "ROB944-CTRL-MATCH-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)

    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="c" * 64,
        expected_full_campaign_hash="c" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    assert len(registered) == 24


@pytest.mark.integration
@pytest.mark.asyncio
async def test__record_primary_attempt_rejects_experiment_id_mismatch(registry_tables):
    session = registry_tables
    strategy_key = "ROB944-CTRL-EIDMISMATCH-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="d" * 64,
        expected_full_campaign_hash="d" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    campaign_run_id = "run-" + uuid.uuid4().hex[:8]
    real_id = registered[0].experiment_id
    wrong_id = registered[1].experiment_id
    evidence = _evidence(
        campaign_run_id, wrong_id
    )  # attempt_key names a DIFFERENT experiment

    with pytest.raises(AttemptKeyExperimentMismatchError):
        await _record_primary_attempt(
            session,
            experiment_id=real_id,
            campaign_run_id=campaign_run_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )

    trials = await reg.list_trials(session, real_id)
    assert trials == []  # nothing was recorded under the mismatched call


@pytest.mark.integration
@pytest.mark.asyncio
async def test__record_primary_attempt_rejects_campaign_run_id_mismatch(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-CTRL-RUNIDMISMATCH-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="e" * 64,
        expected_full_campaign_hash="e" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    exp_id = registered[0].experiment_id
    evidence = _evidence("run-A", exp_id)

    with pytest.raises(AttemptKeyExperimentMismatchError):
        await _record_primary_attempt(
            session,
            experiment_id=exp_id,
            campaign_run_id="run-B",
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    trials = await reg.list_trials(session, exp_id)
    assert trials == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test__record_primary_attempt_mismatch_errors_never_echo_raw_ids(
    registry_tables,
):
    """Captain delta sanitization pass (2026-07-17): neither the expected
    (this call's own params) NOR the evidence's own self-reported
    experiment_id/campaign_run_id may appear in the raised message -- field/
    count-only text throughout, matching every other validation error in
    this module."""
    session = registry_tables
    strategy_key = "ROB944-CTRL-SENTINELECHO-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="2" * 64,
        expected_full_campaign_hash="2" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    real_id = registered[0].experiment_id
    wrong_id = registered[1].experiment_id
    campaign_run_id = "run-sentinel-" + uuid.uuid4().hex[:8]
    evidence = _evidence(campaign_run_id, wrong_id)

    with pytest.raises(AttemptKeyExperimentMismatchError) as exc_info:
        await _record_primary_attempt(
            session,
            experiment_id=real_id,
            campaign_run_id=campaign_run_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    message = str(exc_info.value)
    assert real_id not in message
    assert wrong_id not in message
    assert campaign_run_id not in message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_pipeline_24_registrations_24_primary_attempts_reports_complete(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-CTRL-FULL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)

    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="f" * 64,
        expected_full_campaign_hash="f" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    campaign_run_id = "run-" + uuid.uuid4().hex[:8]

    for exp in registered:
        evidence = _evidence(campaign_run_id, exp.experiment_id)
        recorded = await _record_primary_attempt(
            session,
            experiment_id=exp.experiment_id,
            campaign_run_id=campaign_run_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
        assert recorded is not None
    await session.flush()

    report = await campaign_completeness_report(
        session, campaign_run_id=campaign_run_id, expected_specs=specs
    )
    assert report.verdict == "complete"
    assert report.actual_registrations == 24
    assert report.primary_attempts == 24
    assert report.missing_experiment_ids == []
    assert report.extra_experiment_ids == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test__record_primary_attempt_is_idempotent_on_identical_replay(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-CTRL-IDEMPOTENT-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    registered = await _run_preflight_and_register(
        session,
        specs=specs,
        actual_full_campaign_hash="1" * 64,
        expected_full_campaign_hash="1" * 64,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    exp_id = registered[0].experiment_id
    campaign_run_id = "run-" + uuid.uuid4().hex[:8]
    evidence = _evidence(campaign_run_id, exp_id)

    first = await _record_primary_attempt(
        session,
        experiment_id=exp_id,
        campaign_run_id=campaign_run_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    await session.flush()
    second = await _record_primary_attempt(
        session,
        experiment_id=exp_id,
        campaign_run_id=campaign_run_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert first.id == second.id  # same row, not a duplicate trial


# ---------------------------------------------------------------------------
# run_full_campaign -- captain blocking correction (2026-07-17): the full
# dependency-injected orchestration. Never a partial "24 registrations, no
# attempts" write path -- every registered identity gets exactly one
# deterministic retry_index=0 attempt recorded, and campaign_completeness_report
# is the final word. ``build_attempt_evidence`` is the sole injection point
# for the (research-side, never imported here) loader/walkforward/summarize
# pipeline -- this test file only ever supplies FAKE evidence.
# ---------------------------------------------------------------------------


def _full_campaign_evidence_factory(
    campaign_run_id, full_campaign_hash, *, status="completed", reason_code=None
):
    def _build(experiment_id_by_key):
        out = []
        fold_evidence_hash = _hex64("fold-hash-1")
        for (skey, config_id), exp_id in experiment_id_by_key.items():
            run_identity = _derive_expected_run_identity(
                full_campaign_hash=full_campaign_hash,
                campaign_run_id=campaign_run_id,
                strategy_key=skey,
                experiment_id=exp_id,
                retry_index=0,
                config_id=config_id,
                status=status,
                fold_evidence_hash=fold_evidence_hash,
            )
            out.append(
                AttemptEvidence(
                    attempt_key=AttemptKey(
                        campaign_run_id=campaign_run_id,
                        experiment_id=exp_id,
                        retry_index=0,
                    ),
                    status=status,
                    reason_code=reason_code,
                    fold_evidence_hash=fold_evidence_hash,
                    run_identity=run_identity,
                    scenario_evidence=_scenario_evidence(),
                )
            )
        return out

    return _build


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_hash_drift_prevents_any_registration_or_attempt(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-DRIFT-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = "run-" + uuid.uuid4().hex[:8]
    called = {"n": 0}

    def _boom(_experiment_id_by_key):
        called["n"] += 1
        raise AssertionError(
            "build_attempt_evidence must never be called on hash drift"
        )

    with pytest.raises(CampaignHashDriftError):
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="a" * 64,
            expected_full_campaign_hash="b" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_boom,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert called["n"] == 0
    row = await session.scalar(
        reg.select(reg.ResearchStrategyExperiment).where(
            reg.ResearchStrategyExperiment.strategy_key == strategy_key
        )
    )
    assert row is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_rejects_malformed_hash_format_before_any_registration(
    registry_tables,
):
    """Captain direct-controller identity gate: a malformed (non-hex64)
    hash must be rejected fail-closed BEFORE registration/child execution,
    independent of whatever the CLI already checked."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-MALFORMEDHASH-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    called = {"n": 0}

    def _boom(_experiment_id_by_key):
        called["n"] += 1
        raise AssertionError(
            "build_attempt_evidence must never be called on a malformed hash"
        )

    with pytest.raises(CampaignHashDriftError):
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="NOT-A-VALID-HEX64-HASH",
            expected_full_campaign_hash="NOT-A-VALID-HEX64-HASH",
            campaign_run_id="run-" + uuid.uuid4().hex[:8],
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_boom,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert called["n"] == 0
    row = await session.scalar(
        reg.select(reg.ResearchStrategyExperiment).where(
            reg.ResearchStrategyExperiment.strategy_key == strategy_key
        )
    )
    assert row is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_rejects_arbitrary_campaign_run_id_before_any_registration(
    registry_tables,
):
    """Captain direct-controller identity gate: even with a genuinely
    matching, well-formed hash pair, an arbitrary UUID/timestamp
    campaign_run_id (not the value canonically derived from the hash) must
    be rejected fail-closed BEFORE any registration/child execution."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-ARBITRARYRUNID-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    matching_hash = "9" * 64
    arbitrary_campaign_run_id = (
        "run-" + uuid.uuid4().hex[:8]
    )  # NOT derived from matching_hash
    assert arbitrary_campaign_run_id != _derive_expected_campaign_run_id(matching_hash)
    called = {"n": 0}

    def _boom(_experiment_id_by_key):
        called["n"] += 1
        raise AssertionError(
            "build_attempt_evidence must never be called on a bad campaign_run_id"
        )

    with pytest.raises(CampaignRunIdDerivationError):
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash=matching_hash,
            expected_full_campaign_hash=matching_hash,
            campaign_run_id=arbitrary_campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_boom,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert called["n"] == 0
    row = await session.scalar(
        reg.select(reg.ResearchStrategyExperiment).where(
            reg.ResearchStrategyExperiment.strategy_key == strategy_key
        )
    )
    assert row is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_run_id_derivation_error_never_echoes_either_campaign_run_id(
    registry_tables,
):
    """Captain P1-C sanitization precision (2026-07-17): neither the
    operator-supplied (arbitrary) campaign_run_id NOR the controller's own
    canonically-derived "expected" one may appear in
    CampaignRunIdDerivationError's message -- field/count-only, uniformly,
    even for the OUR-OWN trusted-derived value."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-RUNIDSENTINEL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    matching_hash = "4" * 64
    arbitrary_campaign_run_id = "run-sentinel-" + uuid.uuid4().hex[:8]
    expected_campaign_run_id = _derive_expected_campaign_run_id(matching_hash)
    assert arbitrary_campaign_run_id != expected_campaign_run_id

    def _boom(_experiment_id_by_key):
        raise AssertionError(
            "build_attempt_evidence must never be called on a bad campaign_run_id"
        )

    with pytest.raises(CampaignRunIdDerivationError) as exc_info:
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash=matching_hash,
            expected_full_campaign_hash=matching_hash,
            campaign_run_id=arbitrary_campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_boom,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    message = str(exc_info.value)
    assert arbitrary_campaign_run_id not in message
    assert expected_campaign_run_id not in message


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_batch_campaign_run_id_mismatch_never_echoes_ids(
    registry_tables,
):
    """Captain P1-C sanitization precision: ``_validate_attempt_batch``'s
    per-entry campaign_run_id check must never echo either the mismatched
    attempt's experiment_id or its (wrong) campaign_run_id."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-BATCHRUNIDSENTINEL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("5" * 64)
    wrong_campaign_run_id = "run-sentinel-wrong-" + uuid.uuid4().hex[:8]

    def _build(experiment_id_by_key):
        out = []
        fold_evidence_hash = _hex64("fold-hash-1")
        for i, ((skey, cfg), exp_id) in enumerate(experiment_id_by_key.items()):
            # Every attempt names the CORRECT campaign_run_id except one,
            # which claims a different (wrong) one.
            this_run_id = wrong_campaign_run_id if i == 0 else campaign_run_id
            run_identity = _derive_expected_run_identity(
                full_campaign_hash="5" * 64,
                campaign_run_id=campaign_run_id,
                strategy_key=skey,
                experiment_id=exp_id,
                retry_index=0,
                config_id=cfg,
                status="completed",
                fold_evidence_hash=fold_evidence_hash,
            )
            out.append(
                AttemptEvidence(
                    attempt_key=AttemptKey(
                        campaign_run_id=this_run_id, experiment_id=exp_id, retry_index=0
                    ),
                    status="completed",
                    reason_code=None,
                    fold_evidence_hash=fold_evidence_hash,
                    run_identity=run_identity,
                    scenario_evidence=_scenario_evidence(),
                )
            )
        return out

    with pytest.raises(CampaignBatchValidationError) as exc_info:
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="5" * 64,
            expected_full_campaign_hash="5" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_build,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    message = str(exc_info.value)
    assert wrong_campaign_run_id not in message
    assert campaign_run_id not in message
    report = await campaign_completeness_report(
        session, campaign_run_id=campaign_run_id, expected_specs=specs
    )
    assert report.primary_attempts == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_incomplete_verdict_message_never_echoes_injected_verdict_text(
    registry_tables, monkeypatch
):
    """Captain P1-C sanitization precision (2026-07-17): ``report.verdict``
    is ``Literal["complete", "incomplete"]`` at the schema layer, but this
    module's own message must never interpolate it verbatim regardless --
    uniform field-only text at this trust boundary even for values that
    happen to be closed-enum-safe. Proven by monkeypatching
    ``campaign_completeness_report`` (as bound into this controller module)
    to return an obviously sentinel-injected verdict string and asserting
    it never surfaces in the raised message."""
    import app.services.rob944_campaign_controller as controller_module

    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-VERDICTSENTINEL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("6" * 64)
    build_evidence = _full_campaign_evidence_factory(campaign_run_id, "6" * 64)
    sentinel_verdict = "SECRET-INJECTED-VERDICT-should-never-leak"

    class _FakeReport:
        verdict = sentinel_verdict

    async def _fake_completeness_report(*args, **kwargs):
        return _FakeReport()

    monkeypatch.setattr(
        controller_module, "campaign_completeness_report", _fake_completeness_report
    )

    with pytest.raises(controller_module.CampaignAccountingIncompleteError) as exc_info:
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="6" * 64,
            expected_full_campaign_hash="6" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=build_evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert sentinel_verdict not in str(exc_info.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_registration_failure_never_calls_build_attempt_evidence(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-REGFAIL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)[:23]  # wrong count -> CampaignSpecCountError
    campaign_run_id = _derive_expected_campaign_run_id("c" * 64)
    called = {"n": 0}

    def _boom(_experiment_id_by_key):
        called["n"] += 1
        raise AssertionError(
            "build_attempt_evidence must never be called after registration failure"
        )

    with pytest.raises(CampaignSpecCountError):
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="c" * 64,
            expected_full_campaign_hash="c" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_boom,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert called["n"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_rejects_sentinel_reason_code_never_records_and_never_leaks_into_str_exc(
    registry_tables,
):
    """Captain persistence-boundary correction: a reason_code outside the
    controller's OWN closed, status-scoped allowlist (e.g. a sentinel
    "SECRET" value a compromised/buggy build_attempt_evidence callback might
    smuggle in) must be rejected fail-closed by ``_validate_attempt_batch``
    -- BEFORE any ``record_attempt`` call for ANY of the 24 -- and the
    sentinel value itself must never appear in the raised exception's own
    message (which could be logged/printed/surfaced to an operator
    terminal)."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-SENTINEL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("7" * 64)
    sentinel = "SECRET-TOKEN-abc123"

    def _build(experiment_id_by_key):
        out = []
        for i, ((_skey, _cfg), exp_id) in enumerate(experiment_id_by_key.items()):
            # Every attempt is "completed" (reason_code=None) EXCEPT one,
            # which claims "crashed" with a sentinel reason_code outside the
            # closed allowlist for that status.
            if i == 0:
                status, reason = "crashed", sentinel
            else:
                status, reason = "completed", None
            out.append(
                AttemptEvidence(
                    attempt_key=AttemptKey(
                        campaign_run_id=campaign_run_id,
                        experiment_id=exp_id,
                        retry_index=0,
                    ),
                    status=status,
                    reason_code=reason,
                    fold_evidence_hash=_hex64("fold-hash-1"),
                    run_identity=_hex64(f"run-identity-{exp_id}"),
                    scenario_evidence=_scenario_evidence(),
                )
            )
        return out

    with pytest.raises(CampaignBatchValidationError) as exc_info:
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="7" * 64,
            expected_full_campaign_hash="7" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_build,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    assert sentinel not in str(exc_info.value)

    report = await campaign_completeness_report(
        session, campaign_run_id=campaign_run_id, expected_specs=specs
    )
    assert report.primary_attempts == 0  # no record_attempt call for ANY of the 24


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_rejects_forged_but_well_formed_run_identity(
    registry_tables,
):
    """Independent controller audit correction (2026-07-17): a run_identity
    that is a well-FORMED 64-hex string (passes _assert_hex64) but was not
    actually derived from the trusted lineage facts (a forged/tampered/
    arbitrary value) must still be rejected -- format alone is not enough.

    Captain P1-C sanitization precision: the raised RunIdentityMismatchError
    message must never interpolate experiment_id either, even though it is
    trusted-derived at this point in the batch loop -- field-only text."""
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-FORGEDRUNID-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("8" * 64)
    forged_exp_ids: list[str] = []

    def _build(experiment_id_by_key):
        out = []
        fold_evidence_hash = _hex64("fold-hash-1")
        for (_skey, _config_id), exp_id in experiment_id_by_key.items():
            forged_exp_ids.append(exp_id)
            out.append(
                AttemptEvidence(
                    attempt_key=AttemptKey(
                        campaign_run_id=campaign_run_id,
                        experiment_id=exp_id,
                        retry_index=0,
                    ),
                    status="completed",
                    reason_code=None,
                    fold_evidence_hash=fold_evidence_hash,
                    run_identity=_hex64(
                        f"forged-{exp_id}"
                    ),  # well-formed hex64, but NOT re-derivable
                    scenario_evidence=_scenario_evidence(),
                )
            )
        return out

    with pytest.raises(RunIdentityMismatchError) as exc_info:
        await run_full_campaign(
            session,
            specs=specs,
            actual_full_campaign_hash="8" * 64,
            expected_full_campaign_hash="8" * 64,
            campaign_run_id=campaign_run_id,
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
            build_attempt_evidence=_build,
            strategy_name="S1",
            timeframe="15m",
            runner="rob944-test",
        )
    message = str(exc_info.value)
    assert all(exp_id not in message for exp_id in forged_exp_ids)
    report = await campaign_completeness_report(
        session, campaign_run_id=campaign_run_id, expected_specs=specs
    )
    assert report.primary_attempts == 0  # rejected before any record_attempt call


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_records_all_24_attempts_and_reports_complete(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-FULL-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("d" * 64)

    report = await run_full_campaign(
        session,
        specs=specs,
        actual_full_campaign_hash="d" * 64,
        expected_full_campaign_hash="d" * 64,
        campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        build_attempt_evidence=_full_campaign_evidence_factory(
            campaign_run_id, "d" * 64
        ),
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
    )
    assert report.verdict == "complete"
    assert report.actual_registrations == 24
    assert report.primary_attempts == 24
    assert report.status_counts["completed"] == 24
    assert report.missing_experiment_ids == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_records_mixed_terminal_statuses_never_skipping_a_config(
    registry_tables,
):
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-MIXED-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("e" * 64)
    # Reason codes drawn from the controller's own closed, status-scoped
    # allowlist (_ALLOWED_REASON_CODES_BY_STATUS) -- an arbitrary free-text
    # reason (the old f"reason_{status}") is no longer accepted.
    reasons_by_status = {
        "completed": None,
        "rejected": "rejected:data_gap_in_position",
        "crashed": "child_execution_crashed",
        "timeout": "child_execution_timeout",
    }

    def _build(experiment_id_by_key):
        out = []
        statuses = ["completed", "rejected", "crashed", "timeout"]
        fold_evidence_hash = _hex64("fold-hash-1")
        for i, ((skey, cfg), exp_id) in enumerate(experiment_id_by_key.items()):
            status = statuses[i % 4]
            run_identity = _derive_expected_run_identity(
                full_campaign_hash="e" * 64,
                campaign_run_id=campaign_run_id,
                strategy_key=skey,
                experiment_id=exp_id,
                retry_index=0,
                config_id=cfg,
                status=status,
                fold_evidence_hash=fold_evidence_hash,
            )
            out.append(
                AttemptEvidence(
                    attempt_key=AttemptKey(
                        campaign_run_id=campaign_run_id,
                        experiment_id=exp_id,
                        retry_index=0,
                    ),
                    status=status,
                    reason_code=reasons_by_status[status],
                    fold_evidence_hash=fold_evidence_hash,
                    run_identity=run_identity,
                    scenario_evidence=_scenario_evidence(),
                )
            )
        return out

    report = await run_full_campaign(
        session,
        specs=specs,
        actual_full_campaign_hash="e" * 64,
        expected_full_campaign_hash="e" * 64,
        campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        build_attempt_evidence=_build,
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
    )
    assert report.verdict == "complete"
    assert report.primary_attempts == 24
    assert report.status_counts["completed"] == 6
    assert report.status_counts["rejected"] == 6
    assert report.status_counts["crashed"] == 6
    assert report.status_counts["timeout"] == 6


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_full_campaign_is_idempotent_no_auto_retry_on_replay(registry_tables):
    session = registry_tables
    strategy_key = "ROB944-FULLCAMP-IDEMPOTENT-" + uuid.uuid4().hex[:8]
    specs = _campaign_specs(strategy_key)
    campaign_run_id = _derive_expected_campaign_run_id("f" * 64)
    build_evidence = _full_campaign_evidence_factory(campaign_run_id, "f" * 64)

    report1 = await run_full_campaign(
        session,
        specs=specs,
        actual_full_campaign_hash="f" * 64,
        expected_full_campaign_hash="f" * 64,
        campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        build_attempt_evidence=build_evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
    )
    report2 = await run_full_campaign(
        session,
        specs=specs,
        actual_full_campaign_hash="f" * 64,
        expected_full_campaign_hash="f" * 64,
        campaign_run_id=campaign_run_id,
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
        build_attempt_evidence=build_evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="rob944-test",
    )
    assert report1.primary_attempts == 24
    assert report2.primary_attempts == 24  # replay, NOT 48 -- no duplicate/auto-retry
    assert report2.retry_attempts == 0
