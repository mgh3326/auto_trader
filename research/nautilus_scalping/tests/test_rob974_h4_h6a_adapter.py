"""ROB-982 CP9 -- real H4 source pins and H6-A production identity closure."""

from __future__ import annotations

import ast
import hashlib
import importlib
from pathlib import Path

import pytest
import rob974_h3_manifest as h3_manifest
import rob974_h6a_identity as h6a_identity
import rob974_h6a_payload as h6a_payload

from research_contracts.canonical_hash import canonical_sha256


def _adapter():
    return importlib.import_module("rob974_h4_h6a_adapter")


def test_source_bundle_seal_is_derived_from_ordered_raw_bytes(tmp_path: Path) -> None:
    adapter = _adapter()
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_bytes(b"first\r\nraw\n")
    second.write_bytes(b"second\x00raw\n")
    files = (("first.py", first), ("nested/second.py", second))

    expected = canonical_sha256(
        {
            "schema_version": "rob974.source_bundle.v1",
            "files": [
                {
                    "logical_path": "first.py",
                    "raw_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                },
                {
                    "logical_path": "nested/second.py",
                    "raw_sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
                },
            ],
        }
    )
    assert adapter.source_bundle_sha256(files) == expected
    assert adapter.source_bundle_sha256(tuple(reversed(files))) != expected


def test_source_bundle_rejects_duplicate_logical_path_or_missing_file(
    tmp_path: Path,
) -> None:
    adapter = _adapter()
    source = tmp_path / "source.py"
    source.write_bytes(b"source")
    with pytest.raises(adapter.SourcePinError):
        adapter.source_bundle_sha256((("same.py", source), ("same.py", source)))
    with pytest.raises(adapter.SourcePinError):
        adapter.source_bundle_sha256((("missing.py", tmp_path / "missing.py"),))


def test_production_source_pins_match_each_exact_raw_file_bundle() -> None:
    adapter = _adapter()
    pins = adapter.build_production_source_pins()
    assert pins.feature_source_sha256 == adapter.source_bundle_sha256(
        adapter.FEATURE_SOURCE_FILES
    )
    assert pins.engine_source_sha256 == adapter.source_bundle_sha256(
        adapter.ENGINE_SOURCE_FILES
    )
    assert pins.runner_source_sha256 == adapter.source_bundle_sha256(
        adapter.RUNNER_SOURCE_FILES
    )
    assert pins.pbo_implementation_sha256 == adapter.source_bundle_sha256(
        adapter.PBO_SOURCE_FILES
    )
    pins.require_production_ready()
    assert len(set(pins.as_dict().values())) == 4


def test_production_source_inventory_is_closed_repo_relative_and_nonoverlapping() -> (
    None
):
    adapter = _adapter()
    inventories = (
        adapter.FEATURE_SOURCE_FILES,
        adapter.ENGINE_SOURCE_FILES,
        adapter.RUNNER_SOURCE_FILES,
        adapter.PBO_SOURCE_FILES,
    )
    logical_paths = [logical for inventory in inventories for logical, _ in inventory]
    assert len(logical_paths) == len(set(logical_paths))
    assert all(not Path(logical).is_absolute() for logical in logical_paths)
    assert all(path.is_file() for inventory in inventories for _, path in inventory)
    assert "research/nautilus_scalping/rob974_h4_h6a_adapter.py" in logical_paths
    assert "research/nautilus_scalping/rob974_h4_pbo.py" in logical_paths


def test_production_plan_is_exact_48_h6a_identity_and_deterministic() -> None:
    adapter = _adapter()
    first = adapter.build_production_h4_plan()
    second = adapter.build_production_h4_plan()

    assert first.expected_attempt_ids == h6a_identity.CANONICAL_ROW_ORDER
    assert tuple(spec.row_id for spec in first.row_specs) == first.expected_attempt_ids
    assert len(first.row_specs) == 48
    assert all(spec.provenance == "production" for spec in first.row_specs)
    assert first.envelope.mode == "production_plan"
    assert first.source_pins == adapter.build_production_source_pins()
    assert (
        first.h4_source_pins.runner_bundle_sha256
        == first.source_pins.runner_source_sha256
    )
    assert (
        first.h4_source_pins.pbo_source_sha256
        == first.source_pins.pbo_implementation_sha256
    )
    assert first.full_campaign_hash == first.envelope.full_campaign_hash()
    assert first.campaign_run_id == h6a_payload.derive_primary_run_id(
        first.full_campaign_hash
    )
    assert first.to_dict() == second.to_dict()


def test_production_plan_commits_fold_scenario_gate_and_pbo_authorities() -> None:
    plan = _adapter().build_production_h4_plan()
    payload = plan.envelope.to_dict()
    policy = payload["campaign_policy"]

    assert tuple(fold["fold_id"] for fold in policy["folds"]) == tuple(
        f"fold-{index:02d}" for index in range(8)
    )
    assert policy["embargo_hours"] == 3
    assert tuple(policy["path_membership"]) == (
        "base13",
        "primary_stress17",
        "upward_stress22",
    )
    assert [
        policy["path_membership"][name]["round_trip_all_in_bps"]
        for name in policy["path_membership"]
    ] == [13.0, 17.0, 22.0]
    assert policy["pbo_contract"] == {
        "window_start_ms": 1_751_328_000_000,
        "window_end_ms": 1_782_864_000_000,
        "path_scenario": "primary_stress17",
        "configs_per_strategy": 24,
        "days_per_config": 365,
        "slices": 4,
        "reference_only": True,
    }
    assert policy["gates_bins"]["scorecard"]["common"]["E17_min_bps"] == 5.0
    assert policy["funding_policy"]["strict_expected_debit_limit_bps"] == 3.0
    assert payload["source_pins"] == plan.source_pins.as_dict()


def test_production_plan_uses_verified_parent_and_real_h3_contracts() -> None:
    plan = _adapter().build_production_h4_plan()
    payload = plan.envelope.to_dict()
    assert payload["parent_corpus"]["content_sha256"] == (
        "4bcc2da979b47caa45b5f90a09c326aefff91fa605e110d55ef316d53c9a9351"
    )
    assert payload["parent_corpus"]["physical_manifest_sha256"] == (
        "0767b44f976bf717cdc26bbcb0d01da1800418668f9f153461ce62486de10721"
    )
    by_id = {spec.row_id: spec for spec in plan.row_specs}
    assert by_id["S3-00"].components["code"]["contract_hash"] == (
        h3_manifest.S3_STRATEGY_CONTRACT.contract_hash
    )
    assert by_id["S4-00"].components["code"]["contract_hash"] == (
        h3_manifest.S4_STRATEGY_CONTRACT.contract_hash
    )


def test_coordinated_h3_drift_fails_closed_before_production_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    monkeypatch.setattr(h3_manifest, "RESEARCH_DOCUMENT_SHA256", "f" * 64)
    with pytest.raises(adapter.ContractDriftError):
        adapter.build_production_h4_plan()


def test_cp9_adapter_has_no_runtime_or_side_effect_imports() -> None:
    adapter = _adapter()
    tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint(
        {
            "app",
            "asyncio",
            "datetime",
            "httpx",
            "os",
            "random",
            "requests",
            "socket",
            "sqlalchemy",
            "subprocess",
        }
    )
