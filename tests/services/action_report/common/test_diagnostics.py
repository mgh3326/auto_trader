"""ROB-318 Phase 3 — unit tests for deterministic report diagnostics."""

from __future__ import annotations

import pytest

from app.services.action_report.common.diagnostics import (
    build_data_sufficiency_by_source,
    build_kind_diagnostic,
    build_report_diagnostics,
    build_report_quality_summary,
    classify_why_no_action,
    reason_code_for,
    sanitize_reason,
)


# --- sanitize_reason --------------------------------------------------------
def test_sanitize_reason_none_and_empty() -> None:
    assert sanitize_reason(None) is None
    assert sanitize_reason("") is None
    assert sanitize_reason("   ") is None


def test_sanitize_reason_redacts_secret_shaped_tokens() -> None:
    out = sanitize_reason("auth failed token=ABC123secretvalue for account")
    assert "ABC123secretvalue" not in out
    assert "[REDACTED]" in out


def test_sanitize_reason_redacts_long_opaque_blob() -> None:
    blob = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"  # 32 chars
    out = sanitize_reason(f"kis error context {blob} end")
    assert blob not in out
    assert "[REDACTED]" in out


def test_sanitize_reason_caps_length() -> None:
    out = sanitize_reason("x " * 400)
    assert out is not None
    assert len(out) <= 200


# --- reason_code_for --------------------------------------------------------
def test_reason_code_prefers_valid_collector_code() -> None:
    assert (
        reason_code_for("unavailable", {"reason_code": "user_id_missing"})
        == "user_id_missing"
    )


def test_reason_code_ignores_unknown_collector_code() -> None:
    # An unrecognized code must not pass through as a free-form code.
    assert reason_code_for("unavailable", {"reason_code": "weird"}) == "unavailable"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("hard_stale", "stale"),
        ("soft_stale", "stale"),
        ("unavailable", "unavailable"),
        ("failed", "failed"),
        ("fresh", "unknown"),
        (None, "unknown"),
    ],
)
def test_reason_code_derived_from_status(status: str | None, expected: str) -> None:
    assert reason_code_for(status, None) == expected


# --- build_kind_diagnostic --------------------------------------------------
def test_build_kind_diagnostic_includes_sanitized_reason() -> None:
    diag = build_kind_diagnostic(
        "unavailable",
        {
            "reason_code": "user_id_missing",
            "reason": "kis_live portfolio requires explicit user_id; none supplied",
        },
    )
    assert diag["reason_code"] == "user_id_missing"
    assert "user_id" in diag["reason"]


def test_build_kind_diagnostic_omits_reason_when_absent() -> None:
    diag = build_kind_diagnostic("unavailable", None)
    assert diag == {"reason_code": "unavailable"}
    assert "reason" not in diag


# --- classify_why_no_action -------------------------------------------------
def test_why_no_action_data_insufficient_on_missing_critical() -> None:
    out = classify_why_no_action(
        freshness_summary={"portfolio": {"status": "unavailable"}},
        bundle_status="partial",
        has_action_items=False,
    )
    assert out is not None
    assert out["kind"] == "data_insufficient"
    assert out["blocking_sources"] == ["portfolio"]
    assert "확인 불가" in out["reason_ko"]


def test_why_no_action_stale_gated_on_hard_stale_critical() -> None:
    out = classify_why_no_action(
        freshness_summary={"market": {"status": "hard_stale"}},
        bundle_status="complete",
        has_action_items=False,
    )
    assert out is not None
    assert out["kind"] == "stale_gated"
    assert out["blocking_sources"] == ["market"]


def test_why_no_action_missing_precedes_stale() -> None:
    out = classify_why_no_action(
        freshness_summary={
            "portfolio": {"status": "unavailable"},
            "market": {"status": "hard_stale"},
        },
        bundle_status="partial",
        has_action_items=False,
    )
    assert out["kind"] == "data_insufficient"
    assert out["blocking_sources"] == ["portfolio"]


def test_why_no_action_bundle_failed_is_data_insufficient() -> None:
    out = classify_why_no_action(
        freshness_summary={},
        bundle_status="failed",
        has_action_items=False,
    )
    assert out["kind"] == "data_insufficient"
    assert out["blocking_sources"] == ["bundle"]


