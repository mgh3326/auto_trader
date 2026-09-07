"""ROB-1351 closure, v2 registration, and discovery-policy regression tests."""

from __future__ import annotations

import ast
import inspect
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

import app.mcp_server.tooling.buy_candidate_fanout as fanout
import app.services.buy_gate_ab_shadow.epoch_v2 as epoch_v2
from app.models.buy_gate_ab_experiment_lifecycle import (
    BuyGateABCollectionEpochV2,
    BuyGateABExperimentRegistration,
    BuyGateABExperimentTermination,
)
from app.services.buy_gate_ab_shadow.epoch_v2 import assert_v2_seal, build_marker
from app.services.buy_gate_ab_shadow.spec import (
    EXPERIMENT_ID,
    PINNED_POLICY_PROJECTION_SHA256,
    PINNED_SPEC_SHA256,
    policy_projection_sha256,
    spec_sha256,
)
from app.services.buy_gate_ab_shadow.spec_v2 import (
    EXPERIMENT_ID_V2,
    PINNED_POLICY_PROJECTION_SHA256_V2,
    PINNED_SPEC_SHA256_V2,
    POLICY_PROJECTION_V2,
    PRE_REGISTRATION_V2,
    policy_projection_sha256_v2,
    spec_sha256_v2,
)
from app.services.buy_gate_ab_shadow.termination import (
    ROB_1301_TERMINATION,
    ExperimentTerminationError,
    assert_predecessor_seal_intact,
    terminal_report,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    REPO_ROOT / "alembic" / "versions" / "20260907_rob1351_buy_gate_lifecycle.py"
)
BASELINE = REPO_ROOT / "tests" / "fixtures" / "trading_policy_rob1289_baseline.yaml"
POLICY = REPO_ROOT / "config" / "trading_policy.yaml"


def _migration_upgrade_source() -> str:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    return ast.get_source_segment(MIGRATION.read_text(encoding="utf-8"), upgrade) or ""


def _policy_raw() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


def test_rob1301_seals_remain_intact_after_termination() -> None:
    assert spec_sha256() == PINNED_SPEC_SHA256
    assert policy_projection_sha256() == PINNED_POLICY_PROJECTION_SHA256
    assert_predecessor_seal_intact()
    assert ROB_1301_TERMINATION.experiment_id == EXPERIMENT_ID


def test_terminal_report_is_insufficient_sample_without_performance_metrics() -> None:
    report = terminal_report()

    assert report["status"] == "INSUFFICIENT_SAMPLE"
    assert report["outcome"] == "NO_FIRING"
    assert report["score_computation"] == "not_applicable_stopped_by_operator_decision"
    assert report["carryover"] == "forbidden"
    assert report["winner_declaration"] == "forbidden"
    assert report["policy_implication"] == "none"
    assert report["terminated_at"] == "2026-09-07T09:43:42+09:00"
    assert report["preregistration_spec_sha256"] == PINNED_SPEC_SHA256
    assert report["policy_projection_sha256"] == PINNED_POLICY_PROJECTION_SHA256
    assert not {
        key for key in report if "return" in key.lower() or "drawdown" in key.lower()
    }


def test_termination_record_rejects_naive_empty_and_unsealed_values() -> None:
    with pytest.raises(ExperimentTerminationError, match="timezone-aware"):
        replace(
            ROB_1301_TERMINATION,
            terminated_at=ROB_1301_TERMINATION.terminated_at.replace(tzinfo=None),
        )
    with pytest.raises(ExperimentTerminationError, match="non-empty"):
        replace(ROB_1301_TERMINATION, decided_by="")
    with pytest.raises(ExperimentTerminationError, match="lowercase SHA-256"):
        replace(ROB_1301_TERMINATION, preregistration_spec_sha256="0" * 63)
    with pytest.raises(ExperimentTerminationError, match="differs from ROB-1301 pin"):
        replace(ROB_1301_TERMINATION, preregistration_spec_sha256="0" * 64)


