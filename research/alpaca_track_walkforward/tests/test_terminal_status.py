"""ROB-1062 H4 terminal execution-status artifact contracts.

This suite is deliberately count/status-only.  It loads the committed
artifact and validates blind-count completeness without calling the H4
performance path or unmasking any value.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import terminal_status as ts

_PACKAGE = Path(__file__).resolve().parents[1]
_ARTIFACT = _PACKAGE / "terminal_artifacts" / "rob1062-h4-synthetic-ac27-v1.json"


def test_committed_terminal_artifact_is_exactly_16_configs_x_8_folds() -> None:
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)

    assert artifact.run_id == "rob1062-h4-synthetic-ac27-v1"
    assert len(artifact.cells) == 128
    assert len({(cell.family, cell.config_id) for cell in artifact.cells}) == 16
    assert {cell.fold_id for cell in artifact.cells} == {
        f"fold-{index}" for index in range(8)
    }
    assert {cell.status for cell in artifact.cells} == {"executed"}
    assert all(cell.observation_count > 0 for cell in artifact.cells)
    assert all(not cell.train_blind_counts.is_incomplete for cell in artifact.cells)
    assert all(not cell.oos_blind_counts.is_incomplete for cell in artifact.cells)


def test_terminal_artifact_is_byte_stable_and_bound_to_current_execution() -> None:
    committed = _ARTIFACT.read_bytes()
    artifact = ts.load_terminal_execution_artifact(_ARTIFACT)

    assert artifact.to_bytes() == committed
    assert artifact.fold_schedule_hash == ts.canonical_fold_schedule_hash()
    assert artifact.code_hash == ts.canonical_execution_code_hash()


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
