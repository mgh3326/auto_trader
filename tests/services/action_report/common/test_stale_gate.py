"""ROB-269 Phase 3 — stale_gate post-generation linter (Decision 4 layer (iii))."""

from __future__ import annotations

import pytest

from app.services.action_report.common.stale_gate import (
    StaleLintResult,
    StaleLintViolation,
    lint_action_language,
)


def test_lint_passes_on_fresh_complete():
    result = lint_action_language(
        report_text="삼성전자 매수 검토",
        bundle_status="complete",
        freshness_summary={"overall": "fresh", "portfolio": {"status": "fresh"}},
        account_scope="kis_live",
    )
    assert result.ok is True
    assert result.violations == []


def test_lint_blocks_buy_verb_on_hard_stale():
    result = lint_action_language(
        report_text="삼성전자 매수",
        bundle_status="stale_fallback",
        freshness_summary={"overall": "hard_stale", "portfolio": {"status": "hard_stale"}},
        account_scope="kis_live",
    )
    assert result.ok is False
    assert any(v.matched_verb == "매수" for v in result.violations)


def test_lint_blocks_sell_verb_on_partial_when_portfolio_missing():
    result = lint_action_language(
        report_text="분할매도 권고",
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "unavailable"},
        },
        account_scope="kis_live",
    )
    assert result.ok is False
    matched = {v.matched_verb for v in result.violations}
    # 분할매도 is itself a forbidden verb; 매도 may also match as a substring.
    assert "분할매도" in matched or "매도" in matched


def test_lint_allows_soft_language_on_stale():
    result = lint_action_language(
        report_text="관망 권고. 확인 불가 상태입니다.",
        bundle_status="stale_fallback",
        freshness_summary={"overall": "hard_stale", "portfolio": {"status": "hard_stale"}},
        account_scope="kis_live",
    )
    assert result.ok is True
    assert result.violations == []


def test_lint_blocks_english_verb_on_failed():
    result = lint_action_language(
        report_text="consider adding to the position",
        bundle_status="failed",
        freshness_summary={"overall": "failed"},
        account_scope="kis_live",
    )
    assert result.ok is False
    # ``adding`` is matched as the lemma ``add`` (stem with ing suffix).
    matched_verbs = {v.matched_verb.lower() for v in result.violations}
    assert any("add" in v for v in matched_verbs)


def test_lint_passes_legacy_report_no_bundle():
    """Legacy reports (bundle_status=None) bypass the lint — Phase 3 doesn't
    retroactively enforce action language on pre-Phase-3 reports."""
    result = lint_action_language(
        report_text="삼성전자 매수",
        bundle_status=None,
        freshness_summary=None,
        account_scope="kis_live",
    )
    assert result.ok is True
    assert result.violations == []


def test_lint_blocks_when_account_scope_kis_live_and_portfolio_hard_stale():
    """Even with bundle_status='partial', a hard_stale portfolio + live
    account scope must block — caller's executable action language cannot
    be trusted when the live portfolio snapshot is too old."""
    result = lint_action_language(
        report_text="매수 추천",
        bundle_status="partial",
        freshness_summary={"overall": "partial", "portfolio": {"status": "hard_stale"}},
        account_scope="kis_live",
    )
    assert result.ok is False
    assert any(v.matched_verb == "매수" for v in result.violations)


def test_lint_allows_when_no_account_scope():
    """When account_scope is None the report is informational — there is no
    broker context to mis-trigger, so action language is not blocked."""
    result = lint_action_language(
        report_text="매수 추천",
        bundle_status="partial",
        freshness_summary={"overall": "partial", "portfolio": {"status": "unavailable"}},
        account_scope=None,
    )
    assert result.ok is True
    assert result.violations == []


def test_lint_result_dataclass_shape():
    """StaleLintResult/Violation are simple frozen dataclasses used by callers."""
    v = StaleLintViolation(
        snapshot_kind="portfolio", matched_verb="매수", excerpt="...매수..."
    )
    r = StaleLintResult(ok=False, violations=[v])
    assert r.ok is False
    assert len(r.violations) == 1
    # frozen — assignment raises.
    with pytest.raises(Exception):
        r.ok = True  # type: ignore[misc]