def test_v2_predecessor_and_termination_forbid_carryover_with_db_check() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert PRE_REGISTRATION_V2["predecessor"]["carried_over_samples"] == "forbidden"
    assert ROB_1301_TERMINATION.carryover == "forbidden"
    assert "carryover = 'forbidden'" in source
    assert "buy_gate_ab_experiment_termination" in source
    assert "buy_gate_ab_experiment_registration" in source


def test_v2_hashes_are_pinned_and_any_payload_edit_breaks_each_pin() -> None:
    assert spec_sha256_v2() == PINNED_SPEC_SHA256_V2
    assert policy_projection_sha256_v2() == PINNED_POLICY_PROJECTION_SHA256_V2
    assert_v2_seal()

    mutated_spec = deepcopy(PRE_REGISTRATION_V2)
    mutated_spec["hypothesis"] = f"{mutated_spec['hypothesis']}!"
    assert spec_sha256_v2(mutated_spec) != PINNED_SPEC_SHA256_V2

    mutated_projection = deepcopy(POLICY_PROJECTION_V2)
    mutated_projection["variant_b"]["support_strength_min"] = "moderate"
    assert (
        policy_projection_sha256_v2(mutated_projection)
        != PINNED_POLICY_PROJECTION_SHA256_V2
    )


def test_v2_hashes_are_distinct_from_the_preserved_v1_hashes() -> None:
    assert PINNED_SPEC_SHA256_V2 != PINNED_SPEC_SHA256
    assert PINNED_POLICY_PROJECTION_SHA256_V2 != PINNED_POLICY_PROJECTION_SHA256


def test_v2_control_arm_and_observational_cohort_contract() -> None:
    variant_a = PRE_REGISTRATION_V2["variant_a"]
    variant_b = PRE_REGISTRATION_V2["variant_b"]
    scoring = PRE_REGISTRATION_V2["scoring"]

    assert PRE_REGISTRATION_V2["experiment_id"] == EXPERIMENT_ID_V2
    assert variant_a == {
        "label": "A",
        "role": "live",
        "support_strength_min": "moderate",
        "executes": True,
        "cohort_labels": ["strong", "moderate_only"],
    }
    assert variant_b == {
        "label": "B",
        "role": "shadow",
        "support_strength_min": "weak",
        "executes": False,
        "register_as": "shadow_buy",
    }
    assert PRE_REGISTRATION_V2["only_difference"] == "support_strength_min"
    assert scoring["cohort_split_is_observational_not_randomized"] is True
    assert scoring["winner_declaration"] == "forbidden"
    assert POLICY_PROJECTION_V2["variant_a"] == {
        "label": "A",
        "role": "live",
        "support_strength_min": "moderate",
        "executes": True,
    }


def test_v2_epoch_is_unarmed_but_can_build_the_actual_marker_shape_later() -> None:
    assert not hasattr(epoch_v2, "COLLECTION_EPOCH_V2")
    assert "COLLECTION_EPOCH_V2" not in inspect.getsource(epoch_v2)

    fixture_armed_at = datetime.now(UTC)
    fixture_start = fixture_armed_at.astimezone(
        ZoneInfo("Asia/Seoul")
    ).date() + timedelta(days=1)
    marker = build_marker(
        epoch_id="rob-1351-collection-epoch.v1",
        addendum_version="rob-1351-activation-epoch.v1",
        collection_armed_at=fixture_armed_at,
        collection_start=fixture_start,
        collection_end_exclusive=fixture_start + timedelta(days=28),
        collection_calendar_days=28,
        collection_clock_timezone="Asia/Seoul",
        market_session_timezones=(
            ("kr", "Asia/Seoul"),
            ("us", "America/New_York"),
        ),
    )
    assert marker.experiment_id == EXPERIMENT_ID_V2
    assert marker.preregistration_spec_sha256 == PINNED_SPEC_SHA256_V2
    assert marker.policy_projection_sha256 == PINNED_POLICY_PROJECTION_SHA256_V2


