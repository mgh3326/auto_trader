"""Smoke tests for the CIO quality gate service against the ROB-158 Scout Report.

Validates that :func:`evaluate_scout_report` correctly detects G1 (depth),
G4 (execution path), and G6 (budget reality) hits on the past v1-format
Scout Report that motivated ROB-170 / ROB-197.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.cio_quality_gate_service import (
    CHECKLIST_LABELS,
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


# ---------------------------------------------------------------------------
# Board revision blockers (approval d029ee95):
#   1. v2 Scout Report `|   | • ... |` subline은 부모 row에 병합되어야 한다 —
#      병합 누락 시 정상 v2 입력이 G1 hard gate에서 false fail(REOPEN) 된다.
#   2. `NAVER` 종목명 자체가 news 근거로 오인되어, 실제 뉴스 증거 없이도
#      shallow row가 false pass 되어서는 안 된다.
# ---------------------------------------------------------------------------


V2_SUBLINE_MD = """### 1) 보유 + 주요 신규 후보 동일 기준 비교

| 종목 (코드) | 시장가 | 액션 | 계좌 |
|---|---|---|---|
| **[신규]** Krafton 259960 | 266,500 | **watch only** | KIS 즉시 |
|   | • RSI 64, ADX 18, BB upper 돌파, EMA 5>20 bull |
|   | • 지지 244K (bb_mid) / 263K (fib_0+현재 부근) |
|   | • 뉴스: Bloomberg 2026-04-15 — 신작 런칭 catalyst, earning beat |
|   | • 목표가 357K(컨센서스), PER 15 — fundamental 양호 |
|   | • 기존 NAVER DCA 대비 우위, 섹터 노출 중복 없음 |

### 제한사항
없음
"""


@pytest.mark.unit
def test_v2_subline_bullets_merge_into_parent_row_and_pass_g1():
    """v2 bullet sublines `|   | • ... |` must fold into the parent row.

    Without the merge, the Krafton row sees only source/quote/exec — it would
    false-fail G1 (hard gate → REOPEN) despite the full deep-dive being present
    in the subline bullets.
    """
    report = evaluate_scout_report(markdown=V2_SUBLINE_MD)
    krafton = next(c for c in report.candidates if c.code == "259960")
    # All 8 checklist items must be satisfied once sublines merge.
    for k in range(1, 9):
        assert krafton.items[k], (
            f"#{k} ({list(CHECKLIST_LABELS.values())[k - 1]}) must be True "
            f"when v2 sublines are merged"
        )
    assert krafton.verdict == "pass"
    g1 = next(r for r in report.gates if r.key == "G1")
    assert g1.passed, f"G1 must pass on full v2 input; detail={g1.detail}"


@pytest.mark.unit
def test_v2_subline_merge_drops_without_bullet_first_cell():
    """Sanity check: the merge predicate only folds rows whose first cell is
    empty and which contain a bullet. A follow-up data row (content in first
    cell) must not bleed into the previous candidate.
    """
    from app.services.cio_quality_gate_service import extract_candidates

    md = "| A 111111 | 1,000 | KIS |\n| B 222222 | 2,000 | KIS |\n"
    cands = extract_candidates(md)
    codes = {c.code for c in cands}
    assert codes == {"111111", "222222"}
    a = next(c for c in cands if c.code == "111111")
    assert "B 222222" not in a.context_text


NAVER_NAME_ONLY_MD = """### 1) 보유 비교

| 종목 (코드) | 시장가 | RSI/ADX | BB | P/L | 액션 | 계좌 |
|---|---|---|---|---|---|---|
| NAVER 035420 | 216,000 | RSI 53, ADX 26 | bb_mid 206K, bb_lower 193K | –4.6% | **DCA 대비 우위** | KIS |
"""


@pytest.mark.unit
def test_naver_ticker_name_does_not_trigger_news_evidence():
    """`NAVER` as a stock ticker must not satisfy #5 News by itself.

    Row has real evidence for 1/2/3/4/7/8 (source=DCA, quote, RSI/ADX, bb_mid,
    exec=KIS, 대비/우위) but no news text. Before the fix `\\bNaver\\b` matched
    the ticker name and gave a 6/7 false pass on G1. After the fix, news must
    remain False and the row must fail G1.
    """
    report = evaluate_scout_report(markdown=NAVER_NAME_ONLY_MD)
    naver = next(c for c in report.candidates if c.code == "035420")
    assert naver.items[5] is False, (
        "#5 News must not trigger on the 'NAVER' ticker name alone"
    )
    assert naver.verdict == "fail"
    g1 = next(r for r in report.gates if r.key == "G1")
    assert not g1.passed
    assert "NAVER" in g1.detail or "035420" in g1.detail or "NAVER" in naver.name


@pytest.mark.unit
def test_naver_news_qualifier_still_counts_when_explicit():
    """When 'Naver news' / '네이버뉴스' appears as an explicit news-source
    qualifier, #5 News must still fire."""
    md_explicit_en = "| NAVER 035420 | 216,000 | Naver news — catalyst reform | KIS |\n"
    md_explicit_kr = (
        "| NAVER 035420 | 216,000 | 네이버뉴스 — 외인 순매수 earning beat | KIS |\n"
    )
    for md in (md_explicit_en, md_explicit_kr):
        report = evaluate_scout_report(markdown=md)
        c = next(c for c in report.candidates if c.code == "035420")
        assert c.items[5] is True, (
            f"#5 News must fire on explicit Naver-news qualifier; md={md!r}"
        )


