"""ROB-984 CP3 sole-transaction coordinator and durability matrix.

H4/H5 values here are explicitly issue-derived ``contract_fixture`` data.
H6-A registration, recording, and accounting are the actual merged APIs.
Sessions are call spies only: no engine, query, or real database is created.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import types
from dataclasses import replace
from pathlib import Path

import pytest
import rob974_h6a_accounting as h6a_accounting
import rob974_h6a_smoke as h6a_smoke
import rob974_h6b_artifacts as artifacts
import rob974_h6b_cli as h6b_cli

from app.schemas.research_backtest import StrategyExperimentIdentity
from app.services import rob974_h6b_materializer as materializer
from app.services.research_canonical_hash import compute_identity_hashes
from app.services.research_db_write_guard import ResearchDbPolicy, ResearchDbTarget


def _unfreeze(value):
    if isinstance(value, types.MappingProxyType | dict):
        return {key: _unfreeze(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_unfreeze(item) for item in value]
    return value


class _RegisteredRow:
    def __init__(self, *, primary_key, experiment_id, **fields):
        self.id = primary_key
        self.experiment_id = experiment_id
        for name, value in fields.items():
            setattr(self, name, value)


class _StoredRow:
    def __init__(self, fingerprint):
        self.raw_payload = {"h6a_evidence_fingerprint": fingerprint}


class SessionSpy:
    def __init__(
        self,
        *,
        begin_error=None,
        commit_error=None,
        rollback_error=None,
        close_error=None,
    ):
        self.begin_error = begin_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.calls = []

    async def begin(self):
        self.calls.append("begin")
        if self.begin_error is not None:
            raise self.begin_error

    async def commit(self):
        self.calls.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.calls.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self):
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error


class ActualH6AAccountingPort:
    provenance = "actual_merged_h6a"

    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def reconstruct(self, *, plan, registered_total, attempts):
        self.calls += 1
        if self.error is not None:
            raise self.error
        rows = tuple(
            h6a_accounting.AttemptAccountingRow(
                row_id=item.row_id,
                experiment_id=item.experiment_id,
                retry_index=item.retry_index,
                status=item.status,
                reason_code=item.reason_code,
                fold_evidence_hash=item.fold_evidence_hash,
                run_identity=item.run_identity,
            )
            for item in attempts
        )
        return h6a_accounting.build_combined_accounting(
            campaign_run_id=plan._fixture_run_id,
            canonical_row_ids=tuple(row_id for row_id, _ in plan.ordered_mapping),
            row_id_to_experiment_id=dict(plan.ordered_mapping),
            registered_total=registered_total,
            attempts=rows,
        )


class ContractFixtureH5Port:
    provenance = "contract_fixture"

    def __init__(self, error=None):
        self.error = error
        self.build_calls = 0

    def build_scorecard(self, *, plan, attempts, accounting):
        self.build_calls += 1
        if self.error is not None:
            raise self.error
        return {
            "schema_version": "rob974-h5-contract-fixture.v1",
            "provenance": "contract_fixture",
            "attempts": len(attempts),
            "registered_total": accounting.registered_total,
            "primary_attempts": accounting.primary_attempts,
            "retry_attempts": accounting.retry_attempts,
            "trial_accounting_hash": accounting.trial_accounting_hash,
            "mapping_hash": plan.contract_fixture_mapping_hash,
            "semantic_verdict": "NOT_EVALUATED",
        }

    def canonical_json_bytes(self, scorecard):
        return (
            json.dumps(
                scorecard,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()

    def semantic_hash(self, scorecard):
        return hashlib.sha256(self.canonical_json_bytes(scorecard)).hexdigest()

    def render_markdown(self, scorecard):
        return (
            "# ROB-974 Contract Fixture\n\n"
            f"attempts: {scorecard['attempts']}\n"
            f"accounting: {scorecard['trial_accounting_hash']}\n"
            "semantic_verdict: NOT_EVALUATED\n"
        ).encode()


class DirectoryArtifactPort:
    provenance = "rob974_h6b_directory_atomic_v1"

    def __init__(self, *, stage_error=None, publish_error=None):
        self.stage_error = stage_error
        self.publish_error = publish_error
        self.stage_calls = 0
        self.publish_calls = 0

    def stage(self, *, scorecard, output_dir, h5_port):
        self.stage_calls += 1
        if self.stage_error is not None:
            raise self.stage_error
        return artifacts.stage_scorecard_pair(
            scorecard=scorecard, output_dir=output_dir, h5_port=h5_port
        )

    def publish(self, staged, *, h5_port):
        self.publish_calls += 1
        if self.publish_error is not None:
            raise self.publish_error
        return artifacts.publish_staged_pair(staged, h5_port=h5_port)

    def probe(self, *, output_dir):
        return artifacts.probe_artifact_state(output_dir=output_dir)

    def inspect(self, *, scorecard, output_dir, h5_port):
        return artifacts.inspect_exact_artifact_replay(
            scorecard=scorecard, output_dir=output_dir, h5_port=h5_port
        )


class Fixture:
    def __init__(self, tmp_path: Path, *, session=None):
        self.smoke = h6a_smoke.build_smoke_plan()
        self.plan = h6b_cli.build_contract_fixture_plan()
        assert self.plan.ordered_mapping == tuple(
            (spec.row_id, spec.experiment_id) for spec in self.smoke.row_specs
        )
        s3_specs = []
        s4_specs = []
        for spec in self.smoke.row_specs:
            identity = StrategyExperimentIdentity(
                strategy_key=spec.strategy_key,
                strategy_version=spec.strategy_version,
                hypothesis=spec.hypothesis,
                **_unfreeze(spec.components),
            )
            (s3_specs if spec.row_id.startswith("S3") else s4_specs).append(identity)
        self.campaign = materializer.ContractFixtureCampaignInput(
            plan=self.plan,
            s3_specs=tuple(s3_specs),
            s4_specs=tuple(s4_specs),
            guard_policy=ResearchDbPolicy.of(
                ResearchDbTarget(host="localhost", database_name="test_db")
            ),
        )
        self.attempts = tuple(
            materializer.H6AAttemptBatchItem(
                row_id=item.row_id,
                experiment_id=item.experiment_id,
                retry_index=item.retry_index,
                status=item.status,
                reason_code=item.reason_code,
                fold_evidence_hash=item.fold_evidence_hash,
                run_identity=item.run_identity,
                evidence_payload={
                    "provenance": "contract_fixture",
                    "canonical_row_id": item.row_id,
                    "status": item.status,
                },
            )
            for item in self.smoke.attempts
            if item.retry_index == 0
        )
        self.session = session or SessionSpy()
        self.session_factory_calls = 0
        self.register_delegate_calls = 0
        self.record_delegate_calls = 0
        self.find_calls = 0
        self.h4_calls = 0
        self.accounting = ActualH6AAccountingPort()
        self.h5 = ContractFixtureH5Port()
        self.artifact_port = DirectoryArtifactPort()
        self.output = tmp_path / "materialized-pair"

        def session_factory():
            self.session_factory_calls += 1
            return self.session

        async def register_experiments_fn(
            session, *, specs, guard_opt_in_enabled, guard_policy
        ):
            del session, guard_opt_in_enabled, guard_policy
            self.register_delegate_calls += 1
            rows = []
            starting_pk = 1 if self.register_delegate_calls == 1 else 25
            for primary_key, spec in enumerate(specs, start=starting_pk):
                hashes = compute_identity_hashes(spec.components())
                rows.append(
                    _RegisteredRow(
                        primary_key=primary_key,
                        experiment_id=dict(self.plan.ordered_mapping)[
                            spec.params["row_id"]
                        ],
                        strategy_key=spec.strategy_key,
                        strategy_version=spec.strategy_version,
                        **hashes,
                    )
                )
            return rows

        async def run_h4_attempts_fn(plan):
            assert plan is self.plan
            self.h4_calls += 1
            return self.attempts

        async def find_existing_trial_fn(session, *, experiment_pk, idempotency_key):
            del session, experiment_pk, idempotency_key
            self.find_calls += 1
            return None

        async def record_trial_fn(session, *, experiment_id, request):
            del session, experiment_id
            self.record_delegate_calls += 1
            return _StoredRow(request.raw_payload["h6a_evidence_fingerprint"])

        self.ports = materializer.ContractFixtureExecutionPorts(
            session_factory=session_factory,
            register_experiments_fn=register_experiments_fn,
            run_h4_attempts_fn=run_h4_attempts_fn,
            find_existing_trial_fn=find_existing_trial_fn,
            record_trial_fn=record_trial_fn,
            h6a_accounting=self.accounting,
            h5=self.h5,
            artifacts=self.artifact_port,
        )

    def authorize(self):
        return materializer.issue_contract_fixture_authorization(
            self.plan, approval_token="rob984-cp3-contract-fixture"
        )

    async def run(self, *, authorization=None, ports=None):
        return await materializer.materialize_contract_fixture(
            plan=self.plan,
            authorization=authorization or self.authorize(),
            campaign=self.campaign,
            ports=ports or self.ports,
            output_dir=self.output,
        )


def test_cp3_coordinator_surface_exists() -> None:
    assert materializer.materialize_contract_fixture


@pytest.mark.asyncio
async def test_exact_stage_order_real_h6a_and_directory_publication(tmp_path):
    fixture = Fixture(tmp_path)
    outcome = await fixture.run()
    assert outcome.exit_code == 0
    assert outcome.disposition == "MATERIALIZED"
    assert outcome.trace == (
        "preflight",
        "session_factory",
        "begin",
        "h6a_register",
        "h4_attempts",
        "h6a_record",
        "h6a_accounting",
        "h5_scorecard",
        "artifact_stage",
        "db_commit",
        "artifact_publish",
        "session_close",
    )
    assert fixture.session.calls == ["begin", "commit", "close"]
    assert fixture.register_delegate_calls == 2
    assert fixture.find_calls == fixture.record_delegate_calls == 48
    assert outcome.accounting.registered_total == 48
    assert outcome.accounting.primary_attempts == 48
    assert outcome.accounting.retry_attempts == 0
    assert outcome.counters == materializer.CoordinatorCounters(
        session_factory=1,
        begin=1,
        register=1,
        h4=1,
        record=1,
        accounting=1,
        h5=1,
        stage=1,
        rollback=0,
        commit=1,
        publish=1,
        close=1,
    )
    assert sorted(path.name for path in fixture.output.iterdir()) == [
        "scorecard.json",
        "scorecard.md",
    ]


@pytest.mark.asyncio
async def test_reused_one_shot_refuses_before_session_and_filesystem(tmp_path):
    first = Fixture(tmp_path / "first")
    tmp_path.joinpath("first").mkdir()
    authorization = first.authorize()
    assert (await first.run(authorization=authorization)).exit_code == 0

    second_root = tmp_path / "second"
    second_root.mkdir()
    second = Fixture(second_root)
    outcome = await materializer.materialize_contract_fixture(
        plan=first.plan,
        authorization=authorization,
        campaign=first.campaign,
        ports=second.ports,
        output_dir=second.output,
    )
    assert outcome.exit_code == materializer.AUTHORITY_OR_PREFLIGHT_REFUSED
    assert outcome.primary_error is not None
    assert second.session_factory_calls == 0
    assert outcome.counters.session_factory == outcome.counters.stage == 0
    assert not second.output.exists()


@pytest.mark.asyncio
async def test_session_factory_failure_has_no_session_lifecycle_or_publish(tmp_path):
    fixture = Fixture(tmp_path)
    failure = RuntimeError("factory leaf")

    def broken_factory():
        fixture.session_factory_calls += 1
        raise failure

    outcome = await fixture.run(
        ports=replace(fixture.ports, session_factory=broken_factory)
    )
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.primary_error is failure
    assert outcome.counters.session_factory == 1
    assert outcome.counters.begin == outcome.counters.rollback == 0
    assert outcome.counters.commit == outcome.counters.publish == 0
    assert outcome.counters.close == 0


@pytest.mark.asyncio
async def test_begin_failure_closes_once_without_rollback(tmp_path):
    failure = RuntimeError("begin leaf")
    session = SessionSpy(begin_error=failure)
    fixture = Fixture(tmp_path, session=session)
    outcome = await fixture.run()
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.primary_error is failure
    assert session.calls == ["begin", "close"]
    assert outcome.rollback_outcome == "NOT_ATTEMPTED"
    assert outcome.close_outcome == "SUCCEEDED"


@pytest.mark.asyncio
async def test_second_strategy_registration_failure_rolls_back_exactly_once(tmp_path):
    fixture = Fixture(tmp_path)
    failure = RuntimeError("S4 register leaf")
    original = fixture.ports.register_experiments_fn
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise failure
        return await original(*args, **kwargs)

    outcome = await fixture.run(
        ports=replace(fixture.ports, register_experiments_fn=fail_second)
    )
    assert outcome.primary_error is failure
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert fixture.session.calls == ["begin", "rollback", "close"]
    assert outcome.counters.publish == 0
    assert not fixture.output.exists()


@pytest.mark.parametrize("failure_index", (1, 24, 48))
@pytest.mark.asyncio
async def test_first_middle_last_attempt_failure_preserves_leaf_and_rolls_back(
    tmp_path, failure_index
):
    fixture = Fixture(tmp_path)
    failure = RuntimeError(f"attempt-{failure_index}-leaf")
    original = fixture.ports.record_trial_fn
    calls = 0

    async def fail_at(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failure_index:
            raise failure
        return await original(*args, **kwargs)

    outcome = await fixture.run(ports=replace(fixture.ports, record_trial_fn=fail_at))
    assert outcome.primary_error is failure
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert fixture.session.calls == ["begin", "rollback", "close"]
    assert outcome.counters.commit == outcome.counters.publish == 0


@pytest.mark.parametrize("stage", ("accounting", "h5", "artifact"))
@pytest.mark.asyncio
async def test_each_post_record_precommit_failure_rolls_back_and_never_publishes(
    tmp_path, stage
):
    fixture = Fixture(tmp_path)
    failure = RuntimeError(f"{stage} leaf")
    if stage == "accounting":
        ports = replace(
            fixture.ports, h6a_accounting=ActualH6AAccountingPort(error=failure)
        )
    elif stage == "h5":
        ports = replace(fixture.ports, h5=ContractFixtureH5Port(error=failure))
    else:
        ports = replace(
            fixture.ports, artifacts=DirectoryArtifactPort(stage_error=failure)
        )
    outcome = await fixture.run(ports=ports)
    assert outcome.primary_error is failure
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.counters.rollback == 1
    assert outcome.counters.commit == outcome.counters.publish == 0
    assert not fixture.output.exists()


@pytest.mark.parametrize(
    "physical_stage",
    (
        "json_write",
        "markdown_write",
        "directory_fsync",
        "json_readback",
        "render_parity",
    ),
)
@pytest.mark.asyncio
async def test_each_physical_substage_failure_is_precommit_and_publish_zero(
    tmp_path, monkeypatch, physical_stage
):
    fixture = Fixture(tmp_path)
    if physical_stage in ("json_write", "markdown_write"):
        wanted = "scorecard.json" if physical_stage == "json_write" else "scorecard.md"
        original = artifacts._write_exclusive_fsynced

        def fail_selected(path, payload):
            if path.name == wanted:
                raise OSError(f"{physical_stage} leaf")
            return original(path, payload)

        monkeypatch.setattr(artifacts, "_write_exclusive_fsynced", fail_selected)
    elif physical_stage == "directory_fsync":

        def fail_directory_fsync(_path):
            raise OSError("directory fsync leaf")

        monkeypatch.setattr(artifacts, "_fsync_directory", fail_directory_fsync)
    elif physical_stage == "json_readback":
        original = artifacts._read_regular_file

        def corrupt_json_readback(path):
            raw = original(path)
            return raw + b"corrupt" if path.name == "scorecard.json" else raw

        monkeypatch.setattr(artifacts, "_read_regular_file", corrupt_json_readback)
    else:
        in_memory_scorecard = None

        class ParsedRenderDriftPort(ContractFixtureH5Port):
            def build_scorecard(self, **kwargs):
                nonlocal in_memory_scorecard
                in_memory_scorecard = super().build_scorecard(**kwargs)
                return in_memory_scorecard

            def render_markdown(self, scorecard):
                rendered = super().render_markdown(scorecard)
                if scorecard is not in_memory_scorecard:
                    return rendered.replace(b"NOT_EVALUATED", b"PARSED_DRIFT")
                return rendered

        fixture.h5 = ParsedRenderDriftPort()
        fixture.ports = replace(fixture.ports, h5=fixture.h5)

    outcome = await fixture.run()
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.counters.rollback == 1
    assert outcome.counters.commit == outcome.counters.publish == 0
    assert outcome.primary_error is not None
    assert not fixture.output.exists()


@pytest.mark.parametrize(
    ("failure", "disposition"),
    (
        (materializer.CommitRejectedError("commit rejected"), "COMMIT_FAILED"),
        (ConnectionError("connection lost during commit"), "COMMIT_OUTCOME_UNKNOWN"),
    ),
)
@pytest.mark.asyncio
async def test_commit_failure_is_never_published_or_retried(
    tmp_path, failure, disposition
):
    session = SessionSpy(commit_error=failure)
    fixture = Fixture(tmp_path, session=session)
    outcome = await fixture.run()
    assert outcome.primary_error is failure
    assert outcome.exit_code == materializer.COMMIT_FAILED_OR_UNKNOWN
    assert outcome.disposition == disposition
    assert outcome.retry_forbidden is True
    assert session.calls == ["begin", "commit", "rollback", "close"]
    assert outcome.counters.publish == 0
    assert outcome.staged_pair.staging_dir.exists()
    assert not fixture.output.exists()


@pytest.mark.asyncio
async def test_confirmed_commit_then_publish_failure_never_rolls_back(tmp_path):
    fixture = Fixture(tmp_path)
    failure = RuntimeError("rename leaf")
    ports = replace(
        fixture.ports, artifacts=DirectoryArtifactPort(publish_error=failure)
    )
    outcome = await fixture.run(ports=ports)
    assert outcome.primary_error is failure
    assert outcome.exit_code == materializer.POSTCOMMIT_PUBLISH_FAILURE
    assert outcome.disposition == "DB_DURABLE_ARTIFACT_UNPUBLISHED"
    assert outcome.commit_confirmed is True
    assert fixture.session.calls == ["begin", "commit", "close"]
    assert outcome.counters.rollback == 0
    assert outcome.staged_pair.staging_dir.exists()


@pytest.mark.asyncio
async def test_close_only_failure_preserves_materialized_durability(tmp_path):
    close_failure = RuntimeError("close leaf")
    fixture = Fixture(tmp_path, session=SessionSpy(close_error=close_failure))
    outcome = await fixture.run()
    assert outcome.exit_code == materializer.SESSION_CLOSE_FAILURE
    assert outcome.disposition == "MATERIALIZED_CLOSE_FAILED"
    assert outcome.primary_error is None
    assert outcome.close_error is close_failure
    assert outcome.commit_confirmed is True
    assert outcome.counters.rollback == 0
    assert fixture.output.exists()


@pytest.mark.asyncio
async def test_rollback_and_close_failures_do_not_replace_primary_leaf(tmp_path):
    primary = RuntimeError("H4 primary leaf")
    rollback = RuntimeError("rollback secondary")
    close = RuntimeError("close secondary")
    session = SessionSpy(rollback_error=rollback, close_error=close)
    fixture = Fixture(tmp_path, session=session)

    async def failed_h4(_plan):
        raise primary

    outcome = await fixture.run(
        ports=replace(fixture.ports, run_h4_attempts_fn=failed_h4)
    )
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert outcome.primary_error is primary
    assert outcome.rollback_error is rollback
    assert outcome.close_error is close
    assert outcome.rollback_outcome == outcome.close_outcome == "FAILED"
    assert session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("primary_stage", ("commit", "publish"))
@pytest.mark.asyncio
async def test_close_failure_is_secondary_to_commit_or_publish_failure(
    tmp_path, primary_stage
):
    close = RuntimeError("close secondary")
    primary = RuntimeError(f"{primary_stage} primary")
    session = SessionSpy(
        commit_error=(
            materializer.CommitRejectedError(str(primary))
            if primary_stage == "commit"
            else None
        ),
        close_error=close,
    )
    fixture = Fixture(tmp_path, session=session)
    ports = fixture.ports
    if primary_stage == "publish":
        ports = replace(ports, artifacts=DirectoryArtifactPort(publish_error=primary))
    outcome = await fixture.run(ports=ports)
    expected_exit = (
        materializer.COMMIT_FAILED_OR_UNKNOWN
        if primary_stage == "commit"
        else materializer.POSTCOMMIT_PUBLISH_FAILURE
    )
    assert outcome.exit_code == expected_exit
    assert outcome.close_error is close
    assert outcome.close_outcome == "FAILED"
    assert outcome.disposition != "MATERIALIZED_CLOSE_FAILED"


@pytest.mark.parametrize("lifecycle_method", ("begin", "commit", "rollback", "close"))
@pytest.mark.asyncio
async def test_predecessor_lifecycle_is_poisoned_and_underlying_call_is_zero(
    tmp_path, lifecycle_method
):
    fixture = Fixture(tmp_path)

    async def poisoned_register(session, *, specs, guard_opt_in_enabled, guard_policy):
        del specs, guard_opt_in_enabled, guard_policy
        await getattr(session, lifecycle_method)()

    outcome = await fixture.run(
        ports=replace(fixture.ports, register_experiments_fn=poisoned_register)
    )
    assert isinstance(
        outcome.primary_error, materializer.PredecessorTransactionOwnershipError
    )
    assert outcome.exit_code == materializer.PRECOMMIT_FAILURE
    assert fixture.session.calls == ["begin", "rollback", "close"]
    assert outcome.counters.commit == 0


@pytest.mark.asyncio
async def test_cancellation_during_rollback_still_closes_and_retains_primary(tmp_path):
    primary = RuntimeError("H4 primary")
    cancellation = asyncio.CancelledError("cancel-rollback")
    fixture = Fixture(tmp_path, session=SessionSpy(rollback_error=cancellation))

    async def failed_h4(_plan):
        raise primary

    with pytest.raises(asyncio.CancelledError) as raised:
        await fixture.run(ports=replace(fixture.ports, run_h4_attempts_fn=failed_h4))
    assert raised.value is cancellation
    outcome = cancellation.rob984_materialization_outcome
    assert outcome.primary_error is primary
    assert outcome.rollback_error is cancellation
    assert outcome.counters.rollback == outcome.counters.close == 1
    assert fixture.session.calls == ["begin", "rollback", "close"]


@pytest.mark.parametrize("cancel_stage", ("begin", "h4", "record", "commit", "close"))
@pytest.mark.asyncio
async def test_cancellation_retains_native_exception_and_last_durable_state(
    tmp_path, cancel_stage
):
    cancellation = asyncio.CancelledError(f"cancel-{cancel_stage}")
    session = SessionSpy(
        begin_error=cancellation if cancel_stage == "begin" else None,
        commit_error=cancellation if cancel_stage == "commit" else None,
        close_error=cancellation if cancel_stage == "close" else None,
    )
    fixture = Fixture(tmp_path, session=session)
    ports = fixture.ports
    if cancel_stage == "h4":

        async def cancel_h4(_plan):
            raise cancellation

        ports = replace(ports, run_h4_attempts_fn=cancel_h4)
    elif cancel_stage == "record":

        async def cancel_record(*args, **kwargs):
            del args, kwargs
            raise cancellation

        ports = replace(ports, record_trial_fn=cancel_record)

    with pytest.raises(asyncio.CancelledError) as raised:
        await fixture.run(ports=ports)
    assert raised.value is cancellation
    outcome = cancellation.rob984_materialization_outcome
    assert outcome.counters.close == 1
    if cancel_stage == "commit":
        assert outcome.disposition == "COMMIT_OUTCOME_UNKNOWN"
        assert outcome.counters.publish == 0
    elif cancel_stage == "close":
        assert outcome.disposition == "MATERIALIZED_CLOSE_FAILED"
        assert outcome.commit_confirmed is True
    else:
        assert outcome.disposition == "PRECOMMIT_FAILURE"
        assert outcome.counters.publish == 0
