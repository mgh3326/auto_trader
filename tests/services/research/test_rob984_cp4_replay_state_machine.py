"""ROB-984 CP4 two-sided replay/collision and recovery state machine.

Every database observation is a call-spy ``contract_fixture`` snapshot. No
engine, SQL query, or real database exists in this checkpoint.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import rob974_h6b_artifacts as artifacts

from app.services import rob974_h6b_materializer as materializer
from tests.services.research.test_rob984_cp3_transaction_coordinator import (
    Fixture,
    SessionSpy,
)


class SnapshotInspector:
    provenance = "contract_fixture"

    def __init__(self, snapshot, *, lifecycle_attempt=None, error=None):
        self.snapshot = snapshot
        self.lifecycle_attempt = lifecycle_attempt
        self.error = error
        self.calls = 0

    async def inspect(self, session, *, plan):
        del plan
        self.calls += 1
        if self.lifecycle_attempt is not None:
            await getattr(session, self.lifecycle_attempt)()
        if self.error is not None:
            raise self.error
        return self.snapshot


def _absent_snapshot():
    return materializer.CampaignDbSnapshot(
        campaign_run_id=None,
        registered_mapping=(),
        attempts=(),
    )


def _exact_snapshot(fixture):
    return materializer.CampaignDbSnapshot(
        campaign_run_id=fixture.plan._fixture_run_id,
        registered_mapping=fixture.plan.ordered_mapping,
        attempts=fixture.attempts,
    )


def _ports(fixture, snapshot):
    return replace(
        fixture.ports,
        state_inspector=SnapshotInspector(snapshot),
    )


async def _run(fixture, snapshot, *, ports=None):
    return await materializer.materialize_or_replay_contract_fixture(
        plan=fixture.plan,
        authorization=fixture.authorize(),
        campaign=fixture.campaign,
        ports=ports or _ports(fixture, snapshot),
        output_dir=fixture.output,
    )


def test_cp4_replay_classifier_surfaces_exist() -> None:
    assert materializer.materialize_or_replay_contract_fixture
    assert artifacts.probe_artifact_state


@pytest.mark.asyncio
async def test_dual_absence_is_the_only_state_that_enters_mutation(tmp_path):
    fixture = Fixture(tmp_path)
    outcome = await _run(fixture, _absent_snapshot())
    assert outcome.exit_code == 0
    assert outcome.disposition == "MATERIALIZED"
    assert outcome.db_state == "ABSENT"
    assert outcome.artifact_state == "ABSENT"
    assert outcome.counters.db_inspect == outcome.counters.artifact_probe == 1
    assert outcome.counters.register == outcome.counters.record == 1
    assert outcome.counters.commit == outcome.counters.publish == 1
    assert fixture.output.exists()


@pytest.mark.asyncio
async def test_exact_db_and_exact_physical_pair_is_write_free_replay(
    tmp_path, monkeypatch
):
    fixture = Fixture(tmp_path)
    assert (await fixture.run()).disposition == "MATERIALIZED"
    before_session_calls = tuple(fixture.session.calls)
    before_register = fixture.register_delegate_calls
    before_record = fixture.record_delegate_calls
    before_stage = fixture.artifact_port.stage_calls
    before_publish = fixture.artifact_port.publish_calls
    before_bytes = {path.name: path.read_bytes() for path in fixture.output.iterdir()}
    before_inodes = {path.name: path.stat().st_ino for path in fixture.output.iterdir()}

    def forbidden_write(*_args, **_kwargs):
        pytest.fail("exact replay attempted a physical write")

    monkeypatch.setattr(artifacts, "_write_exclusive_fsynced", forbidden_write)
    monkeypatch.setattr(artifacts, "_rename_noreplace", forbidden_write)
    outcome = await _run(fixture, _exact_snapshot(fixture))
    assert outcome.exit_code == 0
    assert outcome.disposition == "REPLAY_NOOP"
    assert outcome.db_state == "EXACT"
    assert outcome.artifact_state == "PAIR_PRESENT"
    assert outcome.replay_inspection.disposition == "EXACT_ARTIFACT_REPLAY"
    assert outcome.counters.register == outcome.counters.record == 0
    assert outcome.counters.commit == outcome.counters.stage == 0
    assert outcome.counters.delete == outcome.counters.publish == 0
    assert outcome.counters.accounting == outcome.counters.h5 == 1
    assert outcome.counters.replay_verify == 1
    assert outcome.counters.rollback == outcome.counters.close == 1
    assert tuple(fixture.session.calls) == before_session_calls + (
        "begin",
        "rollback",
        "close",
    )
    assert fixture.register_delegate_calls == before_register
    assert fixture.record_delegate_calls == before_record
    assert fixture.artifact_port.stage_calls == before_stage
    assert fixture.artifact_port.publish_calls == before_publish
    assert {path.name: path.read_bytes() for path in fixture.output.iterdir()} == (
        before_bytes
    )
    assert {path.name: path.stat().st_ino for path in fixture.output.iterdir()} == (
        before_inodes
    )


@pytest.mark.parametrize(
    "db_variant",
    ("partial", "wrong_run", "wrong_mapping", "mismatch", "out_of_plan"),
)
@pytest.mark.asyncio
async def test_partial_wrong_or_out_of_plan_db_state_never_mutates(
    tmp_path, db_variant
):
    fixture = Fixture(tmp_path)
    exact = _exact_snapshot(fixture)
    if db_variant == "partial":
        snapshot = replace(
            exact,
            registered_mapping=exact.registered_mapping[:-1],
            attempts=exact.attempts[:-1],
        )
    elif db_variant == "wrong_run":
        snapshot = replace(exact, campaign_run_id="rob974-h6a-wrong-run")
    elif db_variant == "wrong_mapping":
        mapping = list(exact.registered_mapping)
        mapping[0], mapping[1] = mapping[1], mapping[0]
        snapshot = replace(exact, registered_mapping=tuple(mapping))
    elif db_variant == "mismatch":
        snapshot = replace(exact, mismatch_row_ids=("S3-00",))
    else:
        snapshot = replace(
            exact,
            out_of_plan_experiment_ids=("f" * 64,),
        )
    outcome = await _run(fixture, snapshot)
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert isinstance(outcome.primary_error, materializer.ReplayCollisionError)
    assert outcome.retry_forbidden is True
    assert outcome.counters.register == outcome.counters.record == 0
    assert outcome.counters.commit == outcome.counters.stage == 0
    assert outcome.counters.delete == outcome.counters.publish == 0
    assert outcome.counters.rollback == outcome.counters.close == 1
    assert not fixture.output.exists()


@pytest.mark.asyncio
async def test_db_exact_artifact_absent_is_asymmetric_and_never_repaired(tmp_path):
    fixture = Fixture(tmp_path)
    outcome = await _run(fixture, _exact_snapshot(fixture))
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert isinstance(outcome.primary_error, materializer.ReplayCollisionError)
    assert outcome.retry_forbidden is True
    assert outcome.db_state == "EXACT"
    assert outcome.artifact_state == "ABSENT"
    assert outcome.counters.register == outcome.counters.record == 0
    assert outcome.counters.stage == outcome.counters.commit == 0
    assert not fixture.output.exists()


@pytest.mark.asyncio
async def test_artifact_exact_db_absent_is_asymmetric_and_never_writes(tmp_path):
    fixture = Fixture(tmp_path)
    assert (await fixture.run()).exit_code == 0
    pair_before = {path.name: path.read_bytes() for path in fixture.output.iterdir()}
    register_before = fixture.register_delegate_calls
    record_before = fixture.record_delegate_calls
    outcome = await _run(fixture, _absent_snapshot())
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert isinstance(outcome.primary_error, materializer.ReplayCollisionError)
    assert outcome.retry_forbidden is True
    assert outcome.db_state == "ABSENT"
    assert outcome.artifact_state == "PAIR_PRESENT"
    assert outcome.counters.register == outcome.counters.record == 0
    assert outcome.counters.commit == outcome.counters.stage == 0
    assert fixture.register_delegate_calls == register_before
    assert fixture.record_delegate_calls == record_before
    assert {path.name: path.read_bytes() for path in fixture.output.iterdir()} == (
        pair_before
    )


@pytest.mark.parametrize("invalid_shape", ("half_pair", "extra_file", "final_symlink"))
@pytest.mark.asyncio
async def test_invalid_final_shape_refuses_before_session_or_query(
    tmp_path, invalid_shape
):
    fixture = Fixture(tmp_path)
    if invalid_shape == "final_symlink":
        target = tmp_path / "target"
        target.mkdir()
        fixture.output.symlink_to(target, target_is_directory=True)
    else:
        fixture.output.mkdir()
        fixture.output.joinpath("scorecard.json").write_text("{}\n")
        if invalid_shape == "extra_file":
            fixture.output.joinpath("scorecard.md").write_text("# fixture\n")
            fixture.output.joinpath("extra").write_text("forensic")
    inspector = SnapshotInspector(_exact_snapshot(fixture))
    outcome = await _run(
        fixture,
        inspector.snapshot,
        ports=replace(fixture.ports, state_inspector=inspector),
    )
    assert outcome.exit_code == materializer.AUTHORITY_OR_PREFLIGHT_REFUSED
    assert isinstance(outcome.primary_error, materializer.ReplayCollisionError)
    assert outcome.retry_forbidden is True
    assert outcome.artifact_state == "INVALID_FINAL"
    assert outcome.counters.session_factory == outcome.counters.db_inspect == 0
    assert outcome.counters.delete == 0
    assert inspector.calls == 0
    assert fixture.output.exists() or fixture.output.is_symlink()


@pytest.mark.asyncio
async def test_stale_staging_refuses_without_deletion_session_or_query(tmp_path):
    fixture = Fixture(tmp_path)
    stale = tmp_path / f".{fixture.output.name}.staging-forensic"
    stale.mkdir()
    stale.joinpath("evidence").write_text("preserve")
    inspector = SnapshotInspector(_absent_snapshot())
    outcome = await _run(
        fixture,
        inspector.snapshot,
        ports=replace(fixture.ports, state_inspector=inspector),
    )
    assert outcome.exit_code == materializer.AUTHORITY_OR_PREFLIGHT_REFUSED
    assert outcome.artifact_state == "STALE_STAGING"
    assert outcome.retry_forbidden is True
    assert outcome.counters.session_factory == outcome.counters.db_inspect == 0
    assert outcome.counters.delete == 0
    assert inspector.calls == 0
    assert stale.joinpath("evidence").read_text() == "preserve"


@pytest.mark.asyncio
async def test_differing_pair_with_exact_db_fails_replay_without_repair(tmp_path):
    fixture = Fixture(tmp_path)
    assert (await fixture.run()).exit_code == 0
    json_path = fixture.output / "scorecard.json"
    json_path.write_bytes(json_path.read_bytes() + b"corrupt")
    corrupted = json_path.read_bytes()
    outcome = await _run(fixture, _exact_snapshot(fixture))
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.db_state == "EXACT"
    assert outcome.retry_forbidden is True
    assert outcome.counters.replay_verify == 1
    assert outcome.counters.register == outcome.counters.record == 0
    assert outcome.counters.commit == outcome.counters.stage == 0
    assert json_path.read_bytes() == corrupted


@pytest.mark.asyncio
async def test_replay_close_only_failure_has_distinct_disposition(tmp_path):
    fixture = Fixture(tmp_path)
    assert (await fixture.run()).exit_code == 0
    close_failure = RuntimeError("replay close leaf")
    fixture.session.close_error = close_failure
    outcome = await _run(fixture, _exact_snapshot(fixture))
    assert outcome.exit_code == materializer.SESSION_CLOSE_FAILURE
    assert outcome.disposition == "REPLAY_NOOP_CLOSE_FAILED"
    assert outcome.close_error is close_failure
    assert outcome.primary_error is None
    assert outcome.counters.commit == outcome.counters.publish == 0


@pytest.mark.asyncio
async def test_replay_rollback_failure_is_not_reported_as_noop(tmp_path):
    fixture = Fixture(tmp_path)
    assert (await fixture.run()).exit_code == 0
    rollback_failure = RuntimeError("read-only rollback leaf")
    fixture.session.rollback_error = rollback_failure
    outcome = await _run(fixture, _exact_snapshot(fixture))
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.disposition == "PRECOMMIT_FAILURE"
    assert outcome.primary_error is rollback_failure
    assert outcome.rollback_error is rollback_failure
    assert outcome.counters.commit == outcome.counters.publish == 0


@pytest.mark.parametrize("lifecycle_method", ("begin", "commit", "rollback", "close"))
@pytest.mark.asyncio
async def test_inspector_cannot_own_transaction_lifecycle(tmp_path, lifecycle_method):
    fixture = Fixture(tmp_path)
    inspector = SnapshotInspector(
        _absent_snapshot(), lifecycle_attempt=lifecycle_method
    )
    outcome = await _run(
        fixture,
        inspector.snapshot,
        ports=replace(fixture.ports, state_inspector=inspector),
    )
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert isinstance(
        outcome.primary_error, materializer.PredecessorTransactionOwnershipError
    )
    assert fixture.session.calls == ["begin", "rollback", "close"]
    assert outcome.counters.commit == outcome.counters.publish == 0


@pytest.mark.parametrize("crash_window", ("commit_unknown", "publish_failed"))
@pytest.mark.asyncio
async def test_crash_windows_preserve_forensics_and_forbid_retry(
    tmp_path, crash_window
):
    if crash_window == "commit_unknown":
        session = SessionSpy(commit_error=ConnectionError("ambiguous COMMIT"))
        fixture = Fixture(tmp_path, session=session)
        ports = _ports(fixture, _absent_snapshot())
    else:
        fixture = Fixture(tmp_path)
        fixture.artifact_port.publish_error = RuntimeError(
            "publish failed after commit"
        )
        ports = _ports(fixture, _absent_snapshot())
    outcome = await materializer.materialize_or_replay_contract_fixture(
        plan=fixture.plan,
        authorization=fixture.authorize(),
        campaign=fixture.campaign,
        ports=ports,
        output_dir=fixture.output,
    )
    expected = (
        materializer.COMMIT_FAILED_OR_UNKNOWN
        if crash_window == "commit_unknown"
        else materializer.POSTCOMMIT_PUBLISH_FAILURE
    )
    assert outcome.exit_code == expected
    assert outcome.retry_forbidden is True
    assert outcome.staged_pair.staging_dir.exists()
    assert outcome.counters.publish == (0 if crash_window == "commit_unknown" else 1)
