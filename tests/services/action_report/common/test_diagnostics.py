"""ROB-318 Phase 3 — unit tests for deterministic report diagnostics."""

from __future__ import annotations

import pytest

from app.services.action_report.common.diagnostics import (
    build_kind_diagnostic,
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
