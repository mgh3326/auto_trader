"""ROB-1062 H4 terminal execution-status artifact contracts.

This suite is deliberately count/status-only.  It loads the committed
artifact and validates blind-count completeness without calling the H4
performance path or unmasking any value.

It also pins the contract the v1 campaign was missing: a complete cell count
is NOT evidence of that many observations.  ``structural_incomplete == 0`` and
"the eight folds observed eight different periods" are separate assertions and
both are made here.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import pytest
import terminal_status as ts

_PACKAGE = Path(__file__).resolve().parents[1]
_ARTIFACT = _PACKAGE / "terminal_artifacts" / "rob1062-h4-synthetic-ac27-v2.json"
_V1_ARTIFACT = _PACKAGE / "terminal_artifacts" / "rob1062-h4-synthetic-ac27-v1.json"

# The exact committed bytes of the superseded v1 artifact. It is the input the
# 2026-07-29 v1 H6 seal consumed, so it must stay on disk unmodified.
_V1_ARTIFACT_SHA256 = "3021dde006c58545d58a6d6952c0957e4ec672e55f51984ec0d704a8e272f2e1"


def _fold_payloads_by_config(
    artifact: ts.TerminalExecutionArtifact,
) -> dict[tuple[str, str], dict[str, str]]:
    """Map each config to {fold_id: digest of that fold's counts}."""

    grouped: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for cell in artifact.cells:
        grouped[(cell.family, cell.config_id)][cell.fold_id] = (
            ts._fold_agnostic_count_digest(cell)
        )
    return grouped


def test_committed_terminal_artifact_is_exactly_16_configs_x_8_folds() -> None:
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)

    assert artifact.run_id == "rob1062-h4-synthetic-ac27-v2"
    assert len(artifact.cells) == 128
    assert len({(cell.family, cell.config_id) for cell in artifact.cells}) == 16
    assert {cell.fold_id for cell in artifact.cells} == {
        f"fold-{index}" for index in range(8)
    }
    assert {cell.status for cell in artifact.cells} == {"executed"}
    assert all(cell.observation_count > 0 for cell in artifact.cells)
    assert all(not cell.train_blind_counts.is_incomplete for cell in artifact.cells)
    assert all(not cell.oos_blind_counts.is_incomplete for cell in artifact.cells)


def test_committed_artifact_holds_128_distinct_observations_not_16_replicated() -> None:
    """The v1 BLOCK condition, pinned as a permanent contract.

    v1 passed every count check with 16 distinct count payloads replicated
    across eight folds. Complete-and-degenerate must never pass again.
    """
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)
    grouped = _fold_payloads_by_config(artifact)

    assert len(grouped) == 16
    for key, by_fold in grouped.items():
        assert len(by_fold) == 8, key
        assert len(set(by_fold.values())) == 8, (
            f"{key} does not observe eight distinct periods: "
            f"{len(set(by_fold.values()))}/8 distinct fold payloads"
        )

    every_cell = {digest for by_fold in grouped.values() for digest in by_fold.values()}
    assert len(every_cell) == 128


def test_observation_count_alone_cannot_detect_fold_replication() -> None:
    """Why the gate must read the FULL blind counts.

    ``observation_count`` is fixed by the decision calendar, so it is equal
    across folds of equal length even when the folds are genuinely different
    periods. A degeneracy check built on it would be vacuous.
    """
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)
    counts_by_config: dict[tuple[str, str], set[int]] = defaultdict(set)
    for cell in artifact.cells:
        counts_by_config[(cell.family, cell.config_id)].add(cell.observation_count)

    assert all(len(values) == 1 for values in counts_by_config.values())


