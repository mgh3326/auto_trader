"""ROB-984 CP8 actual merged H4/H5 identity and composition boundary."""

from __future__ import annotations

import dataclasses
import importlib
import io
import json
import random
import socket
import subprocess
import time
from pathlib import Path

import pytest

_FULL_CAMPAIGN_HASH = "341a5a57ec14b7a499ea58d74de3b7d9c4b2c4e8bb514c789f6f528231a4045d"
_CAMPAIGN_RUN_ID = "rob974h6a-ReYDH4lJ8dDmDJxApTNIB7p72qAjTBI2Qited6Ni9Y0"
_RUNNER_BUNDLE = "eef75d09aac6ee862f8e3d959c6abd11e72b3faf072cee620ddb9309678af806"
_MAPPING_HASH = "9ec3fdac35c3a98ed0f17bb5f10ab75fb1d68abf89a964e471c3182f53a11bf0"
_INTEGRATION_HEAD = "c3c31b76e3a79e9cf9573e066b1d7e278088fc8e"
_INTEGRATION_TREE = "bc8091c50e720af86b610332714d077e7b461397"


def _cli():
    return importlib.import_module("rob974_h6b_cli")


def _materializer():
    return importlib.import_module("app.services.rob974_h6b_materializer")


def _actual_plan():
    build = getattr(_cli(), "build_production_identity_plan", None)
    assert callable(build), (
        "CP8 requires a pure actual merged H4/H5 production identity plan"
    )
    return build()


def test_cp8_literal_identity_pin_followup_one():
    plan = _actual_plan()
    assert plan.full_campaign_hash == _FULL_CAMPAIGN_HASH
    assert plan.campaign_run_id == _CAMPAIGN_RUN_ID


def test_cp8_literal_identity_pin_followup_two():
    plan = _actual_plan()
    assert plan.full_campaign_hash == _FULL_CAMPAIGN_HASH
    assert plan.campaign_run_id == _CAMPAIGN_RUN_ID


def test_actual_plan_is_exact_48_source_pinned_and_fixture_free():
    plan = _actual_plan()
    materializer = _materializer()
    assert type(plan) is materializer.ProductionIdentityPlan
    assert plan.exact_48_mapping_hash == _MAPPING_HASH
    assert materializer.validate_exact_48_mapping(plan.ordered_mapping) == _MAPPING_HASH
    assert tuple(row_id for row_id, _ in plan.ordered_mapping) == (
        materializer.CANONICAL_ROW_ORDER
    )
    assert len({experiment_id for _, experiment_id in plan.ordered_mapping}) == 48
    assert plan.source_pins.runner_source_sha256 == _RUNNER_BUNDLE

    raw = _cli().render_plan_bytes(plan)
    payload = json.loads(raw)
    assert payload["status"] == "PRODUCTION_IDENTITY_READY"
    assert payload["predecessor_mode"] == "actual_merged_h4_h5"
    assert payload["actual_h4_contract"] == "PASS"
    assert payload["actual_h5_contract"] == "PASS"
    assert payload["full_campaign_hash"] == _FULL_CAMPAIGN_HASH
    assert payload["campaign_run_id"] == _CAMPAIGN_RUN_ID
    assert payload["exact_48_mapping_hash"] == _MAPPING_HASH
    assert "fixture" not in raw.decode().lower()
    assert "placeholder" not in raw.decode().lower()
    assert all(value is not None for value in payload["source_pins"].values())


def test_actual_plan_is_byte_identical_without_process_network_clock_random_or_env(
    monkeypatch: pytest.MonkeyPatch,
):
    cli = _cli()
    baseline = cli.render_plan_bytes(_actual_plan())

    def poison(*_args, **_kwargs):
        raise AssertionError("forbidden actual-plan effect")

    monkeypatch.setattr(subprocess, "run", poison)
    monkeypatch.setattr(subprocess, "Popen", poison)
    monkeypatch.setattr(socket, "socket", poison)
    monkeypatch.setattr(time, "time", poison)
    monkeypatch.setattr(random, "random", poison)
    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong@localhost/test_db")
    first = cli.render_plan_bytes(_actual_plan())
    second = cli.render_plan_bytes(_actual_plan())
    assert first == second == baseline


def test_plan_cli_now_emits_actual_identity_twice_byte_identically():
    cli = _cli()
    first_out, first_err = io.StringIO(), io.StringIO()
    second_out, second_err = io.StringIO(), io.StringIO()
    assert cli.run_cli(("--plan",), stdout=first_out, stderr=first_err) == 0
    assert cli.run_cli(("--plan",), stdout=second_out, stderr=second_err) == 0
    assert first_out.getvalue() == second_out.getvalue()
    assert json.loads(first_out.getvalue())["full_campaign_hash"] == (
        _FULL_CAMPAIGN_HASH
    )
    assert first_err.getvalue() == second_err.getvalue() == ""


