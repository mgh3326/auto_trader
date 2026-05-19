"""ROB-269 Phase 3 — Layer (ii) ↔ (iii) critical-kind allowlist alignment.

Both ``generator_constraints.derive_generator_constraints`` (layer ii) and
``stale_gate.lint_action_language`` (layer iii) consume the same
``CRITICAL_SNAPSHOT_KINDS`` constant. This test pins the two layers to the
SAME decision for every status combination across the critical/optional
split — the contract review-fix from PR #876 item 2.
"""

from __future__ import annotations

from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
)
from app.services.action_report.common.generator_constraints import (
    derive_generator_constraints,
)
from app.services.action_report.common.stale_gate import lint_action_language


def test_critical_snapshot_kinds_is_the_phase2_required_set():
    """The 4 critical kinds match Phase 2's policy ``required_kinds()`` for
    ``intraday_action_report_v1`` — see app/services/investment_snapshots/policy.py."""
    from app.services.investment_snapshots.policy import INTRADAY_ACTION_REPORT_V1

    assert set(CRITICAL_SNAPSHOT_KINDS) == set(
        INTRADAY_ACTION_REPORT_V1.required_kinds()
    )


def test_critical_kind_degrading_statuses_locked():
    """The degrading-status set is locked — adding/removing one without
    revisiting both layers would silently change blocking behaviour."""
    assert CRITICAL_KIND_DEGRADING_STATUSES == frozenset(
        {"hard_stale", "unavailable", "failed"}
    )


def test_layer_ii_and_iii_agree_on_optional_unavailable_does_not_block():
    """Optional kinds unavailable + critical kinds fresh:
    layer (ii) returns allow=True, layer (iii) returns ok=True."""
    freshness = {
        "overall": "partial",
        # All critical kinds fresh.
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "fresh"},
        # All optional kinds degraded.
        "news": {"status": "unavailable"},
        "naver_remote_debug": {"status": "unavailable"},
        "toss_remote_debug": {"status": "hard_stale"},
        "browser_probe": {"status": "failed"},
        "invest_page": {"status": "unavailable"},
        "candidate_universe": {"status": "unavailable"},
        "symbol": {"status": "hard_stale"},
    }

    constraints = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary=freshness,
        account_scope="kis_live",
    )
    lint = lint_action_language(
        report_text="매수 추천",
        bundle_status="partial",
        freshness_summary=freshness,
        account_scope="kis_live",
    )

    assert constraints.allow_action_language is True
    assert constraints.forced_action_mode == "default"
    assert lint.ok is True
    assert lint.violations == []


def test_layer_ii_and_iii_agree_on_each_critical_kind_degradation():
    """For every critical kind × every degrading status, BOTH layers must
    return a blocking decision."""
    for kind in CRITICAL_SNAPSHOT_KINDS:
        for status in CRITICAL_KIND_DEGRADING_STATUSES:
            freshness = {
                "overall": "partial",
                "portfolio": {"status": "fresh"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
            }
            freshness[kind] = {"status": status}

            constraints = derive_generator_constraints(
                bundle_status="partial",
                freshness_summary=freshness,
                account_scope="kis_live",
            )
            lint = lint_action_language(
                report_text="매수 권고",
                bundle_status="partial",
                freshness_summary=freshness,
                account_scope="kis_live",
            )

            assert constraints.allow_action_language is False, (
                f"layer (ii) did not block on critical kind={kind}, status={status}"
            )
            assert lint.ok is False, (
                f"layer (iii) did not block on critical kind={kind}, status={status}"
            )


def test_layer_ii_and_iii_agree_on_bundle_status_failed_blocks():
    """bundle_status='failed' blocks both layers regardless of per-kind statuses."""
    freshness = {
        "overall": "failed",
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "fresh"},
    }
    constraints = derive_generator_constraints(
        bundle_status="failed",
        freshness_summary=freshness,
        account_scope="kis_live",
    )
    lint = lint_action_language(
        report_text="매수",
        bundle_status="failed",
        freshness_summary=freshness,
        account_scope="kis_live",
    )
    assert constraints.allow_action_language is False
    assert lint.ok is False


def test_layer_ii_and_iii_agree_on_bundle_status_stale_fallback_blocks():
    """bundle_status='stale_fallback' blocks both layers."""
    freshness = {
        "overall": "hard_stale",
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "fresh"},
    }
    constraints = derive_generator_constraints(
        bundle_status="stale_fallback",
        freshness_summary=freshness,
        account_scope="kis_live",
    )
    lint = lint_action_language(
        report_text="매수",
        bundle_status="stale_fallback",
        freshness_summary=freshness,
        account_scope="kis_live",
    )
    assert constraints.allow_action_language is False
    assert lint.ok is False


def test_layer_ii_and_iii_agree_on_account_scope_none_bypass():
    """account_scope=None is informational — both layers bypass."""
    freshness = {
        "overall": "failed",
        "portfolio": {"status": "failed"},
    }
    constraints = derive_generator_constraints(
        bundle_status="failed",
        freshness_summary=freshness,
        account_scope=None,
    )
    lint = lint_action_language(
        report_text="매수 추천",
        bundle_status="failed",
        freshness_summary=freshness,
        account_scope=None,
    )
    assert constraints.allow_action_language is True
    assert lint.ok is True


def test_layer_ii_and_iii_agree_on_legacy_bundle_status_none_bypass():
    """bundle_status=None is legacy (pre-Phase-3 reports) — both layers bypass."""
    constraints = derive_generator_constraints(
        bundle_status=None,
        freshness_summary=None,
        account_scope="kis_live",
    )
    lint = lint_action_language(
        report_text="매수",
        bundle_status=None,
        freshness_summary=None,
        account_scope="kis_live",
    )
    assert constraints.allow_action_language is True
    assert lint.ok is True
