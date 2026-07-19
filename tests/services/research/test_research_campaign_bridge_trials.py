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
import hashlib
import json
import uuid
from types import SimpleNamespace

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
    ChildFailureDiagnostic,
    ChildFailureDiagnosticOverflow,
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
    terminal_evidence_fingerprint,
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
# ROB-970 (Q2/Q3, Fable-approved orch-fable-answer-rob970-20260719.md):
# diagnostic_evidence round-trips into H6's persisted raw_payload, and never
# influences terminal_evidence_fingerprint (observer-effect-0).
# --------------------------------------------------------------------------- #


def _diagnostic(**overrides) -> ChildFailureDiagnostic:
    base = {
        "transport": "in_process",
        "stage": "generator",
        "exception_type": "RuntimeError",
        "message": "boom: synthetic signal-generation failure",
        "traceback_text": "Traceback (most recent call last):\nRuntimeError: boom\n",
        "stderr": None,
        "strategy": "S1",
        "config_id": "S1-00",
        "symbol": "BTCUSDT",
        "fold_id": "fold-00",
        "scenario_name": None,
        "signature": "a" * 64,
        "occurrence_count": 3,
        "truncated": False,
    }
    base.update(overrides)
    return ChildFailureDiagnostic(**base)


@pytest.mark.unit
def test_terminal_evidence_fingerprint_is_unaffected_by_diagnostic_evidence() -> None:
    """Observer-effect-0: the fingerprint used for idempotent replay
    detection must be IDENTICAL regardless of diagnostic_evidence content --
    diagnostic evidence has no semantic-identity role."""
    without = _evidence("camp1", "e" * 64, run_identity="run-fixed")
    with_one = without.model_copy(update={"diagnostic_evidence": (_diagnostic(),)})
    with_different = without.model_copy(
        update={
            "diagnostic_evidence": (
                _diagnostic(message="a totally different secret-bearing message"),
            )
        }
    )
    fp_without = terminal_evidence_fingerprint(without)
    fp_with_one = terminal_evidence_fingerprint(with_one)
    fp_with_different = terminal_evidence_fingerprint(with_different)
    assert fp_without == fp_with_one == fp_with_different