def test_why_no_action_stale_fallback_bundle() -> None:
    out = classify_why_no_action(
        freshness_summary={},
        bundle_status="stale_fallback",
        has_action_items=False,
    )
    assert out["kind"] == "stale_gated"
    assert out["blocking_sources"] == ["bundle"]


def test_why_no_action_real_no_action_when_fresh_but_no_items() -> None:
    out = classify_why_no_action(
        freshness_summary={"portfolio": {"status": "fresh"}},
        bundle_status="complete",
        has_action_items=False,
    )
    assert out is not None
    assert out["kind"] == "real_no_action"
    assert out["blocking_sources"] == []


def test_why_no_action_none_when_action_present_and_fresh() -> None:
    out = classify_why_no_action(
        freshness_summary={"portfolio": {"status": "fresh"}},
        bundle_status="complete",
        has_action_items=True,
    )
    assert out is None


# --- build_data_sufficiency_by_source ---------------------------------------
def test_data_sufficiency_carries_status_and_reason_code() -> None:
    out = build_data_sufficiency_by_source(
        {
            "overall": "unavailable",
            "portfolio": {
                "status": "unavailable",
                "reason_code": "user_id_missing",
                "reason": "...",
            },
            "market": {"status": "fresh", "as_of": "2026-05-26T00:00:00"},
        }
    )
    assert "overall" not in out
    assert out["portfolio"]["status"] == "unavailable"
    assert out["portfolio"]["reason_code"] == "user_id_missing"
    assert out["market"]["status"] == "fresh"
    assert out["market"]["as_of"] == "2026-05-26T00:00:00"


# --- build_report_quality_summary -------------------------------------------
def test_quality_grade_high_confidence_all_fresh() -> None:
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
        bundle_status="complete",
    )
    assert out["grade"] == "high_confidence"
    assert out["fresh_coverage_pct"] == 100


def test_quality_grade_informational_when_critical_degraded() -> None:
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "unavailable",
            "portfolio": {"status": "unavailable"},
            "market": {"status": "fresh"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "informational_only"
    assert out["kind_status_counts"]["unavailable"] == 1


def test_quality_grade_no_action_when_bundle_failed() -> None:
    out = build_report_quality_summary(
        freshness_summary={"overall": "failed"},
        bundle_status="failed",
    )
    assert out["grade"] == "no_action"


# --- build_report_diagnostics -----------------------------------------------
def test_build_report_diagnostics_bundles_three_rollups() -> None:
    why = {"kind": "data_insufficient", "blocking_sources": ["portfolio"]}
    out = build_report_diagnostics(
        freshness_summary={
            "overall": "unavailable",
            "portfolio": {"status": "unavailable", "reason_code": "user_id_missing"},
        },
        bundle_status="partial",
        why_no_action=why,
    )
    assert out["why_no_action"] == why
    assert out["data_sufficiency_by_source"]["portfolio"]["reason_code"] == (
        "user_id_missing"
    )
    assert out["report_quality_summary"]["grade"] == "informational_only"


def test_build_external_cross_checks_marks_affects_generation_false() -> None:
    from app.services.action_report.common.diagnostics import (
        build_external_cross_checks,
    )

    out = build_external_cross_checks(
        {
            "portfolio": {"status": "fresh"},  # core — ignored here
            "toss_remote_debug": {
                "status": "unavailable",
                "reason_code": "unavailable",
                "as_of": "2026-05-26T00:00:00Z",
            },
            "naver_remote_debug": {"status": "partial"},
        }
    )
    assert set(out) == {"toss_remote_debug", "naver_remote_debug"}
    assert out["toss_remote_debug"]["affects_report_generation"] is False
    assert out["toss_remote_debug"]["status"] == "unavailable"
    assert out["toss_remote_debug"]["reason_code"] == "unavailable"
    assert out["toss_remote_debug"]["as_of"] == "2026-05-26T00:00:00Z"
    assert out["naver_remote_debug"]["affects_report_generation"] is False


def test_build_external_cross_checks_empty_when_no_external_present() -> None:
    from app.services.action_report.common.diagnostics import (
        build_external_cross_checks,
    )

    assert build_external_cross_checks({"portfolio": {"status": "fresh"}}) == {}


def test_quality_summary_splits_core_optional_external_coverage() -> None:
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},  # optional internal
            "toss_remote_debug": {"status": "unavailable"},  # external
            "naver_remote_debug": {"status": "unavailable"},  # external
        },
        bundle_status="partial",
    )
    # All 4 core kinds fresh.
    assert out["core_fresh_coverage_pct"] == 100
    # 1 optional internal kind (news), 0 fresh.
    assert out["optional_fresh_coverage_pct"] == 0
    # External rollup excluded from core/optional coverage; surfaced separately.
    assert out["external_cross_check_status"] == "unavailable"
    # Grade unchanged: core fresh + partial bundle → high_confidence.
    assert out["grade"] == "high_confidence"


