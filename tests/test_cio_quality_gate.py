"""Smoke test for scripts/cio_quality_gate.py against ROB-158 Scout Report.

Validates that the quality gate correctly detects G1 (depth), G4 (execution
path), and G6 (budget reality) hits on the past v1-format Scout Report that
motivated ROB-170 / ROB-197.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "cio_quality_gate.py"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "scout_reports" / "rob_158.md"


def _load_module():
    module_name = "cio_quality_gate"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


@pytest.fixture(scope="module")
def rob_158_md() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _gate(results, key):
    return next(r for r in results if r.key == key)


@pytest.mark.unit
def test_rob_158_g1_hits_on_shallow_candidates(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    g1 = _gate(results, "G1")
    assert not g1.passed, "G1 should hit on ROB-158"
    failed_codes = {c.code for c in cands if c.verdict == "fail"}
    assert "009150" in failed_codes, "삼성전기 009150 must be in G1 fails"
    assert "006400" in failed_codes, "삼성SDI 006400 must be in G1 fails"


@pytest.mark.unit
def test_rob_158_g4_hits_on_new_candidates_with_bare_kis(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    g4 = _gate(results, "G4")
    assert not g4.passed, "G4 should hit on ROB-158"
    new_cands = [c for c in cands if c.is_new]
    new_codes = {c.code for c in new_cands}
    assert "259960" in new_codes and "011070" in new_codes, (
        "Krafton and LG이노텍 must be detected as new candidates"
    )


@pytest.mark.unit
def test_rob_158_g6_hits_no_cash_balance_call(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    g6 = _gate(results, "G6")
    assert not g6.passed, "G6 should hit — get_cash_balance was not called"
    assert "get_cash_balance: 없음" in g6.detail


@pytest.mark.unit
def test_rob_158_g3_g5_pass_and_g2_soft_warns(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    g2 = _gate(results, "G2")
    g3 = _gate(results, "G3")
    g5 = _gate(results, "G5")
    assert g3.passed, "ROB-158 has no explicit tool failure signals"
    assert g5.passed, "ROB-158 has DCA vs new comparison text"
    assert not g2.passed, "ROB-158 grouped rejection uses '…등' without breakdown"
    assert g2.severity == "soft"


@pytest.mark.unit
def test_rob_158_exit_code_reopen(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    hard_failed = any(r.severity == "hard" and not r.passed for r in results)
    assert hard_failed, "ROB-158 should exit code 2 (REOPEN)"


@pytest.mark.unit
def test_reopen_comment_template_includes_all_hard_gates(mod, rob_158_md):
    cands = mod.extract_candidates(rob_158_md)
    results = mod.run_gates(rob_158_md, cands)
    reopen = mod.build_reopen_comment(results)
    assert reopen is not None
    assert "G1 Depth" in reopen
    assert "G4 Execution path" in reopen
    assert "G6 Budget reality" in reopen
