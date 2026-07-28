"""ROB-1064 H6 — H2/H4 binding, current seal, and offline safety guards."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import authority

_PACKAGE = Path(__file__).resolve().parents[1]


def test_authority_binds_exact_h2_configs_h4_folds_and_provenance() -> None:
    configs = authority.canonical_expected_configs()
    folds = authority.canonical_fold_ids()
    provenance = authority.canonical_trial_provenance()

    assert len(configs) == 16
    assert len({config.config_id for config in configs}) == 16
    assert len({config.config_hash for config in configs}) == 16
    assert folds == tuple(f"fold-{index}" for index in range(8))
    assert len(provenance.corpus_manifest_hash) == 64
    assert len(provenance.fold_schedule_hash) == 64
    assert len(provenance.code_hash) == 64
    assert provenance.run_id


def test_current_seal_is_truthful_structural_incomplete_not_fake_success() -> None:
    seal = authority.build_current_seal()

    assert seal.report.expected == 16
    assert seal.report.registered == 16
    assert seal.report.primary == 16
    assert seal.report.status_sum == 16
    assert seal.report.cells == 128
    assert seal.report.retry == 0
    assert seal.report.structural_incomplete == 16
    assert seal.report.performance_usable is False
    assert {trial.current_status for trial in seal.trials} == {
        "structural_incomplete"
    }
    assert all(
        cell.observation_count is None and cell.unobserved_reason
        for trial in seal.trials
        for cell in trial.cells
    )


def test_committed_current_report_is_exact_deterministic_generator_output() -> None:
    committed = (
        _PACKAGE / "sealed_reports" / "rob-1064-current.json"
    ).read_bytes()
    generated = authority.build_current_seal().to_bytes()

    assert committed == generated
    parsed = json.loads(committed)
    assert parsed["semantic_hash"] == authority.build_current_seal().semantic_hash


def test_reexecution_is_byte_identical() -> None:
    assert authority.build_current_seal().to_bytes() == (
        authority.build_current_seal().to_bytes()
    )


def test_package_has_no_runtime_or_side_effect_imports() -> None:
    forbidden_roots = {
        "app",
        "alpaca",
        "httpx",
        "requests",
        "socket",
        "sqlalchemy",
        "taskiq",
    }
    for path in _PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module.split(".", 1)[0]} if node.module else set()
            else:
                continue
            assert names.isdisjoint(forbidden_roots), (
                f"{path.name} imports forbidden runtime dependency "
                f"{sorted(names & forbidden_roots)}"
            )


def test_no_broker_order_scheduler_or_performance_surface() -> None:
    forbidden_tokens = (
        "broker.",
        "order_service",
        "@broker.task",
        "scheduler",
        "forward_return",
        "hit_rate",
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _PACKAGE.glob("*.py")
    ).lower()
    for token in forbidden_tokens:
        assert token not in source