def test_quality_summary_external_status_none_when_absent() -> None:
    out = build_report_quality_summary(
        freshness_summary={"portfolio": {"status": "fresh"}},
        bundle_status="complete",
    )
    assert out["external_cross_check_status"] is None
    assert out["core_fresh_coverage_pct"] == 100


# --- build_report_quality_summary: ROB-366 B10 honesty demotion --------------
def test_quality_grade_demotes_when_core_kind_soft_stale_on_partial() -> None:
    # ROB-366 B10: the real bundle had core_fresh_coverage_pct=75 — one critical
    # kind ('market') was non-fresh but only soft_stale, which is NOT in
    # CRITICAL_KIND_DEGRADING_STATUSES, so the old grade fell through to
    # high_confidence. A non-fully-fresh core on a non-complete bundle is now an
    # honest informational_only.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "soft_stale"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "informational_only"
    assert out["core_fresh_coverage_pct"] == 75


def test_quality_grade_demotes_on_thin_optional_coverage_without_cross_check() -> None:
    # ROB-366 B10: core fully fresh but news/candidate/symbol all empty and no
    # external cross-check to compensate → internal coverage (4/7 ≈ 57%) is too
    # thin to honestly read as high_confidence.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
            "candidate_universe": {"status": "unavailable"},
            "symbol": {"status": "unavailable"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "informational_only"
    assert out["core_fresh_coverage_pct"] == 100


def test_quality_grade_thin_coverage_stays_high_with_passing_cross_check() -> None:
    # A passing external cross-check is the one signal that legitimately rescues a
    # thin-but-core-fresh bundle: it stays high_confidence. Guards against the
    # demotion over-firing when there IS corroborating evidence.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
            "candidate_universe": {"status": "unavailable"},
            "symbol": {"status": "unavailable"},
            "toss_remote_debug": {"status": "fresh"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
    assert out["external_cross_check_status"] == "fresh"


def test_quality_grade_demotes_when_external_cross_check_hard_stale() -> None:
    # A hard_stale external probe is a degrading status
    # (CRITICAL_KIND_DEGRADING_STATUSES) — stale-expired evidence must NOT count
    # as a usable cross-check that rescues thin coverage back to high_confidence.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
            "candidate_universe": {"status": "unavailable"},
            "symbol": {"status": "unavailable"},
            "toss_remote_debug": {"status": "hard_stale"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "informational_only"
    assert out["external_cross_check_status"] == "hard_stale"


def test_quality_grade_thin_coverage_stays_high_with_soft_stale_cross_check() -> None:
    # soft_stale is a non-degrading status, so a soft_stale external cross-check
    # still corroborates and lets a thin-but-core-fresh bundle stay high.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
            "candidate_universe": {"status": "unavailable"},
            "symbol": {"status": "unavailable"},
            "toss_remote_debug": {"status": "soft_stale"},
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
    assert out["external_cross_check_status"] == "soft_stale"


def test_quality_grade_complete_bundle_stays_high_without_external() -> None:
    # An external probe that was simply never run must never tank an otherwise
    # complete bundle — ROB-323 fail-open preserved in the grade. internal
    # coverage is 100% so no demotion fires regardless of the absent probe.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
        },
        bundle_status="complete",
    )
    assert out["grade"] == "high_confidence"
    assert out["external_cross_check_status"] is None


def test_quality_grade_no_action_on_stale_fallback_precedence_unchanged() -> None:
    # The honesty demotion only ever moves high_confidence → informational_only;
    # it must never interfere with the top-precedence no_action branch.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "hard_stale",
            "market": {"status": "hard_stale"},
        },
        bundle_status="stale_fallback",
    )
    assert out["grade"] == "no_action"