def test_execution_plan_can_only_bind_exact_actual_identity_and_integration_pins(
    tmp_path: Path,
):
    materializer = _materializer()
    identity = _actual_plan()
    build = getattr(materializer, "build_production_execution_plan", None)
    assert callable(build), "CP8 requires the sealed production execution-plan adapter"
    output_root = (tmp_path / "scorecard-pair").resolve()
    plan = build(
        identity=identity,
        output_root=output_root,
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    assert type(plan) is materializer.ProductionExecutionPlan
    assert plan.output_root == output_root
    assert plan.full_campaign_hash == _FULL_CAMPAIGN_HASH
    assert plan.campaign_run_id == _CAMPAIGN_RUN_ID
    assert plan.exact_48_mapping_hash == _MAPPING_HASH
    assert plan.source_pins.runner_source_sha256 == _RUNNER_BUNDLE
    assert plan.source_pins.integration_head_sha == _INTEGRATION_HEAD
    assert plan.source_pins.integration_tree_sha == _INTEGRATION_TREE

    with pytest.raises(materializer.H6BPreflightRefused):
        build(
            identity=_cli().build_contract_fixture_plan(),
            output_root=output_root,
            integration_head_sha=_INTEGRATION_HEAD,
            integration_tree_sha=_INTEGRATION_TREE,
        )
    with pytest.raises((TypeError, materializer.H6BPlanError)):
        dataclasses.replace(plan, provenance="contract_fixture")


def test_actual_h6a_h4_h5_adapters_are_concrete_and_fixture_rejecting():
    materializer = _materializer()
    for name in (
        "ActualH4CampaignResult",
        "ActualMergedH6AAccounting",
        "ActualMergedH5Composition",
        "ProductionCampaignInput",
        "ProductionExecutionPorts",
    ):
        value = getattr(materializer, name, None)
        assert value is not None, f"CP8 missing actual adapter {name}"
    assert materializer.ActualMergedH6AAccounting.provenance == "actual_merged_h6a"
    assert materializer.ActualMergedH5Composition.provenance == "actual_merged_h5"


@pytest.mark.asyncio
async def test_one_shot_authority_is_bound_to_exact_output_and_source_plan(tmp_path):
    materializer = _materializer()
    identity = _actual_plan()
    plan_a = materializer.build_production_execution_plan(
        identity=identity,
        output_root=(tmp_path / "approved-output").resolve(),
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    plan_b = materializer.build_production_execution_plan(
        identity=identity,
        output_root=(tmp_path / "substituted-output").resolve(),
        integration_head_sha=_INTEGRATION_HEAD,
        integration_tree_sha=_INTEGRATION_TREE,
    )
    target = materializer.DatabaseTarget(
        host="db-authority.invalid",
        port=5432,
        database="rob974_db",
        user="rob974_runner_test",
    )
    authorization = materializer.issue_run_authorization(
        plan_a,
        materializer.RunAuthority(
            expected_full_campaign_hash=plan_a.full_campaign_hash,
            expected_campaign_run_id=plan_a.campaign_run_id,
            expected_exact_48_mapping_hash=plan_a.exact_48_mapping_hash,
            approved_target=target,
            observed_target=target,
            inherited_target=None,
            write_opt_in=True,
            expected_output_root=plan_a.output_root,
            requested_output_root=plan_a.output_root,
            expected_source_pins=plan_a.source_pins,
            observed_source_pins=plan_a.source_pins,
            one_shot_approval="cp8-plan-binding",
        ),
    )

    class PoisonRunner:
        provenance = "actual_merged_h4"

        async def run(self, _plan):
            raise AssertionError("mismatched authority reached H4")

    class PoisonArtifacts:
        provenance = "rob974_h6b_directory_atomic_v1"

    class PoisonInspector:
        provenance = "actual_read_only_campaign_state"

        async def inspect(self, _session, *, plan):
            del plan
            raise AssertionError("mismatched authority reached DB inspection")

    session_calls = 0

    def session_factory():
        nonlocal session_calls
        session_calls += 1
        raise AssertionError("mismatched authority reached session factory")

    guard_module = importlib.import_module("app.services.research_db_write_guard")
    campaign = materializer.ProductionCampaignInput(
        plan=plan_b,
        guard_policy=guard_module.ResearchDbPolicy.of(
            guard_module.ResearchDbTarget(host="localhost", database_name="test_db")
        ),
    )
    ports = materializer.ProductionExecutionPorts(
        session_factory=session_factory,
        h4_runner=PoisonRunner(),
        artifacts=PoisonArtifacts(),
        state_inspector=PoisonInspector(),
    )
    outcome = await materializer.materialize_production(
        plan=plan_b,
        authorization=authorization,
        campaign=campaign,
        ports=ports,
    )
    assert outcome.exit_code == materializer.AUTHORITY_OR_PREFLIGHT_REFUSED
    assert outcome.trace == ("preflight",)
    assert session_calls == 0