def test_migration_does_not_insert_an_unarmed_v2_epoch_row() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    upgrade = _migration_upgrade_source()
    epoch_create = 'op.create_table(\n        "buy_gate_ab_collection_epoch_v2"'

    assert epoch_create in upgrade
    after_epoch_create = upgrade.split(epoch_create, maxsplit=1)[1]
    assert "bulk_insert" not in after_epoch_create
    assert "INSERT INTO review.buy_gate_ab_collection_epoch_v2" not in source
    assert (
        "collection_armed_at" not in BuyGateABExperimentRegistration.__table__.columns
    )
    assert "collection_armed_at" in BuyGateABCollectionEpochV2.__table__.columns


def test_migration_upgrade_is_additive_and_leaves_the_v1_epoch_untouched() -> None:
    upgrade = _migration_upgrade_source()

    assert "op.drop_table(" not in upgrade
    assert not re.search(r"(?m)^\s*(?:UPDATE|DELETE|TRUNCATE|DROP\s+TABLE)\b", upgrade)
    assert '"buy_gate_ab_collection_epoch"' not in upgrade
    assert "buy_gate_ab_experiment_termination" in upgrade
    assert "buy_gate_ab_experiment_registration" in upgrade
    assert "BEFORE UPDATE OR DELETE" in MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE TRUNCATE" in MIGRATION.read_text(encoding="utf-8")
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in MIGRATION.read_text(encoding="utf-8")
    assert "terminated_at" in BuyGateABExperimentTermination.__table__.columns


def test_fanout_discovery_strength_is_frozen_to_moderate_and_separate_from_reserve() -> (
    None
):
    assert (
        _policy_raw()["thresholds"]["screen.support_strength_min"]["value"]
        == "moderate"
    )

    gates = fanout._FanoutGates.from_policy(fanout.load_trading_policy())

    assert gates.discovery_support_strength_min == "moderate"
    assert gates.support_strength_min == "moderate"
    assert gates.as_dict()["discovery_support_strength_min"] == "moderate"
    assert gates.as_dict()["support_strength_min"] == "moderate"


def test_fanout_missing_discovery_strength_key_fails_closed() -> None:
    policy = fanout.load_trading_policy().model_copy(deep=True)
    del policy.thresholds["screen.support_strength_min"]

    with pytest.raises(ValueError, match="screen\\.support_strength_min"):
        fanout._FanoutGates.from_policy(policy)


def test_fanout_rejects_discovery_strength_outside_closed_vocabulary() -> None:
    policy = fanout.load_trading_policy().model_copy(deep=True)
    policy.thresholds["screen.support_strength_min"].value = "bogus"

    with pytest.raises(ValueError, match="must be one of weak, moderate, strong"):
        fanout._FanoutGates.from_policy(policy)


def test_fanout_rejects_discovery_strength_other_than_operator_frozen_moderate() -> (
    None
):
    policy = fanout.load_trading_policy().model_copy(deep=True)
    policy.thresholds["screen.support_strength_min"].value = "strong"

    with pytest.raises(ValueError, match="fanout gate literals"):
        fanout._FanoutGates.from_policy(policy)


def test_policy_regression_keeps_all_non_discovery_strength_boundaries() -> None:
    current = _policy_raw()
    baseline = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))

    for key, expected in (
        ("screen.rsi_max", 45),
        ("screen.support_within_pct", 8),
        ("screen.upside_min_pct", 40),
        ("portfolio.sector_cluster_cap_pct", 10),
        ("portfolio.max_symbols_per_theme", 2),
    ):
        assert current["thresholds"][key]["value"] == expected
        assert (
            current["thresholds"][key]["value"] == baseline["thresholds"][key]["value"]
        )

    current_reserve = current["decision_rules"]["buy.support_reserve_net"]
    baseline_reserve = baseline["decision_rules"]["buy.support_reserve_net"]
    for key in (
        "support_strength_min",
        "independent_support_source_count_min",
        "independent_support_source_families",
        "support_within_current_pct_max",
        "honest_upside_pct_min",
    ):
        assert current_reserve[key] == baseline_reserve[key]