def test_build_data_quality_audit_shape() -> None:
    from app.services.action_report.common.diagnostics import build_data_quality_audit

    audit = build_data_quality_audit(
        freshness_summary={
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "toss_remote_debug": {
                "status": "unavailable",
                "reason_code": "unavailable",
            },
        },
        bundle_status="partial",
        snapshot_bundle_uuid="b-123",
    )
    assert audit["snapshot_bundle_uuid"] == "b-123"
    assert audit["core"]["status"] == "usable"
    assert audit["core"]["blocking_gaps"] == []
    assert audit["core"]["fresh_coverage_pct"] == 100
    assert (
        audit["external_cross_checks"]["toss_remote_debug"]["affects_report_generation"]
        is False
    )
    # An unavailable external probe is reported as an info-severity gap, never
    # a blocker.
    assert any(g["severity"] == "info" for g in audit["gaps"])
    assert all(g["severity"] != "blocking" for g in audit["gaps"])


def test_build_data_quality_audit_core_degraded_lists_blocking_gap() -> None:
    from app.services.action_report.common.diagnostics import build_data_quality_audit

    audit = build_data_quality_audit(
        freshness_summary={"portfolio": {"status": "unavailable"}},
        bundle_status="failed",
        snapshot_bundle_uuid=None,
    )
    assert audit["core"]["status"] == "degraded"
    assert "portfolio" in audit["core"]["blocking_gaps"]


def test_report_diagnostics_includes_data_quality_audit() -> None:
    from app.services.action_report.common.diagnostics import build_report_diagnostics

    out = build_report_diagnostics(
        freshness_summary={
            "portfolio": {"status": "fresh"},
            "toss_remote_debug": {"status": "unavailable"},
        },
        bundle_status="partial",
        why_no_action=None,
        snapshot_bundle_uuid="b-1",
    )
    assert "data_quality_audit" in out
    assert out["data_quality_audit"]["snapshot_bundle_uuid"] == "b-1"


def test_quality_grade_demotes_when_candidate_universe_stale_no_cross_check() -> None:
    # ROB-415: candidate_universe (the buy-candidate source) is stale while other
    # optional kinds are fresh, so aggregate internal coverage stays >=70% and the
    # old thin_coverage rule never fired. With no usable external cross-check, a
    # stale candidate_universe must demote high_confidence → informational_only.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "symbol": {"status": "fresh"},
            "invest_page": {"status": "fresh"},
            "candidate_universe": {"status": "soft_stale"},
            "toss_remote_debug": {"status": "unavailable"},  # external, no rescue
        },
        bundle_status="partial",
    )
    # Core fully fresh and internal coverage is high (7/8 ≈ 88%), so the old rule
    # left it high_confidence — the bug.
    assert out["core_fresh_coverage_pct"] == 100
    assert out["grade"] == "informational_only"
    assert out["external_cross_check_status"] == "unavailable"


def test_quality_grade_candidate_universe_stale_rescued_by_cross_check() -> None:
    # ROB-415 / ROB-323: a usable external cross-check still corroborates a stale
    # candidate_universe, so the bundle stays high_confidence. Guards the demotion
    # from over-firing when there IS fresh external evidence.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "symbol": {"status": "fresh"},
            "invest_page": {"status": "fresh"},
            "candidate_universe": {"status": "soft_stale"},
            "toss_remote_debug": {"status": "fresh"},  # usable cross-check
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
    assert out["external_cross_check_status"] == "fresh"


def test_quality_grade_candidate_universe_fresh_stays_high() -> None:
    # candidate_universe present and fresh must NOT trigger the ROB-415 demotion.
    out = build_report_quality_summary(
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "fresh"},
            "candidate_universe": {"status": "fresh"},
            "toss_remote_debug": {"status": "unavailable"},  # external, no rescue
        },
        bundle_status="partial",
    )
    assert out["grade"] == "high_confidence"
