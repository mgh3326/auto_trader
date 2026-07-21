"""ROB-984 CP1 pure plan, closed CLI, and exact preflight contracts."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import io
import json
import random
import socket
import subprocess
import time
from pathlib import Path

import pytest


def _cli():
    spec = importlib.util.find_spec("rob974_h6b_cli")
    assert spec is not None, "ROB-984 CP1 pure plan behavior is not implemented"
    return importlib.import_module("rob974_h6b_cli")


def _materializer():
    return importlib.import_module("app.services.rob974_h6b_materializer")


def test_cp1_pure_plan_behavior_is_implemented() -> None:
    assert _cli().build_contract_fixture_plan


def _all_mapping_keys(value: object) -> set[str]:
    if type(value) is dict:
        return set(value) | {
            key for child in value.values() for key in _all_mapping_keys(child)
        }
    if type(value) is list:
        return {key for child in value for key in _all_mapping_keys(child)}
    return set()


def test_plan_is_deterministic_exact_48_fixture_only_and_has_no_production_claim():
    cli = _cli()
    materializer = _materializer()
    first = cli.build_contract_fixture_plan()
    second = cli.build_contract_fixture_plan()
    first_bytes = cli.render_plan_bytes(first)
    second_bytes = cli.render_plan_bytes(second)
    assert first_bytes == second_bytes
    assert first_bytes.endswith(b"\n")
    payload = json.loads(first_bytes)
    assert payload["status"] == "NOT_LAUNCHABLE_CONTRACT_FIXTURE"
    assert payload["predecessor_mode"] == "contract_fixture"
    assert payload["actual_h4_contract"] == "NOT_EVALUATED"
    assert payload["actual_h5_contract"] == "NOT_EVALUATED"
    assert payload["production_identity"] == "DEFERRED_UNTIL_H4_SOURCE_PINS"
    assert payload["h6a"]["payload_mode"] == "fixture_plan"
    assert set(payload["h6a"]["source_pins"].values()) == {None}
    assert "full_campaign_hash" not in _all_mapping_keys(payload)
    assert "campaign_run_id" not in _all_mapping_keys(payload)
    mapping = tuple(
        (entry["row_id"], entry["experiment_id"])
        for entry in payload["contract_fixture_ordered_mapping"]
    )
    assert tuple(row_id for row_id, _ in mapping) == materializer.CANONICAL_ROW_ORDER
    assert len(mapping) == len(set(mapping)) == 48
    assert len({experiment_id for _, experiment_id in mapping}) == 48
    assert (
        materializer.validate_exact_48_mapping(mapping)
        == payload["contract_fixture_exact_48_mapping_hash"]
    )


def test_plan_has_no_fs_process_network_clock_random_or_environment_effect(
    monkeypatch: pytest.MonkeyPatch,
):
    cli = _cli()
    baseline = cli.render_plan_bytes(cli.build_contract_fixture_plan())

    def poison(*_args, **_kwargs):
        raise AssertionError("forbidden plan effect")

    monkeypatch.setattr(Path, "mkdir", poison)
    monkeypatch.setattr(Path, "open", poison)
    monkeypatch.setattr(subprocess, "run", poison)
    monkeypatch.setattr(subprocess, "Popen", poison)
    monkeypatch.setattr(socket, "socket", poison)
    monkeypatch.setattr(time, "time", poison)
    monkeypatch.setattr(random, "random", poison)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://wrong:wrong@LOCALHOST:5432/rob974_db_suffix",
    )
    assert cli.render_plan_bytes(cli.build_contract_fixture_plan()) == baseline


@pytest.mark.parametrize(
    "mutator",
    (
        lambda rows: rows[:-1],
        lambda rows: rows + (rows[-1],),
        lambda rows: (rows[1], rows[0], *rows[2:]),
        lambda rows: (("X-00", rows[0][1]), *rows[1:]),
        lambda rows: (rows[0], (rows[1][0], rows[0][1]), *rows[2:]),
    ),
)
def test_mapping_47_49_reorder_wrong_row_and_duplicate_fail(mutator):
    materializer = _materializer()
    rows = _cli().build_contract_fixture_plan().ordered_mapping
    with pytest.raises(materializer.H6BPlanError):
        materializer.validate_exact_48_mapping(tuple(mutator(rows)))


def test_mapping_exact_builtin_types_and_hash_drift_fail():
    materializer = _materializer()
    plan = _cli().build_contract_fixture_plan()
    with pytest.raises(materializer.H6BPlanError):
        materializer.validate_exact_48_mapping(list(plan.ordered_mapping))
    with pytest.raises(materializer.H6BPlanError):
        dataclasses.replace(
            plan,
            contract_fixture_mapping_hash=(
                ("0" if plan.contract_fixture_mapping_hash[0] != "0" else "1")
                + plan.contract_fixture_mapping_hash[1:]
            ),
        )


def _target(**changes):
    materializer = _materializer()
    values = {
        "host": "db-approved.internal",
        "port": 5432,
        "database": "rob974_db",
        "user": "rob974_runner",
    }
    values.update(changes)
    return materializer.DatabaseTarget(**values)


@pytest.mark.parametrize(
    "observed",
    (
        {"host": "DB-APPROVED.INTERNAL"},
        {"host": "localhost"},
        {"port": 5433},
        {"database": "rob974_db_test"},
        {"database": "xrob974_db"},
        {"user": "rob974_runneR"},
    ),
)
def test_exact_target_alias_case_suffix_and_one_character_mutants_fail(observed):
    materializer = _materializer()
    approved = _target()
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_database_target_pair(
            approved=approved, observed=_target(**observed), inherited=None
        )


def test_test_db_and_every_non_empirical_approved_database_fail():
    materializer = _materializer()
    for name in ("test_db", "rob974_test_db", "ROB974_DB", "rob974_db_suffix"):
        target = _target(database=name)
        with pytest.raises(materializer.H6BPreflightRefused):
            materializer.validate_database_target_pair(
                approved=target, observed=target, inherited=None
            )


def test_port_string_bool_and_target_subclass_fail_without_coercion():
    materializer = _materializer()
    for value in ("5432", True, 5432.0):
        with pytest.raises(materializer.H6BPlanError):
            _target(port=value)

    class TargetSubclass(materializer.DatabaseTarget):
        pass

    subclassed = TargetSubclass(
        "db-approved.internal", 5432, "rob974_db", "rob974_runner"
    )
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_database_target_pair(
            approved=subclassed, observed=subclassed, inherited=None
        )


def test_inherited_dsn_conflict_fails_exactly():
    materializer = _materializer()
    approved = _target()
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_database_target_pair(
            approved=approved,
            observed=approved,
            inherited=_target(host="localhost"),
        )


def _pins(seed: str = "a"):
    materializer = _materializer()
    return materializer.ExactSourcePins(
        integration_head_sha=seed * 40,
        integration_tree_sha="b" * 40,
        feature_source_sha256="c" * 64,
        engine_source_sha256="d" * 64,
        runner_source_sha256="e" * 64,
        pbo_implementation_sha256="f" * 64,
    )


def test_stale_source_or_tree_pin_and_subclass_fail():
    materializer = _materializer()
    expected = _pins()
    materializer.validate_source_pins_pair(expected=expected, observed=expected)
    for field in dataclasses.fields(expected):
        changed = dataclasses.replace(
            expected,
            **{field.name: "1" * len(getattr(expected, field.name))},
        )
        with pytest.raises(materializer.H6BPreflightRefused):
            materializer.validate_source_pins_pair(expected=expected, observed=changed)

    class PinSubclass(materializer.ExactSourcePins):
        pass

    subclassed = PinSubclass(*dataclasses.astuple(expected))
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_source_pins_pair(expected=expected, observed=subclassed)


def test_arbitrary_run_id_is_rejected_by_h6a_derivation():
    materializer = _materializer()
    full_hash = "a" * 64
    derived = materializer.derive_campaign_run_id(full_hash)
    materializer.validate_derived_run_id(
        full_campaign_hash=full_hash, campaign_run_id=derived
    )
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_derived_run_id(
            full_campaign_hash=full_hash, campaign_run_id="operator-run-id"
        )


def test_fixture_run_is_closed_before_session_child_or_filesystem():
    cli = _cli()
    complete_run = (
        "--run",
        "--expected-full-campaign-hash",
        "a" * 64,
        "--campaign-run-id",
        "fixture-run",
        "--expected-mapping-hash",
        "b" * 64,
        "--approved-db-host",
        "db-approved.internal",
        "--approved-db-port",
        "5432",
        "--approved-db-name",
        "rob974_db",
        "--approved-db-user",
        "rob974_runner",
        "--write-opt-in",
        "true",
        "--output-root",
        "/tmp/rob984-not-created",
        "--integration-head-sha",
        "c" * 64,
        "--integration-tree-sha",
        "d" * 64,
        "--feature-source-sha256",
        "e" * 64,
        "--engine-source-sha256",
        "f" * 64,
        "--runner-source-sha256",
        "1" * 64,
        "--pbo-implementation-sha256",
        "2" * 64,
        "--one-shot-approval",
        "fixture-token",
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    assert cli.run_cli(complete_run, stdout=stdout, stderr=stderr) == 4
    assert stdout.getvalue() == ""
    assert stderr.getvalue().startswith("AUTHORITY_OR_PREFLIGHT_REFUSED")
    assert not Path("/tmp/rob984-not-created").exists()
    stdout, stderr = io.StringIO(), io.StringIO()
    assert cli.run_cli(("--run",), stdout=stdout, stderr=stderr) == 2
    assert "CLI_USAGE_OR_PLAN_ERROR" in stderr.getvalue()


def test_plan_cli_is_byte_identical_and_run_only_arguments_are_usage_error():
    cli = _cli()
    first_out, first_err = io.StringIO(), io.StringIO()
    second_out, second_err = io.StringIO(), io.StringIO()
    assert cli.run_cli(("--plan",), stdout=first_out, stderr=first_err) == 0
    assert cli.run_cli(("--plan",), stdout=second_out, stderr=second_err) == 0
    assert first_out.getvalue() == second_out.getvalue()
    assert first_err.getvalue() == second_err.getvalue() == ""
    bad_out, bad_err = io.StringIO(), io.StringIO()
    assert (
        cli.run_cli(
            ("--plan", "--approved-db-host", "localhost"),
            stdout=bad_out,
            stderr=bad_err,
        )
        == 2
    )


def test_h6b_alone_issues_exact_one_shot_operation_specific_h6a_contexts():
    materializer = _materializer()
    plan = _cli().build_contract_fixture_plan()
    issued = materializer.issue_contract_fixture_authorization(
        plan, approval_token="cp1-contract-fixture-one-shot"
    )
    register, record = materializer.build_h6a_mutation_contexts(issued)
    assert type(register) is type(record) is materializer.ApprovedMutationContext
    assert register.operation_kind == materializer.REGISTER_CAMPAIGN_OPERATION_KIND
    assert record.operation_kind == materializer.RECORD_ATTEMPTS_OPERATION_KIND
    assert (
        register.canonical_plan_hash,
        register.derived_run_id,
        register.exact_48_mapping_hash,
        register.approval_token,
    ) == (
        record.canonical_plan_hash,
        record.derived_run_id,
        record.exact_48_mapping_hash,
        record.approval_token,
    )
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.build_h6a_mutation_contexts(issued)
    assert tuple(
        inspect.signature(materializer.build_h6a_mutation_contexts).parameters
    ) == ("authorization",)


def test_self_attested_authorization_context_swap_and_subclass_fail():
    materializer = _materializer()
    self_attested = materializer.IssuedOneShotAuthorization(
        campaign_hash="a" * 64,
        run_id="self-attested",
        mapping_hash="b" * 64,
        token="caller-token",
        _issuer=object(),
    )
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.build_h6a_mutation_contexts(self_attested)

    plan = _cli().build_contract_fixture_plan()

    class PlanSubclass(materializer.ContractFixturePlan):
        pass

    subclassed_plan = PlanSubclass(*dataclasses.astuple(plan))
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.issue_contract_fixture_authorization(
            subclassed_plan, approval_token="x"
        )

    issued = materializer.issue_contract_fixture_authorization(
        plan, approval_token="swap-test"
    )
    register, record = materializer.build_h6a_mutation_contexts(issued)
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_h6a_context_pair(record, register)

    class ContextSubclass(materializer.ApprovedMutationContext):
        pass

    subclassed_context = ContextSubclass(
        register.operation_kind,
        register.canonical_plan_hash,
        register.derived_run_id,
        register.exact_48_mapping_hash,
        register.approval_token,
    )
    with pytest.raises(materializer.H6BPreflightRefused):
        materializer.validate_h6a_context_pair(subclassed_context, record)


def test_exit_disposition_table_is_closed_and_historical_verdict_is_separate():
    materializer = _materializer()
    assert tuple(row[0] for row in materializer.EXIT_DISPOSITION_TABLE) == (
        0,
        2,
        4,
        6,
        7,
        8,
        9,
        10,
    )
    assert 5 not in {row[0] for row in materializer.EXIT_DISPOSITION_TABLE}
    zero = materializer.EXIT_DISPOSITION_TABLE[0]
    assert zero[1] == ("MATERIALIZED", "REPLAY_NOOP")
    assert "semantic verdict separately" in zero[2]
    assert all(
        "historical_pass" not in dispositions
        and "historical_fail" not in dispositions
        and "incomplete" not in dispositions
        for _, dispositions, _ in materializer.EXIT_DISPOSITION_TABLE
    )
