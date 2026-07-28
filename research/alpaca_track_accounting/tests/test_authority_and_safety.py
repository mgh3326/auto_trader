"""ROB-1064 H6 — H2/H4 binding, current seal, and offline safety guards."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import accounting as acct
import authority
import pytest
import terminal_status as h4_terminal

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
    assert provenance.run_id == "rob1062-h4-synthetic-ac27-v2"


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
    assert (
        seal.semantic_hash
        == "b57a600c347b377e93565435502c6c9988063342d514eb26a0fcc1f6811556ef"
    )
    assert {trial.current_status for trial in seal.trials} == {"structural_incomplete"}
    assert all(
        cell.observation_count is None and cell.unobserved_reason
        for trial in seal.trials
        for cell in trial.cells
    )


def test_materialized_h4_terminal_artifact_produces_a_distinct_usable_seal() -> None:
    historical = authority.build_current_seal()
    resealed = authority.build_materialized_seal()

    assert historical.semantic_hash == (
        "b57a600c347b377e93565435502c6c9988063342d514eb26a0fcc1f6811556ef"
    )
    assert resealed.semantic_hash != historical.semantic_hash
    assert resealed.report.cells == 128
    assert resealed.report.structural_incomplete == 0
    assert resealed.report.performance_usable is True
    assert {trial.current_status for trial in resealed.trials} == {"executed"}
    assert all(
        cell.status == "executed"
        and cell.observation_count is not None
        and cell.unobserved_reason is None
        for trial in resealed.trials
        for cell in trial.cells
    )
    # Reaching here means the H4 loader accepted the source artifact, which
    # includes its ``degenerate_fold_replication`` gate: a seal can no longer
    # be built from an artifact whose folds are replicas of each other.
    artifact = h4_terminal.load_terminal_execution_artifact(
        h4_terminal.CANONICAL_TERMINAL_ARTIFACT_PATH
    )
    per_config: dict[tuple[str, str], set[str]] = {}
    for cell in artifact.cells:
        per_config.setdefault((cell.family, cell.config_id), set()).add(
            h4_terminal._fold_agnostic_count_digest(cell)
        )
    assert len(per_config) == 16
    assert all(len(digests) == 8 for digests in per_config.values())


def test_committed_current_report_is_exact_deterministic_generator_output() -> None:
    committed = (_PACKAGE / "sealed_reports" / "rob-1064-current.json").read_bytes()
    generated = authority.build_current_seal().to_bytes()

    assert committed == generated
    parsed = json.loads(committed)
    assert parsed["semantic_hash"] == authority.build_current_seal().semantic_hash


def test_committed_reseal_is_exact_deterministic_generator_output() -> None:
    committed = (
        _PACKAGE / "sealed_reports" / "rob-1064-run-2026-07-29-h4-terminal-v2.json"
    ).read_bytes()
    generated = authority.build_materialized_seal().to_bytes()

    assert committed == generated
    parsed = json.loads(committed)
    assert parsed["report"]["structural_incomplete"] == 0
    assert parsed["report"]["performance_usable"] is True


def test_every_earlier_seal_is_preserved_byte_for_byte() -> None:
    """Append-only in the strongest available form.

    The pre-materialization seal and the superseded v1 materialized seal are
    both pinned by content digest. A future reseal that deletes, truncates, or
    rewrites either one fails here. The v1 materialized seal is deliberately
    NOT required to be regenerable: the defective corpus identity it consumed
    no longer exists, and re-deriving it would mean keeping that corpus alive.
    """
    directory = _PACKAGE / "sealed_reports"
    assert set(authority.PRESERVED_SEAL_DIGESTS) == {
        "rob-1064-current.json",
        "rob-1064-run-2026-07-29-h4-terminal-v1.json",
    }
    for filename, digest in authority.PRESERVED_SEAL_DIGESTS.items():
        path = directory / filename
        assert path.is_file(), filename
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest, filename

    # The current seal is a NEW file, not a rewrite of either of them.
    assert authority.MATERIALIZED_SEAL_PATH.name not in (
        authority.PRESERVED_SEAL_DIGESTS
    )
    assert authority.MATERIALIZED_SEAL_PATH.is_file()
    assert {path.name for path in directory.glob("*.json")} == {
        *authority.PRESERVED_SEAL_DIGESTS,
        authority.MATERIALIZED_SEAL_PATH.name,
    }


def test_superseded_v1_seal_recorded_the_degenerate_campaign() -> None:
    """What the preserved v1 seal proves: counts were right, sample was not.

    Its report is a truthful record of the arithmetic; the reason it was
    blocked lives in the H4 artifact it consumed, not in these counts.
    """
    parsed = json.loads(
        (
            _PACKAGE / "sealed_reports" / "rob-1064-run-2026-07-29-h4-terminal-v1.json"
        ).read_bytes()
    )

    assert parsed["report"]["cells"] == 128
    assert parsed["report"]["structural_incomplete"] == 0
    assert parsed["report"]["performance_usable"] is True
    provenance_run_ids = {trial["provenance"]["run_id"] for trial in parsed["trials"]}
    assert provenance_run_ids == {"rob1062-h4-synthetic-ac27-v1"}
    # And the current authority no longer produces that identity.
    assert authority.canonical_trial_provenance().run_id == (
        "rob1062-h4-synthetic-ac27-v2"
    )


def test_h5_gate_accepts_the_current_seal_and_blocks_the_preserved_incomplete_one() -> (
    None
):
    """Boundary only — ``verify_seal_for_h5``. H5 itself is NOT run."""
    current = authority.build_materialized_seal()
    report = acct.verify_seal_for_h5(current)

    assert report.performance_usable is True
    assert report.structural_incomplete == 0
    assert report.cells == 128
    assert report.violations == ()

    with pytest.raises(acct.H5GateBlocked):
        acct.verify_seal_for_h5(authority.build_current_seal())


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
        "time",
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
        path.read_text(encoding="utf-8") for path in _PACKAGE.glob("*.py")
    ).lower()
    for token in forbidden_tokens:
        assert token not in source
