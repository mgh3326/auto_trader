"""CIO quality gate e2e 테스트 — Scout Report shallow 감지 + reopen trigger.

ROB-196 / ROB-170 §7 · 4개 hard-gate (G1/G3/G4/G6) 각각이 의도적으로 shallow 한
Scout Report 를 수신했을 때 `evaluate_scout_report()` 가 해당 gate 를 탐지하고
§7.2 reopen 코멘트를 생성하는지 검증한다.

의존:
- `app.services.cio_quality_gate_service` (ROB-197 deliverable)
- `tests/fixtures/scout_reports/` markdown fixtures

테스트 대상 모듈이 import 안 되는 환경에서는 `importorskip` 으로 전체 skip
(ROB-197 이 merge 되기 전 ROB-196 branch 단독 CI 에서 green 유지).

모든 테스트는 `cash_balance=1_670_000` 을 주입해 대상 gate 외 G6 이 노이즈로
같이 터지는 것을 피한다. 개별 fixture 는 targeted gate 외 hard-gate 가 clean
하도록 설계돼 있으며, assertion 은 `_hard_violation_ids(result)` 로 정확한
hit set 을 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

cio_gate = pytest.importorskip(
    "app.services.cio_quality_gate_service",
    reason="ROB-197 CIO quality gate service not merged yet",
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "scout_reports"
CIO_CASH = 1_670_000  # ROB-158 실제 예수금 (raw KRW)


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _hard_violation_ids(result) -> set[str]:
    return {v.gate_id for v in result.violations if v.severity == "hard"}


def test_g1_depth_fail_triggers_reopen() -> None:
    """신규 후보 1건에 news/fundamental/S/R 누락 → G1 hit → reopen 생성."""
    report = _load("g1_depth_fail.md")

    result = cio_gate.evaluate_scout_report(markdown=report, cash_balance=CIO_CASH)

    assert result.overall_status == "FAIL"
    assert _hard_violation_ids(result) == {"G1"}
    assert result.reopen_comment is not None
    assert "G1 Depth" in result.reopen_comment
    g1 = next(v for v in result.violations if v.gate_id == "G1")
    assert "삼성바이오로직스" in g1.detail or "207940" in g1.detail


def test_g3_tool_failure_without_limitation_section_triggers_reopen() -> None:
    """tool_failures 주입되고 `### 제한사항` 섹션 없음 → G3 hit."""
    report = _load("g3_tool_failure.md")

    result = cio_gate.evaluate_scout_report(
        markdown=report,
        cash_balance=CIO_CASH,
        tool_failures=["screen_stocks: schema mismatch, retry 1회 실패"],
    )

    assert result.overall_status == "FAIL"
    assert _hard_violation_ids(result) == {"G3"}
    assert result.reopen_comment is not None
    assert "G3" in result.reopen_comment


def test_g4_execution_path_missing_triggers_reopen() -> None:
    """신규 후보 실행경로 셀이 bare 'KIS' (qualifier 없음) → G4 hit."""
    report = _load("g4_execution_path.md")

    result = cio_gate.evaluate_scout_report(markdown=report, cash_balance=CIO_CASH)

    assert result.overall_status == "FAIL"
    assert _hard_violation_ids(result) == {"G4"}
    assert result.reopen_comment is not None
    assert "G4 Execution path" in result.reopen_comment
    g4 = next(v for v in result.violations if v.gate_id == "G4")
    assert "LG이노텍" in g4.detail or "011070" in g4.detail


def test_g6_budget_over_cash_without_disclosure_triggers_reopen() -> None:
    """주문안 총액 5.0M vs 예수금 1.67M (배수 2.99x) + disclosure 없음 → G6 hit.

    `cash_balance` 주입은 get_cash_balance 호출 증거로 인정되지만,
    over-budget + no-disclosure 조건에서는 G6 여전히 fail 이어야 함.
    """
    report = _load("g6_budget_reality.md")

    result = cio_gate.evaluate_scout_report(markdown=report, cash_balance=CIO_CASH)

    assert result.overall_status == "FAIL"
    assert _hard_violation_ids(result) == {"G6"}
    assert result.reopen_comment is not None
    assert "G6 Budget reality" in result.reopen_comment


def test_all_gates_pass_does_not_reopen() -> None:
    """모든 gate pass → overall PASS, reopen 없음 (regression guard)."""
    report = _load("all_gates_pass.md")

    result = cio_gate.evaluate_scout_report(
        markdown=report,
        cash_balance=CIO_CASH,
        tool_failures=[],
    )

    assert result.overall_status == "PASS"
    assert _hard_violation_ids(result) == set()
    assert result.reopen_comment is None


@pytest.mark.parametrize(
    "fixture_name,expected_hard_gate,label",
    [
        ("g1_depth_fail.md", "G1", "G1 Depth"),
        ("g3_tool_failure.md", "G3", "G3"),
        ("g4_execution_path.md", "G4", "G4 Execution path"),
        ("g6_budget_reality.md", "G6", "G6 Budget reality"),
    ],
)
def test_reopen_comment_cites_violated_gate(
    fixture_name: str, expected_hard_gate: str, label: str
) -> None:
    """§7.2 reopen 템플릿의 `위반 gate` 라인이 실제 위반된 gate label 을 포함해야 한다."""
    kwargs: dict = {"markdown": _load(fixture_name), "cash_balance": CIO_CASH}
    if fixture_name == "g3_tool_failure.md":
        kwargs["tool_failures"] = ["screen_stocks: schema mismatch"]

    result = cio_gate.evaluate_scout_report(**kwargs)

    assert result.reopen_comment is not None
    assert expected_hard_gate in result.reopen_comment
    assert label in result.reopen_comment
    assert "Scout reopen" in result.reopen_comment
