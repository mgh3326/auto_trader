"""ROB-984 CP5 standalone first-statement READ ONLY post-audit coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
import rob974_h6a_smoke as h6a_smoke
import rob974_h6b_artifacts as artifacts
import rob974_h6b_cli as h6b_cli
import rob974_h6b_postaudit as postaudit

from app.services.rob974_h6b_materializer import DatabaseTarget


class _SelectStatement:
    is_select = True

    def __str__(self):
        return "SELECT canonical_raw_rows"


class _NonSelectStatement:
    is_select = False

    def __init__(self, sql):
        self.sql = sql

    def __str__(self):
        return self.sql


class AuditSessionSpy:
    def __init__(
        self,
        *,
        begin_error=None,
        read_only_error=None,
        rollback_error=None,
        close_error=None,
    ):
        self.begin_error = begin_error
        self.read_only_error = read_only_error
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.events = []

    async def begin(self):
        self.events.append("begin")
        if self.begin_error is not None:
            raise self.begin_error

    async def execute(self, statement, *_args, **_kwargs):
        sql = str(statement)
        self.events.append(f"execute:{sql}")
        if sql == "SET TRANSACTION READ ONLY" and self.read_only_error is not None:
            raise self.read_only_error
        return object()

    async def rollback(self):
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self):
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error

    async def commit(self):
        self.events.append("commit")
        raise AssertionError("post-audit must never commit")


class RawQueryPort:
    provenance = "contract_fixture"

    def __init__(
        self,
        snapshot,
        *,
        statement=None,
        lifecycle_attempt=None,
        error=None,
    ):
        self.snapshot = snapshot
        self.statement = statement or _SelectStatement()
        self.lifecycle_attempt = lifecycle_attempt
        self.error = error
        self.calls = 0

    async def fetch_raw_rows(self, session, *, plan):
        del plan
        self.calls += 1
        if self.lifecycle_attempt is not None:
            await getattr(session, self.lifecycle_attempt)()
        await session.execute(self.statement)
        if self.error is not None:
            raise self.error
        return self.snapshot


class ContractFixtureH5AuditPort:
    provenance = "contract_fixture"

    def __init__(self):
        self.verify_calls = 0

    def scorecard_for(self, seal):
        return {
            "schema_version": "rob974-h5-contract-fixture-postaudit.v1",
            "provenance": "contract_fixture",
            "full_campaign_hash": seal.full_campaign_hash,
            "campaign_run_id": seal.campaign_run_id,
            "exact_48_mapping_hash": seal.exact_48_mapping_hash,
            "accounting": {
                "experiments": seal.experiments,
                "trials": seal.trials,
                "strategy_counts": dict(seal.strategy_counts),
                "primary_attempts": seal.primary_attempts,
                "total_attempts": seal.total_attempts,
                "retry_attempts": seal.retry_attempts,
                "status_counts": dict(seal.status_counts),
                "out_of_plan_experiments": seal.out_of_plan_experiments,
                "out_of_campaign_trials": seal.out_of_campaign_trials,
                "trial_accounting_hash": seal.trial_accounting_hash,
            },
            "scenario_names": list(seal.scenario_names),
            "named_evidence": [
                {
                    "row_id": item.row_id,
                    "unique_hashes": [
                        {"name": name, "hash": value}
                        for name, value in item.unique_hashes
                    ],
                    "ordered_path_hashes": [
                        {"name": name, "hash": value}
                        for name, value in item.ordered_path_hashes
                    ],
                }
                for item in seal.named_evidence
            ],
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
        accounting = scorecard["accounting"]
        return (
            "# ROB-974 Post-Audit Contract Fixture\n\n"
            f"campaign: {scorecard['campaign_run_id']}\n"
            f"experiments: {accounting['experiments']}\n"
            f"trials: {accounting['trials']}\n"
            f"accounting: {accounting['trial_accounting_hash']}\n"
            f"semantic_verdict: {scorecard['semantic_verdict']}\n"
        ).encode()

    def verify_persisted_scorecard(self, *, scorecard, semantic_hash, expected):
        self.verify_calls += 1
        expected_scorecard = self.scorecard_for(expected)
        if scorecard != expected_scorecard:
            raise postaudit.PostAuditMismatch(
                "persisted H5 scorecard differs from H6-A audit seal"
            )
        if semantic_hash != self.semantic_hash(scorecard):
            raise postaudit.PostAuditMismatch("persisted H5 semantic hash differs")


class AuditFixture:
    def __init__(self, tmp_path, *, session=None):
        smoke = h6a_smoke.build_smoke_plan()
        self.plan = h6b_cli.build_contract_fixture_plan()
        self.attempts = tuple(item for item in smoke.attempts if item.retry_index == 0)
        self.snapshot = postaudit.PostAuditRawSnapshot(
            full_campaign_hash=smoke.full_campaign_hash,
            campaign_run_id=smoke.campaign_run_id,
            registered_mapping=self.plan.ordered_mapping,
            attempts=self.attempts,
        )
        self.seal = postaudit._build_h6a_seal(plan=self.plan, snapshot=self.snapshot)
        self.h5 = ContractFixtureH5AuditPort()
        self.output = tmp_path / "audit-scorecard"
        self.session = session or AuditSessionSpy()
        self.session_factory_calls = 0
        self.query = RawQueryPort(self.snapshot)
        self.target = DatabaseTarget(
            host="localhost",
            port=6543,
            database="rob984_contract_fixture_test_db",
            user="rob984_contract_fixture",
        )
        self.authority = postaudit.PostAuditAuthority(
            expected_target=self.target,
            observed_target=self.target,
            inherited_target=self.target,
            output_dir=self.output,
        )

        def session_factory():
            self.session_factory_calls += 1
            return self.session

        self.ports = postaudit.PostAuditPorts(
            session_factory=session_factory,
            query=self.query,
            h5=self.h5,
        )

    def write_pair(self, scorecard=None):
        scorecard = scorecard or self.h5.scorecard_for(self.seal)
        staged = artifacts.stage_scorecard_pair(
            scorecard=scorecard,
            output_dir=self.output,
            h5_port=self.h5,
        )
        return artifacts.publish_staged_pair(staged, h5_port=self.h5)

    async def run(self, *, authority=None, ports=None):
        return await postaudit.run_contract_fixture_postaudit(
            plan=self.plan,
            authority=authority or self.authority,
            ports=ports or self.ports,
        )


def test_cp5_postaudit_surface_exists() -> None:
    assert postaudit.run_contract_fixture_postaudit


@pytest.mark.asyncio
async def test_first_sql_is_read_only_then_one_raw_select_and_no_commit(tmp_path):
    fixture = AuditFixture(tmp_path)
    fixture.write_pair()
    before = {path.name: path.read_bytes() for path in fixture.output.iterdir()}
    outcome = await fixture.run()
    assert outcome.exit_code == 0
    assert outcome.disposition == "POSTAUDIT_VERIFIED_READ_ONLY"
    assert outcome.trace == (
        "preflight",
        "session_factory",
        "begin",
        "set_transaction_read_only",
        "fetch_canonical_raw_rows",
        "h6a_reconstruct",
        "physical_scorecard_read",
        "h5_scorecard_compare",
        "rollback_read_only",
        "session_close",
    )
    assert fixture.session.events == [
        "begin",
        "execute:SET TRANSACTION READ ONLY",
        "execute:SELECT canonical_raw_rows",
        "rollback",
        "close",
    ]
    assert outcome.counters == postaudit.PostAuditCounters(
        session_factory=1,
        begin=1,
        read_only_statement=1,
        query=1,
        artifact_read=1,
        rollback=1,
        close=1,
    )
    assert {path.name: path.read_bytes() for path in fixture.output.iterdir()} == before


@pytest.mark.asyncio
async def test_exact_48_h6a_accounting_and_named_evidence_are_sealed(tmp_path):
    fixture = AuditFixture(tmp_path)
    fixture.write_pair()
    outcome = await fixture.run()
    seal = outcome.seal
    assert seal.experiments == seal.trials == 48
    assert seal.strategy_counts == (("S3", 24), ("S4", 24))
    assert seal.primary_attempts == seal.total_attempts == 48
    assert seal.retry_attempts == 0
    assert sum(dict(seal.status_counts).values()) == 48
    assert seal.out_of_plan_experiments == seal.out_of_campaign_trials == 0
    assert seal.scenario_names == (
        "base13",
        "primary_stress17",
        "upward_stress22",
    )
    assert len(seal.named_evidence) == 48
    assert all(len(item.unique_hashes) == 8 for item in seal.named_evidence)
    assert all(len(item.ordered_path_hashes) == 3 for item in seal.named_evidence)
    assert (
        outcome.persisted_pair.parsed_scorecard["accounting"]["trial_accounting_hash"]
        == seal.trial_accounting_hash
    )


@pytest.mark.parametrize("field", ("host", "port", "database", "user", "inherited"))
@pytest.mark.asyncio
async def test_exact_target_drift_refuses_before_session_query_and_artifacts(
    tmp_path, field
):
    fixture = AuditFixture(tmp_path)
    values = {
        "host": fixture.target.host,
        "port": fixture.target.port,
        "database": fixture.target.database,
        "user": fixture.target.user,
    }
    if field == "port":
        values[field] += 1
    elif field == "inherited":
        values["user"] += "x"
    else:
        values[field] += "x"
    observed = DatabaseTarget(**values)
    authority = replace(
        fixture.authority,
        observed_target=(fixture.target if field == "inherited" else observed),
        inherited_target=(observed if field == "inherited" else fixture.target),
    )
    outcome = await fixture.run(authority=authority)
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert isinstance(outcome.primary_error, postaudit.PostAuditPreflightError)
    assert outcome.counters.session_factory == outcome.counters.query == 0
    assert outcome.counters.artifact_read == 0
    assert fixture.session_factory_calls == fixture.query.calls == 0


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE runs SET status='x'",
        "DELETE FROM runs",
        "CREATE TABLE x(y int)",
        "DROP TABLE runs",
    ),
)
@pytest.mark.asyncio
async def test_dml_and_ddl_are_rejected_by_select_only_session(tmp_path, statement):
    fixture = AuditFixture(tmp_path)
    fixture.write_pair()
    query = RawQueryPort(fixture.snapshot, statement=_NonSelectStatement(statement))
    outcome = await fixture.run(ports=replace(fixture.ports, query=query))
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert isinstance(outcome.primary_error, postaudit.ReadOnlyQueryViolation)
    assert fixture.session.events == [
        "begin",
        "execute:SET TRANSACTION READ ONLY",
        "rollback",
        "close",
    ]
    assert outcome.counters.commit == outcome.counters.mutation == 0


@pytest.mark.parametrize(
    "method", ("begin", "commit", "rollback", "close", "add", "flush", "delete")
)
@pytest.mark.asyncio
async def test_query_adapter_cannot_own_lifecycle_or_mutate(tmp_path, method):
    fixture = AuditFixture(tmp_path)
    fixture.write_pair()
    query = RawQueryPort(fixture.snapshot, lifecycle_attempt=method)
    outcome = await fixture.run(ports=replace(fixture.ports, query=query))
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert isinstance(outcome.primary_error, postaudit.ReadOnlyQueryViolation)
    assert fixture.session.events == [
        "begin",
        "execute:SET TRANSACTION READ ONLY",
        "rollback",
        "close",
    ]
    assert outcome.counters.commit == outcome.counters.mutation == 0


@pytest.mark.parametrize(
    "snapshot_mutation",
    (
        "wrong_campaign",
        "wrong_mapping",
        "out_of_plan",
        "out_of_campaign",
        "retry",
        "scenario_count",
        "swapped_unique_hashes",
        "swapped_path_hashes",
    ),
)
@pytest.mark.asyncio
async def test_raw_row_mutants_fail_h6a_or_lineage_audit(tmp_path, snapshot_mutation):
    fixture = AuditFixture(tmp_path)
    fixture.write_pair()
    snapshot = fixture.snapshot
    if snapshot_mutation == "wrong_campaign":
        snapshot = replace(snapshot, campaign_run_id="wrong-campaign")
    elif snapshot_mutation == "wrong_mapping":
        mapping = list(snapshot.registered_mapping)
        mapping[0], mapping[1] = mapping[1], mapping[0]
        snapshot = replace(snapshot, registered_mapping=tuple(mapping))
    elif snapshot_mutation == "out_of_plan":
        snapshot = replace(snapshot, out_of_plan_experiment_ids=("f" * 64,))
    elif snapshot_mutation == "out_of_campaign":
        snapshot = replace(snapshot, out_of_campaign_trial_ids=("trial-x",))
    else:
        attempts = list(snapshot.attempts)
        first = attempts[0]
        if snapshot_mutation == "retry":
            object.__setattr__(first, "retry_index", 1)
        elif snapshot_mutation == "scenario_count":
            object.__setattr__(
                first,
                "path_scenario_evidence",
                first.path_scenario_evidence[:-1],
            )
        elif snapshot_mutation == "swapped_unique_hashes":
            left, right = first.unique_evidence[:2]
            left_hash, right_hash = left.content_hash, right.content_hash
            object.__setattr__(left, "content_hash", right_hash)
            object.__setattr__(right, "content_hash", left_hash)
        else:
            left, right = first.path_scenario_evidence[:2]
            left_hash, right_hash = left.artifact_hash, right.artifact_hash
            object.__setattr__(left, "artifact_hash", right_hash)
            object.__setattr__(right, "artifact_hash", left_hash)
        snapshot = replace(snapshot, attempts=tuple(attempts))
    query = RawQueryPort(snapshot)
    outcome = await fixture.run(ports=replace(fixture.ports, query=query))
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert outcome.primary_error is not None
    assert outcome.counters.commit == outcome.counters.mutation == 0
    assert outcome.counters.rollback == outcome.counters.close == 1


@pytest.mark.parametrize(
    "scorecard_mutation",
    (
        "accounting_hash",
        "scenario_count",
        "swapped_unique_hashes",
        "swapped_path_hashes",
        "retry_count",
        "status_count",
        "mapping_hash",
    ),
)
@pytest.mark.asyncio
async def test_physically_valid_but_semantically_mismatched_scorecard_fails(
    tmp_path, scorecard_mutation
):
    fixture = AuditFixture(tmp_path)
    scorecard = fixture.h5.scorecard_for(fixture.seal)
    if scorecard_mutation == "accounting_hash":
        scorecard["accounting"]["trial_accounting_hash"] = "0" * 64
    elif scorecard_mutation == "scenario_count":
        scorecard["scenario_names"] = scorecard["scenario_names"][:-1]
    elif scorecard_mutation == "swapped_unique_hashes":
        values = scorecard["named_evidence"][0]["unique_hashes"]
        values[0]["hash"], values[1]["hash"] = values[1]["hash"], values[0]["hash"]
    elif scorecard_mutation == "swapped_path_hashes":
        values = scorecard["named_evidence"][0]["ordered_path_hashes"]
        values[0]["hash"], values[1]["hash"] = values[1]["hash"], values[0]["hash"]
    elif scorecard_mutation == "retry_count":
        scorecard["accounting"]["retry_attempts"] = 1
    elif scorecard_mutation == "status_count":
        scorecard["accounting"]["status_counts"]["completed"] -= 1
    else:
        scorecard["exact_48_mapping_hash"] = "0" * 64
    fixture.write_pair(scorecard)
    outcome = await fixture.run()
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert isinstance(outcome.primary_error, postaudit.PostAuditMismatch)
    assert outcome.counters.artifact_read == 1
    assert outcome.counters.commit == outcome.counters.mutation == 0


@pytest.mark.parametrize(
    "failure_stage", ("begin", "read_only", "query", "rollback", "close")
)
@pytest.mark.asyncio
async def test_session_readonly_query_rollback_close_failures_are_distinct(
    tmp_path, failure_stage
):
    failure = RuntimeError(f"{failure_stage} leaf")
    session = AuditSessionSpy(
        begin_error=failure if failure_stage == "begin" else None,
        read_only_error=failure if failure_stage == "read_only" else None,
        rollback_error=failure if failure_stage == "rollback" else None,
        close_error=failure if failure_stage == "close" else None,
    )
    fixture = AuditFixture(tmp_path, session=session)
    fixture.write_pair()
    ports = fixture.ports
    if failure_stage == "query":
        ports = replace(ports, query=RawQueryPort(fixture.snapshot, error=failure))
    outcome = await fixture.run(ports=ports)
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    if failure_stage in ("rollback", "close"):
        assert (
            outcome.rollback_error is failure
            if failure_stage == "rollback"
            else outcome.close_error is failure
        )
    else:
        assert outcome.primary_error is failure
    assert outcome.counters.commit == outcome.counters.mutation == 0
    if failure_stage == "begin":
        assert outcome.counters.rollback == 0
        assert outcome.counters.close == 1


@pytest.mark.asyncio
async def test_session_factory_failure_has_no_begin_sql_rollback_close_or_artifact(
    tmp_path,
):
    fixture = AuditFixture(tmp_path)
    failure = RuntimeError("factory leaf")

    def failed_factory():
        fixture.session_factory_calls += 1
        raise failure

    outcome = await fixture.run(
        ports=replace(fixture.ports, session_factory=failed_factory)
    )
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
    assert outcome.primary_error is failure
    assert outcome.counters.session_factory == 1
    assert outcome.counters.begin == outcome.counters.read_only_statement == 0
    assert outcome.counters.query == outcome.counters.artifact_read == 0
    assert outcome.counters.rollback == outcome.counters.close == 0
    assert fixture.session.events == []


@pytest.mark.asyncio
async def test_primary_query_failure_is_not_replaced_by_rollback_or_close(tmp_path):
    primary = RuntimeError("query primary")
    rollback = RuntimeError("rollback secondary")
    close = RuntimeError("close secondary")
    fixture = AuditFixture(
        tmp_path,
        session=AuditSessionSpy(rollback_error=rollback, close_error=close),
    )
    fixture.write_pair()
    ports = replace(fixture.ports, query=RawQueryPort(fixture.snapshot, error=primary))
    outcome = await fixture.run(ports=ports)
    assert outcome.primary_error is primary
    assert outcome.rollback_error is rollback
    assert outcome.close_error is close
    assert outcome.exit_code == postaudit.POSTAUDIT_FAILURE
