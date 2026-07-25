from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DESIGN = (
    ROOT / "docs/superpowers/specs/2026-07-13-rob-847-honest-offline-gate-design.md"
)
REASON_SOURCES = (
    ROOT / "backtest/prepare.py",
    ROOT / "research_contracts/trial_evidence.py",
    ROOT / "research_contracts/honest_offline_gate.py",
    ROOT / "app/services/research_offline_gate_service.py",
)
ERROR_CALLS = {
    "EvaluationWindowError",
    "OfflineGateFinalizeError",
    "SealedOOSArtifactError",
    "TrialEvidenceError",
}
RESULT_CALLS = {"StatisticResult", "FDRResult"}


def _strings(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _implemented_reason_codes() -> set[str]:
    reasons: set[str] = set()
    for source in REASON_SOURCES:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"add", "append"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "reasons"
                and node.args
            ):
                reasons.update(_strings(node.args[0]))
            if isinstance(node.func, ast.Name) and node.func.id in ERROR_CALLS:
                if node.args:
                    reasons.update(_strings(node.args[0]))
            if isinstance(node.func, ast.Name) and node.func.id in RESULT_CALLS:
                if len(node.args) >= 2:
                    reasons.update(_strings(node.args[1]))
    return reasons


def test_design_lists_every_implemented_stable_reason_code_exactly() -> None:
    design = DESIGN.read_text(encoding="utf-8")
    section = design.split("## Failure normalization", 1)[1].split(
        "## Test strategy", 1
    )[0]
    documented = set(re.findall(r"`([a-z][a-z0-9_]+)`", section))

    assert documented == _implemented_reason_codes()