@pytest.mark.unit
def test_terminal_evidence_fingerprint_is_unaffected_by_diagnostic_overflow() -> None:
    """ROB-970 R1 (Q1=A, cap=32): observer-effect-0 extends to the honest
    overflow accounting too."""
    without = _evidence("camp1", "e" * 64, run_identity="run-fixed-overflow")
    with_overflow = without.model_copy(
        update={
            "diagnostic_overflow": ChildFailureDiagnosticOverflow(
                truncated=True, omitted_distinct_signatures=5, omitted_occurrences=42
            )
        }
    )
    assert terminal_evidence_fingerprint(without) == terminal_evidence_fingerprint(
        with_overflow
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnostic_evidence_round_trips_into_raw_payload(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    diag = _diagnostic()
    evidence = _evidence("camp1", experiment_id, run_identity="run-diag-1")
    evidence = evidence.model_copy(update={"diagnostic_evidence": (diag,)})

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

    stored = row.raw_payload["diagnostic_evidence"]
    assert len(stored) == 1
    stored_diag = stored[0]
    assert stored_diag["transport"] == "in_process"
    assert stored_diag["stage"] == "generator"
    assert stored_diag["exception_type"] == "RuntimeError"
    assert stored_diag["config_id"] == "S1-00"
    assert stored_diag["occurrence_count"] == 3
    assert stored_diag["stderr"] is None
    # the persisted idempotency fingerprint is unaffected by diagnostic
    # content -- matches the fingerprint of the SAME evidence with no
    # diagnostic_evidence at all.
    without_diag = evidence.model_copy(update={"diagnostic_evidence": ()})
    assert row.raw_payload["h6_evidence_fingerprint"] == terminal_evidence_fingerprint(
        without_diag
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_diagnostic_evidence_absent_persists_empty_list_not_missing_key(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-diag-2")

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
    assert row.raw_payload["diagnostic_evidence"] == []


# --------------------------------------------------------------------------- #
# R2 audit item 2/3 (5th boundary): AttemptEvidence is NOT frozen (only its
# ChildFailureDiagnostic/ChildFailureDiagnosticOverflow leaves are), so a
# caller could reassign `.diagnostic_evidence`/`.diagnostic_overflow` AFTER
# construction, bypassing every Pydantic validator entirely (research H6 ->
# CLI -> H5 seal -> app schema all already checked -- this is the 5th and
# final "service assembly/persistence" boundary, which must not simply trust
# that an already-constructed AttemptEvidence still satisfies the cap/
# consistency invariants at the moment it is actually persisted).
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_boundary_rejects_diagnostic_evidence_forged_over_cap_post_construction(
    registry_tables,
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-forge-1")
    # Forge past the cap via direct attribute assignment -- AttemptEvidence
    # is not frozen, so this bypasses the schema's own model_validator.
    evidence.diagnostic_evidence = tuple(
        _diagnostic(signature=("a" * 63) + str(i)) for i in range(33)
    )

    with pytest.raises(bridge.DiagnosticEvidenceBoundaryViolation):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
    count = await session.scalar(
        select(func.count())
        .select_from(ResearchBacktestRun)
        .where(
            ResearchBacktestRun.strategy_experiment_id
            == (await bridge._get_experiment_by_id(session, experiment_id)).id
        )
    )
    assert count == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_boundary_rejects_diagnostic_evidence_entries_of_the_wrong_type(
    registry_tables,
) -> None:
    """Forging non-``ChildFailureDiagnostic`` entries into the tuple (e.g.
    plain dicts) must fail closed at the service boundary rather than being
    blindly iterated and persisted."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-forge-2")
    evidence.diagnostic_evidence = ({"message": "not a real diagnostic"},)

    with pytest.raises(bridge.DiagnosticEvidenceBoundaryViolation):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_boundary_rejects_diagnostic_overflow_forged_inconsistent_post_construction(
    registry_tables,
) -> None:
    """``ChildFailureDiagnosticOverflow`` itself is frozen, but a caller can
    still swap ``evidence.diagnostic_overflow`` wholesale for an unvalidated
    instance built via ``model_construct`` (which skips every validator)."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-forge-3")
    evidence.diagnostic_overflow = ChildFailureDiagnosticOverflow.model_construct(
        truncated=True, omitted_distinct_signatures=0, omitted_occurrences=0
    )

    with pytest.raises(bridge.DiagnosticEvidenceBoundaryViolation):
        await record_attempt(
            session,
            experiment_id=experiment_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_service_boundary_accepts_genuinely_well_formed_diagnostic_evidence(
    registry_tables,
) -> None:
    """Control case: legitimate, unmutated evidence must not be rejected by
    the new service-boundary guard."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-forge-4")
    evidence = evidence.model_copy(update={"diagnostic_evidence": (_diagnostic(),)})

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
    assert len(row.raw_payload["diagnostic_evidence"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_with_different_diagnostic_evidence_still_replays_not_mismatch(
    registry_tables, capsys
) -> None:
    """ROB-970 R1 (Q2=C-modified, Fable-approved
    orch-fable-answer-rob970-r1-20260719.md): a replay under the SAME
    attempt key whose diagnostics DIVERGE is neither a semantic
    TerminalEvidenceMismatch (fingerprint still matches) nor a silent loss
    (the R1 Important-2 bug) -- the row stays untouched (original wins,
    append-only) AND the divergence is loudly surfaced via a
    diagnostic_replay_divergence observation."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-diag-3")

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
    capsys.readouterr()  # drain anything from the first (non-replay) call
    replay_with_diag = evidence.model_copy(
        update={"diagnostic_evidence": (_diagnostic(),)}
    )
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=replay_with_diag,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert first.id == second.id
    # the ORIGINAL row's raw_payload is never mutated by a later replay
    # carrying different (non-semantic) diagnostic content -- append-only,
    # original wins.
    assert second.raw_payload["diagnostic_evidence"] == []

    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())
    assert payload["event"] == "diagnostic_replay_divergence"
    # R2 audit: never the raw idempotency_key -- only a stable digest of it.
    assert "idempotency_key" not in payload
    assert (
        payload["idempotency_key_digest"]
        == hashlib.sha256(
            evidence.attempt_key.idempotency_key().encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_identical_diagnostics_is_write_free_noop_no_observation(
    registry_tables, capsys
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-diag-identical")
    evidence = evidence.model_copy(update={"diagnostic_evidence": (_diagnostic(),)})

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
    capsys.readouterr()
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,  # byte-identical replay
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert first.id == second.id
    captured = capsys.readouterr()
    assert captured.err == ""  # write-free no-op -- no divergence observation


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_divergent_diagnostic_overflow_emits_observation(
    registry_tables, capsys
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-overflow-diverge")

    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    capsys.readouterr()
    replay_with_overflow = evidence.model_copy(
        update={
            "diagnostic_overflow": ChildFailureDiagnosticOverflow(
                truncated=True, omitted_distinct_signatures=1, omitted_occurrences=1
            )
        }
    )
    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=replay_with_overflow,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.err.strip())
    assert payload["event"] == "diagnostic_replay_divergence"


# --------------------------------------------------------------------------- #
# R2 audit item 4 (Q2=C-modified canonical-byte replay contract): each
# divergence DIMENSION, exercised independently, emits EXACTLY one typed
# observation -- new/missing signature, wording, occurrence count, and
# per-record truncation flag (empty<->nonempty and overflow metadata are
# already covered by the two tests immediately above).
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dimension,mutate",
    [
        ("new_signature", lambda d: d.model_copy(update={"signature": "b" * 64})),
        (
            "wording",
            lambda d: d.model_copy(update={"message": "a wholly different message"}),
        ),
        ("occurrence_count", lambda d: d.model_copy(update={"occurrence_count": 99})),
        ("record_truncation", lambda d: d.model_copy(update={"truncated": True})),
    ],
)
async def test_each_diagnostic_divergence_dimension_emits_exactly_one_observation(
    registry_tables, capsys, dimension, mutate
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    base_diag = _diagnostic()
    evidence = _evidence(
        "camp1", experiment_id, run_identity=f"run-dim-{dimension}"
    ).model_copy(update={"diagnostic_evidence": (base_diag,)})

    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    capsys.readouterr()

    divergent = evidence.model_copy(
        update={"diagnostic_evidence": (mutate(base_diag),)}
    )
    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=divergent,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "diagnostic_replay_divergence"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nonempty_to_empty_diagnostic_evidence_emits_exactly_one_observation(
    registry_tables, capsys
) -> None:
    """The reverse direction of the empty->nonempty case already covered
    above: a replay that DROPS previously-recorded diagnostics is equally a
    divergence (never silently accepted as "no diagnostics == nothing to
    compare"), and still never mutates the original row."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence(
        "camp1", experiment_id, run_identity="run-nonempty-to-empty"
    ).model_copy(update={"diagnostic_evidence": (_diagnostic(),)})

    original = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    capsys.readouterr()

    replay_empty = evidence.model_copy(update={"diagnostic_evidence": ()})
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=replay_empty,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert second.id == original.id
    assert len(second.raw_payload["diagnostic_evidence"]) == 1  # original untouched

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "diagnostic_replay_divergence"


@pytest.mark.unit
def test_legacy_row_missing_diagnostic_keys_normalizes_to_default_write_free_noop() -> (
    None
):
    """R2 audit: legacy/absent-field normalization -- a row persisted BEFORE
    ROB-970 (no ``diagnostic_evidence``/``diagnostic_overflow`` keys in
    ``raw_payload`` at all, a migration-free older row) must canonicalize
    IDENTICALLY to a row explicitly storing empty/default diagnostics, and
    an incoming evidence with empty/default diagnostics must byte-match
    that same reconstruction -- the exact comparison
    ``_check_diagnostic_divergence`` performs.

    (This is a pure unit test of the normalization helpers, not an
    integration test through ``record_attempt``: ``research.backtest_runs``
    enforces an append-only DB trigger that rejects any UPDATE, so a
    "legacy row" cannot be simulated by mutating an already-recorded row --
    which is itself a live confirmation of the append-only invariant this
    whole design depends on.)"""
    legacy_row = SimpleNamespace(
        raw_payload={
            "h6_evidence_fingerprint": "f" * 64,
            "campaign_run_id": "camp1",
            "retry_index": 0,
            # no diagnostic_evidence / diagnostic_overflow keys at all.
        }
    )
    explicit_default_row = SimpleNamespace(
        raw_payload={
            "h6_evidence_fingerprint": "f" * 64,
            "campaign_run_id": "camp1",
            "retry_index": 0,
            "diagnostic_evidence": [],
            "diagnostic_overflow": {
                "truncated": False,
                "omitted_distinct_signatures": 0,
                "omitted_occurrences": 0,
            },
        }
    )
    legacy_bytes = bridge._canonical_diagnostic_bytes(
        bridge._stored_diagnostic_evidence_payload(legacy_row),
        bridge._stored_diagnostic_overflow_payload(legacy_row),
    )
    explicit_bytes = bridge._canonical_diagnostic_bytes(
        bridge._stored_diagnostic_evidence_payload(explicit_default_row),
        bridge._stored_diagnostic_overflow_payload(explicit_default_row),
    )
    assert legacy_bytes == explicit_bytes

    evidence = _evidence("camp1", "e" * 64, run_identity="run-legacy-normalize")
    incoming_bytes = bridge._canonical_diagnostic_bytes(
        bridge._diagnostic_evidence_payload(evidence),
        bridge._diagnostic_overflow_payload(evidence),
    )
    assert incoming_bytes == legacy_bytes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_observation_emission_failure_never_turns_divergence_into_a_failed_attempt(
    registry_tables, monkeypatch
) -> None:
    """R2 audit: non-fail-stop by deliberate contract -- an observation
    write/handler failure must NOT turn a diagnostic-only divergence into a
    failed attempt. The original row must still be returned untouched."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-observer-fail")

    original = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )

    def _broken_emit(**kwargs):
        raise RuntimeError("simulated observation-emission failure")

    monkeypatch.setattr(bridge, "_emit_diagnostic_replay_divergence", _broken_emit)

    divergent = evidence.model_copy(update={"diagnostic_evidence": (_diagnostic(),)})
    second = await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=divergent,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    assert second.id == original.id
    assert second.raw_payload["diagnostic_evidence"] == []  # original untouched


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_divergence_observation_never_leaks_raw_diagnostic_text(
    registry_tables, capsys
) -> None:
    session = registry_tables
    _spec, experiment_id = await _register(session)
    evidence = _evidence("camp1", experiment_id, run_identity="run-diag-safe")

    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=evidence,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    capsys.readouterr()
    # R2 audit: a genuinely secret-SHAPED literal (e.g. "SECRET-...") is now
    # rejected at ChildFailureDiagnostic construction itself (the app schema
    # trust boundary) before it can even reach the observation path. This
    # test targets a DIFFERENT guarantee -- that the observation emitter
    # itself never echoes raw diagnostic message text, digest-only -- so it
    # uses a safe-shaped-but-unique marker that legitimately passes schema
    # construction.
    secret_message = "distinctive-marker-9f3a2b1c-should-never-appear-in-observation"
    replay_with_diag = evidence.model_copy(
        update={"diagnostic_evidence": (_diagnostic(message=secret_message),)}
    )
    await record_attempt(
        session,
        experiment_id=experiment_id,
        evidence=replay_with_diag,
        strategy_name="S1",
        timeframe="15m",
        runner="pytest",
        guard_opt_in_enabled=True,
        guard_policy=_POLICY,
    )
    captured = capsys.readouterr()
    assert secret_message not in captured.err
    payload = json.loads(captured.err.strip())
    # only stable digests/counts/context -- never raw diagnostic text.
    assert "message" not in json.dumps(payload)
    assert secret_message not in json.dumps(payload)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replay_semantic_mismatch_still_raises_terminal_evidence_mismatch(
    registry_tables,
) -> None:
    """Regression guard: the NEW diagnostic-divergence detection must never
    interfere with the EXISTING semantic-mismatch fail-stop."""
    session = registry_tables
    _spec, experiment_id = await _register(session)
    original = _evidence("camp1", experiment_id, run_identity="run-fixed-semantic")

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
        "camp1", experiment_id, status="crashed", reason_code="child_execution_crashed"
    )
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_genuinely_simultaneous_divergent_replays_each_emit_exactly_one_observation(
    registry_tables, capsys
) -> None:
    """R2 audit: replaces the former SEQUENTIAL "concurrency proxy" test --
    two independent test-DB sessions issue GENUINELY simultaneous (via
    ``asyncio.gather`` over two independent sessions/tasks, not sequential
    awaits on one shared session) divergent replays against the SAME
    already-recorded attempt key. Both must observe (never lose, never
    dedupe-collapse) their own divergence, and the original row's
    raw_payload bytes must remain byte-for-byte unchanged by either."""
    from app.core.db import engine

    session_maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with session_maker() as setup:
        _spec, experiment_id = await _register(setup)
        await setup.commit()

    evidence = _evidence(
        "camp-concurrent-diag", experiment_id, run_identity="run-concurrent-diag"
    )
    async with session_maker() as seed:
        original = await record_attempt(
            seed,
            experiment_id=experiment_id,
            evidence=evidence,
            strategy_name="S1",
            timeframe="15m",
            runner="pytest",
            guard_opt_in_enabled=True,
            guard_policy=_POLICY,
        )
        original_raw_payload = dict(original.raw_payload)
        original_id = original.id
        await seed.commit()

    replay_a = evidence.model_copy(
        update={
            "diagnostic_evidence": (_diagnostic(message="concurrent divergence A"),)
        }
    )
    replay_b = evidence.model_copy(
        update={
            "diagnostic_evidence": (_diagnostic(message="concurrent divergence B"),)
        }
    )

    async def worker(replay_evidence):
        async with session_maker() as s:
            row = await record_attempt(
                s,
                experiment_id=experiment_id,
                evidence=replay_evidence,
                strategy_name="S1",
                timeframe="15m",
                runner="pytest",
                guard_opt_in_enabled=True,
                guard_policy=_POLICY,
            )
            await s.commit()
            return row.id

    capsys.readouterr()
    ids = await asyncio.gather(worker(replay_a), worker(replay_b))
    assert ids == [original_id, original_id]

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 2  # no loss
    events = [json.loads(line) for line in lines]
    assert all(e["event"] == "diagnostic_replay_divergence" for e in events)
    # no dedupe-collapse: two DISTINCT incoming digests observed.
    assert len({e["incoming_diagnostic_digest"] for e in events}) == 2

    async with session_maker() as check:
        final = await check.get(ResearchBacktestRun, original_id)
        assert final.raw_payload == original_raw_payload


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