# ---------------------------------------------------------------------------
# ROB-206 regressions:
#   1. Parser must locate the `실행경로` / `execution path` cell by header,
#      independent of column position. v2 §6.2 puts 액션 in the last column.
#   2. G4 must accept execution qualifiers placed in v2 sub-bullet lines
#      (e.g. "execution path: KIS 즉시"), not only in a dedicated column.
# ---------------------------------------------------------------------------


EXEC_MIDDLE_COLUMN_MD = """### 신규 후보 비교

| 종목 (코드) | 실행경로 | 시장가 | RSI | 액션 |
|---|---|---|---|---|
| **[신규]** Krafton 259960 | KIS 즉시 | 266,500 | 64 | watch only |
"""


@pytest.mark.unit
def test_exec_cell_resolved_by_header_when_column_is_not_last():
    """`_extract_execution_cell` must pull from the 실행경로 column by header,
    not from the last cell. With 실행경로 placed in column 2 (not last), the
    new candidate has a valid qualifier and G4 must pass.
    """
    report = evaluate_scout_report(markdown=EXEC_MIDDLE_COLUMN_MD)
    krafton = next(c for c in report.candidates if c.code == "259960")
    assert krafton.execution_cell == "KIS 즉시", (
        f"execution_cell must be pulled from the header-matched column; "
        f"got {krafton.execution_cell!r}"
    )
    g4 = next(r for r in report.gates if r.key == "G4")
    assert g4.passed, f"G4 must pass when exec qualifier sits mid-row; {g4.detail}"


V2_EXEC_SUBLINE_MD = """### 신규 후보 + 기존 DCA 동일 프레임 비교

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | 구조적 Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **Krafton 259960** | 신규(watch) | 266,500 | 64 | 18 | 244K (bb_mid) | watch only |
|   | • BB 223K/244K/265K · EMA 5>20<120 · 괴리 –8.3% · 기존 NAVER DCA 대비 열위 |
|   | • 뉴스 2건 (Reuters: PUBG / Bloomberg: earning guidance) · 컨센서스 목표가 290K, PER 18 · execution path: KIS 즉시 · same-depth-check: pass |

### 제한사항
없음
"""


@pytest.mark.unit
def test_v2_sec62_execution_path_in_subline_passes_g4():
    """v2 §6.2 core table has no 실행경로 column — the last cell is 액션.
    Execution path lives inside the sub-bullet line ("execution path: KIS 즉시").
    G4 must accept the sub-bullet qualifier via context_text fallback.
    """
    report = evaluate_scout_report(markdown=V2_EXEC_SUBLINE_MD)
    krafton = next(c for c in report.candidates if c.code == "259960")
    # Parent-row last cell is 액션 ("watch only") — no direct qualifier there.
    assert "즉시" not in krafton.execution_cell
    # But context_text carries the sub-bullet execution-path evidence.
    assert "즉시" in krafton.context_text
    g4 = next(r for r in report.gates if r.key == "G4")
    assert g4.passed, (
        f"G4 must pass when execution qualifier is carried in sub-bullet; {g4.detail}"
    )


@pytest.mark.unit
def test_g4_still_fails_when_bare_kis_appears_without_qualifier_anywhere():
    """Regression guard: the combined (execution_cell + context_text) check
    must NOT over-match. A row with bare `KIS` in both the cell and the
    sub-bullet still lacks an EXEC_QUALIFIER_RE match and G4 must fail.
    """
    md = """### 신규 후보

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **LG이노텍 011070** | 신규(buy 검토) | 212,500 | 58 | 24 | 202K | buy 검토 |
|   | • BB 194K/202K/222K · execution path: KIS · same-depth-check: pass |
"""
    report = evaluate_scout_report(markdown=md)
    lginnotek = next(c for c in report.candidates if c.code == "011070")
    assert lginnotek.is_new
    g4 = next(r for r in report.gates if r.key == "G4")
    assert not g4.passed, f"G4 must hit when bare 'KIS' has no qualifier; {g4.detail}"


