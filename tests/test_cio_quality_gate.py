"""Smoke tests for the CIO quality gate service against the ROB-158 Scout Report.

Validates that :func:`evaluate_scout_report` correctly detects G1 (depth),
G4 (execution path), and G6 (budget reality) hits on the past v1-format
Scout Report that motivated ROB-170 / ROB-197.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.cio_quality_gate_service import (
    build_reopen_comment,
    evaluate_scout_report,
    extract_candidates,
    run_gates,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "scout_reports" / "rob_158.md"


@pytest.fixture(scope="module")
def rob_158_md() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _gate(results, key):
    return next(r for r in results if r.key == key)


def _violation(report, gate_id):
    return next((v for v in report.violations if v.gate_id == gate_id), None)


@pytest.mark.unit
def test_rob_158_g1_hits_on_shallow_candidates(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    g1 = _gate(results, "G1")
    assert not g1.passed, "G1 should hit on ROB-158"
    failed_codes = {c.code for c in cands if c.verdict == "fail"}
    assert "009150" in failed_codes, "삼성전기 009150 must be in G1 fails"
    assert "006400" in failed_codes, "삼성SDI 006400 must be in G1 fails"


@pytest.mark.unit
def test_rob_158_g4_hits_on_new_candidates_with_bare_kis(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    g4 = _gate(results, "G4")
    assert not g4.passed, "G4 should hit on ROB-158"
    new_cands = [c for c in cands if c.is_new]
    new_codes = {c.code for c in new_cands}
    assert "259960" in new_codes and "011070" in new_codes, (
        "Krafton and LG이노텍 must be detected as new candidates"
    )


@pytest.mark.unit
def test_rob_158_g6_hits_no_cash_balance_call(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    g6 = _gate(results, "G6")
    assert not g6.passed, "G6 should hit — get_cash_balance was not called"
    assert "get_cash_balance: 없음" in g6.detail


@pytest.mark.unit
def test_rob_158_g3_g5_pass_and_g2_soft_warns(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    g2 = _gate(results, "G2")
    g3 = _gate(results, "G3")
    g5 = _gate(results, "G5")
    assert g3.passed, "ROB-158 has no explicit tool failure signals"
    assert g5.passed, "ROB-158 has DCA vs new comparison text"
    assert not g2.passed, "ROB-158 grouped rejection uses '…등' without breakdown"
    assert g2.severity == "soft"


@pytest.mark.unit
def test_rob_158_exit_code_reopen(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    hard_failed = any(r.severity == "hard" and not r.passed for r in results)
    assert hard_failed, "ROB-158 should exit code 2 (REOPEN)"


@pytest.mark.unit
def test_reopen_comment_template_includes_all_hard_gates(rob_158_md):
    cands = extract_candidates(rob_158_md)
    results = run_gates(rob_158_md, cands)
    reopen = build_reopen_comment(results)
    assert reopen is not None
    assert "G1 Depth" in reopen
    assert "G4 Execution path" in reopen
    assert "G6 Budget reality" in reopen


# ---------------------------------------------------------------------------
# evaluate_scout_report() — top-level service API contract (ROB-196 e2e base)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_evaluate_scout_report_rob_158_overall_fail(rob_158_md):
    report = evaluate_scout_report(markdown=rob_158_md)
    assert report.overall_status == "FAIL"
    gate_ids = {v.gate_id for v in report.violations}
    # Hard-gate hits from rob_158: G1 depth, G4 execution path, G6 budget
    assert {"G1", "G4", "G6"}.issubset(gate_ids)
    # Soft-gate warning: G2 grouped rejection truncated with '…등'
    g2 = _violation(report, "G2")
    assert g2 is not None and g2.severity == "soft"
    assert report.reopen_comment is not None
    assert "G1 Depth" in report.reopen_comment


@pytest.mark.unit
def test_evaluate_scout_report_cash_balance_override_clears_g6_call_flag(rob_158_md):
    """Caller-supplied cash_balance should credit the get_cash_balance check.

    rob_158 does not call get_cash_balance in the body, so G6 fails on that
    alone. Supplying cash_balance out-of-band resolves the call evidence;
    whether G6 still fails then depends on the over-budget ratio.
    """
    report = evaluate_scout_report(markdown=rob_158_md, cash_balance=50_000_000)
    g6_gate = next(r for r in report.gates if r.key == "G6")
    assert "caller-supplied" in g6_gate.detail


@pytest.mark.unit
def test_evaluate_scout_report_tool_failures_merged_into_g3(rob_158_md):
    """Extra tool_failures argument must fail G3 when `### 제한사항` is absent."""
    report = evaluate_scout_report(
        markdown=rob_158_md,
        tool_failures=["screen_stocks: schema mismatch"],
    )
    g3 = _violation(report, "G3")
    assert g3 is not None, "G3 must fail when tool_failures passed without 제한사항"
    assert g3.severity == "hard"
    assert "schema mismatch" in g3.detail
