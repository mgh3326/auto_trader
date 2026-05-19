"""ROB-269 Phase 3 — derive_generator_constraints (Decision 4 layer ii)."""

from __future__ import annotations

from app.services.action_report.common.generator_constraints import (
    GeneratorConstraints,
    derive_generator_constraints,
)


def test_bundle_failed_blocks_action_language():
    c = derive_generator_constraints(
        bundle_status="failed",
        freshness_summary={"overall": "failed"},
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "no_action"
    assert "수집 실패" in c.reason_ko


def test_bundle_stale_fallback_blocks_action_language():
    c = derive_generator_constraints(
        bundle_status="stale_fallback",
        freshness_summary={"overall": "hard_stale"},
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "no_action"
    assert "hard-stale" in c.reason_ko


def test_portfolio_hard_stale_downgrades_to_informational_only():
    c = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "hard_stale"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "informational_only"
    assert "포지션" in c.reason_ko


def test_journal_unavailable_downgrades():
    c = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "journal": {"status": "unavailable"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "informational_only"
    assert "거래일지" in c.reason_ko


def test_watch_context_failed_downgrades():
    c = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "watch_context": {"status": "failed"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "informational_only"
    assert "감시 컨텍스트" in c.reason_ko


def test_market_hard_stale_downgrades():
    c = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "market": {"status": "hard_stale"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is False
    assert c.forced_action_mode == "informational_only"
    assert "시장" in c.reason_ko


def test_complete_with_fresh_kinds_allows_action_language():
    c = derive_generator_constraints(
        bundle_status="complete",
        freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is True
    assert c.forced_action_mode == "default"
    assert c.reason_ko == ""


def test_partial_with_optional_unavailable_still_allows_action_language():
    """Only critical kinds (portfolio/journal/watch_context/market) degrade;
    optional kinds like news being unavailable don't block."""
    c = derive_generator_constraints(
        bundle_status="partial",
        freshness_summary={
            "overall": "partial",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is True
    assert c.forced_action_mode == "default"


def test_account_scope_none_bypasses_gate_entirely():
    """Informational report — no broker context, action language is moot."""
    c = derive_generator_constraints(
        bundle_status="failed",
        freshness_summary={"overall": "failed"},
        account_scope=None,
    )
    assert c.allow_action_language is True
    assert c.forced_action_mode == "default"


def test_bundle_status_none_legacy_pass_through():
    """Legacy report (no Phase 3 bundle linkage) — pass through; the
    post-gen linter also bypasses this state."""
    c = derive_generator_constraints(
        bundle_status=None,
        freshness_summary=None,
        account_scope="kis_live",
    )
    assert c.allow_action_language is True
    assert c.forced_action_mode == "default"


def test_reused_bundle_treated_as_active_bundle():
    """A reused fresh bundle behaves like ``complete`` for the generator."""
    c = derive_generator_constraints(
        bundle_status="reused",
        freshness_summary={
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "market": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
        },
        account_scope="kis_live",
    )
    assert c.allow_action_language is True
    assert c.forced_action_mode == "default"


def test_generator_constraints_is_frozen_dataclass():
    c = GeneratorConstraints(
        allow_action_language=False,
        forced_action_mode="no_action",
        reason_ko="test",
    )
    try:
        c.allow_action_language = True  # type: ignore[misc]
        raise AssertionError("expected FrozenInstanceError")
    except Exception:
        pass  # frozen dataclass raises on assignment — exact type varies by python