@pytest.mark.unit
def test_is_new_detected_from_v2_category_column():
    """v2 §6.2 puts 신규/보유 in the 분류 column (col idx 2), not embedded in
    the 종목 name. is_new must fire on the 분류 cell alone.
    """
    md = """| 시장 | 종목 | 분류 | 시장가 | 액션 |
|---|---|---|---|---|
| KR | NAVER 035420 | 보유/DCA | 216,000 | DCA limit |
| KR | Krafton 259960 | 신규(watch) | 266,500 | watch only |
"""
    report = evaluate_scout_report(markdown=md)
    naver = next(c for c in report.candidates if c.code == "035420")
    krafton = next(c for c in report.candidates if c.code == "259960")
    assert not naver.is_new, "보유/DCA cell must not mark NAVER as 신규"
    assert krafton.is_new, "신규(watch) cell in col 2 must mark Krafton as 신규"


# ---------------------------------------------------------------------------
# ROB-206 review follow-up — over-match guards:
#   1. G4 must not treat incidental 해외/자동/수동 tokens in unrelated bullet
#      segments (뉴스 headlines, S/R notes) as execution-path qualifiers.
#   2. is_new must not treat `신규` appearing in 액션/메모/뉴스 cells as a
#      positive signal — only the 분류 column (or legacy [신규] name tag)
#      counts.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_g4_ignores_exec_qualifier_tokens_in_unrelated_bullet_segments():
    """`해외 매출`, `전기 자동차`, `수동 검증` style tokens in 뉴스/S-R/memo
    sub-bullets must NOT satisfy EXEC_QUALIFIER_RE. G4 is only satisfied by
    an actual `execution path:` / `실행경로:` segment OR a direct 실행경로
    cell match.
    """
    md = """### 신규 후보

| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | **LG이노텍 011070** | 신규(buy 검토) | 212,500 | 58 | 24 | 202K | buy 검토 |
|   | • BB 194K/202K/222K · EMA 5>20>60<120 · 괴리 –4.5% |
|   | • 뉴스 3건 (Reuters: 해외 매출 전망 / Bloomberg: 전기 자동차 수요 / 한경: 수동 검증 통과) · 컨센서스 목표가 250K, PER 16 · execution path: KIS · same-depth-check: pass |
"""
    report = evaluate_scout_report(markdown=md)
    lginnotek = next(c for c in report.candidates if c.code == "011070")
    assert lginnotek.is_new
    # The sub-bullet contains 해외/자동/수동 tokens in 뉴스 segment, but the
    # actual `execution path:` segment is `KIS` (bare). G4 must still hit.
    g4 = next(r for r in report.gates if r.key == "G4")
    assert not g4.passed, (
        f"G4 must ignore 해외/자동/수동 appearing outside execution-path segment; "
        f"detail={g4.detail}"
    )


@pytest.mark.unit
def test_g4_segment_scan_still_accepts_multiple_labelled_segments():
    """If the row has multiple `execution path:` segments (e.g. per-market
    split), G4 must pass when any one of them carries a qualifier.
    """
    md = """| 시장 | 종목 | 분류 | 시장가 | RSI | ADX | Buy Zone | 액션 |
|---|---|---|---|---|---|---|---|
| KR | Krafton 259960 | 신규(watch) | 266,500 | 64 | 18 | 244K | watch only |
|   | • 뉴스 — none · execution path: KIS · 실행경로: Toss manual · same-depth-check: pass |
"""
    report = evaluate_scout_report(markdown=md)
    g4 = next(r for r in report.gates if r.key == "G4")
    assert g4.passed, (
        f"G4 must accept `Toss manual` qualifier in the 실행경로 segment even "
        f"when a sibling `execution path: KIS` segment is bare; detail={g4.detail}"
    )


@pytest.mark.unit
def test_is_new_does_not_fire_on_action_or_news_mention_of_신규():
    """A 보유 row whose 액션 or 뉴스 cell happens to reference `신규 추천`,
    `신규 카테고리`, etc. must NOT be promoted to 신규. Only the 분류 column
    (or v1 name-cell `[신규]` tag) counts.
    """
    md = """| 시장 | 종목 | 분류 | 시장가 | 뉴스 | 액션 | 비고 |
|---|---|---|---|---|---|---|
| KR | NAVER 035420 | 보유/DCA | 216,000 | 뉴스 1건 — 신규 CEO 임명 | DCA limit | 신규 후보 대비 우위 |
"""
    report = evaluate_scout_report(markdown=md)
    naver = next(c for c in report.candidates if c.code == "035420")
    assert not naver.is_new, (
        "보유/DCA row must stay 보유 even when 뉴스/액션/비고 cells mention 신규"
    )


@pytest.mark.unit
def test_is_new_does_not_fire_on_category_column_saying_보유_in_v1_name_tag():
    """Symmetric guard: when 분류 column is present AND says 보유, an
    incidental `신규` elsewhere on the row (e.g. 비고: `신규 섹터 노출 없음`)
    must still leave is_new=False because the 분류 column is authoritative.
    """
    md = """| 시장 | 종목 | 분류 | 시장가 | 비고 |
|---|---|---|---|---|
| KR | NAVER 035420 | 보유 | 216,000 | 신규 섹터 노출 없음 |
"""
    report = evaluate_scout_report(markdown=md)
    naver = next(c for c in report.candidates if c.code == "035420")
    assert not naver.is_new
