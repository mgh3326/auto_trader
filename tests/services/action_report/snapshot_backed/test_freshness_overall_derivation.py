"""ROB-323 — core-aware overall freshness derivation.

Optional/external kinds (news, toss/naver/browser remote-debug, ...) must not
push the derived ``overall`` past the worst *core* kind status.
"""

from __future__ import annotations

from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
    _derive_overall_from_kind_statuses,
)


def test_derive_overall_ignores_excluded_optional_kinds() -> None:
    summary = {
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "fresh"},
        # Optional/external — all unavailable, must be excluded.
        "toss_remote_debug": {"status": "unavailable"},
        "naver_remote_debug": {"status": "unavailable"},
        "browser_probe": {"status": "unavailable"},
        "news": {"status": "unavailable"},
    }
    excluded = frozenset(
        {"toss_remote_debug", "naver_remote_debug", "browser_probe", "news"}
    )
    assert (
        _derive_overall_from_kind_statuses(summary, exclude_kinds=excluded) == "fresh"
    )


def test_derive_overall_still_reflects_worst_core_kind() -> None:
    summary = {
        "portfolio": {"status": "hard_stale"},
        "market": {"status": "fresh"},
        "toss_remote_debug": {"status": "unavailable"},
    }
    excluded = frozenset({"toss_remote_debug"})
    assert (
        _derive_overall_from_kind_statuses(summary, exclude_kinds=excluded)
        == "hard_stale"
    )


def test_derive_overall_defaults_to_no_exclusions() -> None:
    # Back-compat: called without exclude_kinds, behaves as before.
    summary = {"portfolio": {"status": "fresh"}, "news": {"status": "unavailable"}}
    assert _derive_overall_from_kind_statuses(summary) == "unavailable"


class _FakeEnsure:
    def __init__(self, status, freshness_summary, coverage_summary=None):
        self.status = status
        self.freshness_summary = freshness_summary
        self.coverage_summary = coverage_summary or {"required": {}, "optional": {}}


def _gen() -> SnapshotBackedReportGenerator:
    # __init__ only stores collaborators; _enrich_freshness_summary touches none.
    return SnapshotBackedReportGenerator.__new__(SnapshotBackedReportGenerator)


def test_enrich_mirrors_partial_bundle_status_not_optional_unavailable() -> None:
    """status='partial' (optional-only failure, core fresh) → overall='partial',
    never 'unavailable'."""
    ensure = _FakeEnsure(
        status="partial",
        freshness_summary={
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "toss_remote_debug": {"status": "unavailable"},
            "naver_remote_debug": {"status": "unavailable"},
            "browser_probe": {"status": "unavailable"},
        },
    )
    out = _gen()._enrich_freshness_summary(ensure)
    assert out["overall"] == "partial"


def test_enrich_failed_bundle_status_maps_to_failed_overall() -> None:
    ensure = _FakeEnsure(status="failed", freshness_summary={})
    out = _gen()._enrich_freshness_summary(ensure)
    assert out["overall"] == "failed"


def test_enrich_reused_falls_back_to_core_aware_per_kind() -> None:
    """status='reused' has no direct mapping → per-kind derivation that
    excludes the optional coverage bucket + external kinds."""
    ensure = _FakeEnsure(
        status="reused",
        freshness_summary={
            "portfolio": {"status": "fresh"},
            "market": {"status": "fresh"},
            "news": {"status": "unavailable"},
            "toss_remote_debug": {"status": "unavailable"},
        },
        coverage_summary={
            "required": {"portfolio": "fresh", "market": "fresh"},
            "optional": {"news": "unavailable", "toss_remote_debug": "unavailable"},
        },
    )
    out = _gen()._enrich_freshness_summary(ensure)
    assert out["overall"] == "fresh"


def test_enrich_keeps_explicit_overall() -> None:
    ensure = _FakeEnsure(
        status="partial",
        freshness_summary={"overall": "soft_stale", "portfolio": {"status": "fresh"}},
    )
    out = _gen()._enrich_freshness_summary(ensure)
    assert out["overall"] == "soft_stale"