def test_artifact_rejects_folds_that_are_numerical_replicas() -> None:
    """The ``degenerate_fold_replication`` gate is non-vacuous."""
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)
    by_key = {cell.key: cell for cell in artifact.cells}
    template = by_key[("AP-A2", "AP-A2-00", "fold-0")]
    replicated_cells = tuple(
        replace(template, fold_id=cell.fold_id)
        if (cell.family, cell.config_id) == ("AP-A2", "AP-A2-00")
        else cell
        for cell in artifact.cells
    )

    with pytest.raises(ts.TerminalArtifactError, match=ts.DEGENERATE_FOLD_REPLICATION):
        ts.TerminalExecutionArtifact(
            run_id=artifact.run_id,
            corpus_manifest_hash=artifact.corpus_manifest_hash,
            fold_schedule_hash=artifact.fold_schedule_hash,
            code_hash=artifact.code_hash,
            cells=replicated_cells,
            artifact_hash=artifact.artifact_hash,
        )


def test_terminal_artifact_is_byte_stable_and_bound_to_current_execution() -> None:
    committed = _ARTIFACT.read_bytes()
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)

    assert artifact.to_bytes() == committed
    assert artifact.fold_schedule_hash == ts.canonical_fold_schedule_hash()
    assert artifact.code_hash == ts.canonical_execution_code_hash()


def test_superseded_v1_artifact_is_preserved_and_not_loadable() -> None:
    """The evidence behind the v1 seal stays on disk, and stays rejected.

    Preservation and acceptance are different things: the bytes must survive
    verbatim, and the loader must refuse them because they were produced by a
    corpus identity that no longer exists.
    """
    assert ts.HISTORICAL_TERMINAL_ARTIFACT_PATHS == (_V1_ARTIFACT,)
    raw = _V1_ARTIFACT.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _V1_ARTIFACT_SHA256

    payload = json.loads(raw)
    assert payload["run_id"] == "rob1062-h4-synthetic-ac27-v1"
    assert len(payload["cells"]) == 128

    # The degeneracy gate rejects it before provenance is even reached: the
    # v1 artifact is exactly the shape the new gate exists to stop.
    with pytest.raises(ts.TerminalArtifactError, match=ts.DEGENERATE_FOLD_REPLICATION):
        ts.load_terminal_execution_artifact(_V1_ARTIFACT)


def test_v1_artifact_is_the_degenerate_shape_this_fix_removed() -> None:
    """Documents the defect against the preserved evidence itself."""
    payload = json.loads(_V1_ARTIFACT.read_bytes())
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cell in payload["cells"]:
        digest = json.dumps(
            {key: value for key, value in cell.items() if key != "fold_id"},
            sort_keys=True,
        )
        grouped[(cell["family"], cell["config_id"])].add(digest)

    assert len(grouped) == 16
    # Every one of the 16 configs had all eight folds byte-identical.
    assert all(len(values) == 1 for values in grouped.values())
    assert {cell["status"] for cell in payload["cells"]} == {"executed"}


def test_terminal_artifact_contains_no_performance_surface() -> None:
    payload = json.loads(_ARTIFACT.read_bytes())
    serialized = json.dumps(payload, sort_keys=True).lower()

    for forbidden in ("forward_return", "pnl", "hit_rate", "directional_accuracy"):
        assert forbidden not in serialized


def test_terminal_materializer_calls_no_performance_or_reveal_helpers() -> None:
    source = (_PACKAGE / "terminal_status.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {
        "actual_fill_pnl_bp",
        "gross_pnl_bp",
        "shadow_net_pnl_bp",
        "three_view_pnl_bp",
        "unmask",
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called.isdisjoint(forbidden_calls)


def test_loader_rejects_semantic_tampering(tmp_path: Path) -> None:
    payload = json.loads(_ARTIFACT.read_bytes())
    payload["cells"][0]["oos_blind_counts"]["total_decision_records"] += 1
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    try:
        ts.load_terminal_execution_artifact(tampered)
    except ts.TerminalArtifactError as exc:
        assert "artifact hash" in str(exc)
    else:
        raise AssertionError("tampered terminal artifact was accepted")
