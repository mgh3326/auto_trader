# ROB-323 — /invest/reports Core Generation vs External Data-Quality Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop optional external cross-check sources (`toss_remote_debug`, `naver_remote_debug`, `browser_probe`) from polluting `/invest/reports` core report generation — they must never escalate `freshness_summary.overall`, the gate-relevant bundle status, the stale gate, the action-language lint, or the publish path; they surface only as a fail-open data-quality audit.

**Architecture:** The fix is narrow and reuses the existing `critical_kinds.py` contract. Root cause: the ensure service already computes a **core-aware** `bundle_status` (escalates only from the `required` coverage bucket), but the generator derives `freshness_summary.overall` via a **worst-across-ALL-kinds** scan (`_derive_overall_from_kind_statuses`), so the 3 always-`unavailable` stub collectors push `overall` to `"unavailable"`. That polluted `overall` is then re-mapped to `"stale_fallback"` by `_infer_bundle_status`, which blocks action language + publish, while `report_quality_summary` (fed the real `status="partial"`) reports `high_confidence` — the visible contradiction. We (1) make `overall` mirror the authoritative core-aware bundle status, (2) split core/optional/external in the diagnostics + add a fail-open data-quality audit embedded in the existing `snapshot_report_diagnostics` JSONB, and (3) render core data state separately from external cross-check state in the UI.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / pytest (`uv run pytest`); React + TypeScript + Vitest (`frontend/invest`, `npm test`).

**Slicing (confirmed):** 3 PRs.
- **PR1 — Backend gate semantics (the bugfix).** `EXTERNAL_AUDIT_KINDS` constant + core-aware `overall` derivation + regression tests. Closes the contradiction and the false block on its own.
- **PR2 — Diagnostics core/external split + data-quality audit shape.** No DB/schema migration — new keys ride inside the existing `snapshot_report_diagnostics` JSONB.
- **PR3 — Frontend `/invest/reports` core-vs-external rendering.** TS types + `ReportDiagnosticsPanel` split + tests.

**Design decisions (confirmed via review):**
- `overall` basis = **mirror the authoritative `bundle_status`** (already core-aware); per-kind fallback only for `reused` (no direct mapping), and even then exclude optional/external kinds.
- Audit storage = **embed in existing `snapshot_report_diagnostics` JSONB** (no new table, no migration).

**Non-goals / safety boundaries (carried from the issue):** No real KIS live order/cancel/modify. No broker/order/watch/order-intent mutation. No production DB backfill/cutover. No Prefect/TaskIQ scheduler registration/unpause. Toss/Naver/browser are never a trading authority and are never scraped from the frontend request path. No secret/env values printed. The real remote-debug collector stays a fail-open **stub** in this work — wiring an actual `127.0.0.1:9222` collector is explicit follow-up; all tests use fixtures/fakes.

---

## File Structure

**PR1 (backend semantics):**
- Modify: `app/services/action_report/common/critical_kinds.py` — add `EXTERNAL_AUDIT_KINDS`.
- Modify: `app/services/action_report/snapshot_backed/generator.py` — core-aware `overall` derivation (`_derive_overall_from_kind_statuses` gains `exclude_kinds`; `_enrich_freshness_summary` mirrors `bundle_status`; add `_optional_kind_names`).
- Test: `tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py` (new) — unit tests for the derivation.
- Test: `tests/services/action_report/snapshot_backed/test_generator.py` — integration regression tests via `generate()`.
- Test: `tests/services/action_report/common/test_critical_kinds_alignment.py` — extend with an `overall="unavailable"`-driven alignment case.

**PR2 (diagnostics + audit):**
- Modify: `app/services/action_report/common/diagnostics.py` — `build_report_quality_summary` core/optional/external split; new `build_external_cross_checks`, `build_data_quality_audit`; `build_report_diagnostics` threads them + optional `snapshot_bundle_uuid`.
- Modify: `app/services/action_report/snapshot_backed/generator.py` — pass `snapshot_bundle_uuid=ensure_response.bundle_uuid` into `build_report_diagnostics`.
- Test: `tests/services/action_report/common/test_diagnostics.py` — extend.

**PR3 (frontend):**
- Modify: `frontend/invest/src/types/investmentReports.ts` — extend `ReportQualitySummary`, add `ExternalCrossCheck` / `DataQualityAudit` types.
- Modify: `frontend/invest/src/components/investment-reports/ReportDiagnosticsPanel.tsx` — split core degraded chips from external cross-check section.
- Modify: `frontend/invest/src/components/investment-reports/reportDiagnosticsLabels.ts` — add external-kind labels + "리포트 생성에 영향 없음" copy.
- Test: `frontend/invest/src/__tests__/ReportDiagnosticsPanel.test.tsx` — extend.

---

# PR1 — Backend gate semantics (the bugfix)

### Task 1.1: Add `EXTERNAL_AUDIT_KINDS` classification constant

**Files:**
- Modify: `app/services/action_report/common/critical_kinds.py`
- Test: `tests/services/action_report/common/test_critical_kinds_alignment.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/common/test_critical_kinds_alignment.py`:

```python
def test_external_audit_kinds_are_disjoint_from_critical_kinds() -> None:
    """ROB-323 — the external cross-check kinds must never overlap the
    critical/core gating kinds, or an external probe could block generation."""
    from app.services.action_report.common.critical_kinds import (
        CRITICAL_SNAPSHOT_KINDS,
        EXTERNAL_AUDIT_KINDS,
    )

    assert EXTERNAL_AUDIT_KINDS == frozenset(
        {"toss_remote_debug", "naver_remote_debug", "browser_probe"}
    )
    assert EXTERNAL_AUDIT_KINDS.isdisjoint(set(CRITICAL_SNAPSHOT_KINDS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/common/test_critical_kinds_alignment.py::test_external_audit_kinds_are_disjoint_from_critical_kinds -v`
Expected: FAIL with `ImportError: cannot import name 'EXTERNAL_AUDIT_KINDS'`.

- [ ] **Step 3: Add the constant**

In `app/services/action_report/common/critical_kinds.py`, after the `CRITICAL_KIND_DEGRADING_STATUSES` definition (end of file, after line 40), append:

```python


# ROB-323 — External cross-check / data-quality audit kinds.
#
# These are operator-driven reference probes (Toss/Naver/browser remote-debug),
# NOT report-generation sources. Their unavailability must never escalate
# ``freshness_summary['overall']``, the gate-relevant bundle status, the stale
# gate, or the action-language lint. They surface only in the data-quality
# audit diagnostics with ``affects_report_generation=False``. Kept here, next to
# CRITICAL_SNAPSHOT_KINDS, because "what gates" and "what never gates" are two
# halves of the same contract.
EXTERNAL_AUDIT_KINDS: frozenset[str] = frozenset(
    {
        "toss_remote_debug",
        "naver_remote_debug",
        "browser_probe",
    }
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/common/test_critical_kinds_alignment.py::test_external_audit_kinds_are_disjoint_from_critical_kinds -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/common/critical_kinds.py tests/services/action_report/common/test_critical_kinds_alignment.py
git commit -m "feat(rob-323): add EXTERNAL_AUDIT_KINDS classification constant

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.2: Make `_derive_overall_from_kind_statuses` core-aware (exclude optional/external)

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py:109-126`
- Test: `tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py`:

```python
"""ROB-323 — core-aware overall freshness derivation.

Optional/external kinds (news, toss/naver/browser remote-debug, ...) must not
push the derived ``overall`` past the worst *core* kind status.
"""

from __future__ import annotations

from app.services.action_report.snapshot_backed.generator import (
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py -v`
Expected: FAIL — `test_derive_overall_ignores_excluded_optional_kinds` and `test_derive_overall_still_reflects_worst_core_kind` fail with `TypeError: _derive_overall_from_kind_statuses() got an unexpected keyword argument 'exclude_kinds'`.

- [ ] **Step 3: Add the `exclude_kinds` parameter**

In `app/services/action_report/snapshot_backed/generator.py`, replace the function at lines 109-126:

```python
def _derive_overall_from_kind_statuses(
    summary: Mapping[str, Any],
    *,
    exclude_kinds: frozenset[str] = frozenset(),
) -> str | None:
    """Return the worst per-kind status in ``summary``, or ``None`` if the
    summary carries no recognisable per-kind status entries.

    ``exclude_kinds`` (ROB-323) drops optional/external kinds so an
    operator-driven stub's ``unavailable`` cannot pollute the core overall.
    """
    worst_rank = -1
    for kind, info in summary.items():
        if kind == "overall" or kind in exclude_kinds or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        if not isinstance(status, str):
            continue
        rank = _KIND_STATUS_RANK.get(status)
        if rank is None:
            continue
        if rank > worst_rank:
            worst_rank = rank
    if worst_rank < 0:
        return None
    return _RANK_TO_KIND_STATUS[worst_rank]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py -v`
Expected: PASS (all 3).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py
git commit -m "feat(rob-323): core-aware overall derivation via exclude_kinds

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.3: Make `_enrich_freshness_summary` mirror the authoritative core-aware bundle status

**Files:**
- Modify: `app/services/action_report/snapshot_backed/generator.py` (add `_optional_kind_names`; rewrite `_enrich_freshness_summary` at lines 440-458; add `EXTERNAL_AUDIT_KINDS` import)
- Test: `tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py`:

```python
from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py -v -k enrich`
Expected: FAIL — `test_enrich_mirrors_partial_bundle_status_not_optional_unavailable` returns `"unavailable"` (current worst-across-all behavior), and `test_enrich_reused_falls_back_to_core_aware_per_kind` returns `"unavailable"`.

- [ ] **Step 3: Add the import**

In `app/services/action_report/snapshot_backed/generator.py`, find the existing import from `critical_kinds` (it imports `CRITICAL_KIND_DEGRADING_STATUSES`, `CRITICAL_SNAPSHOT_KINDS`) and add `EXTERNAL_AUDIT_KINDS`:

```python
from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
    EXTERNAL_AUDIT_KINDS,
)
```

- [ ] **Step 4: Add the `_optional_kind_names` helper**

In `app/services/action_report/snapshot_backed/generator.py`, immediately after `_derive_overall_from_kind_statuses` (after line 126), add:

```python


def _optional_kind_names(coverage_summary: Any) -> frozenset[str]:
    """Kinds that must not pollute the derived core ``overall`` (ROB-323).

    Union of the coverage summary's ``optional`` bucket and the always-external
    audit kinds. Used only on the ``reused`` fallback path, where there is no
    direct bundle-status → overall mapping.
    """
    coverage = to_jsonable(coverage_summary) or {}
    names: set[str] = set(EXTERNAL_AUDIT_KINDS)
    if isinstance(coverage, Mapping):
        optional = coverage.get("optional")
        if isinstance(optional, Mapping):
            names.update(str(k) for k in optional)
    return frozenset(names)
```

- [ ] **Step 5: Rewrite `_enrich_freshness_summary`**

Replace the method body at lines 440-458 with:

```python
    def _enrich_freshness_summary(self, ensure_response: Any) -> dict[str, Any]:
        summary = to_jsonable(ensure_response.freshness_summary) or {}
        if not isinstance(summary, dict):  # defensive
            summary = {"raw": summary}
        overall = summary.get("overall")
        if not isinstance(overall, str):
            # ROB-323 — prefer the authoritative, already core-aware bundle
            # status. snapshot_bundle._derive_bundle_status escalates only from
            # the 'required' coverage bucket, so optional/external kinds never
            # push it to stale_fallback/failed. Only fall back to a per-kind
            # scan when the status has no direct overall mapping (e.g.
            # 'reused'), and even then exclude optional/external kinds so an
            # operator-driven stub's 'unavailable' cannot pollute core overall.
            mapped = _BUNDLE_STATUS_TO_OVERALL.get(ensure_response.status)
            if mapped is not None:
                overall = mapped
            else:
                derived = _derive_overall_from_kind_statuses(
                    summary,
                    exclude_kinds=_optional_kind_names(
                        getattr(ensure_response, "coverage_summary", None)
                    ),
                )
                overall = (
                    derived
                    if derived is not None
                    else _BUNDLE_STATUS_TO_OVERALL.get(
                        ensure_response.status, "unavailable"
                    )
                )
            summary["overall"] = overall
        return summary
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py -v`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/snapshot_backed/generator.py tests/services/action_report/snapshot_backed/test_freshness_overall_derivation.py
git commit -m "feat(rob-323): overall mirrors core-aware bundle status

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.4: Integration regression — published report with external-only unavailable is not blocked

**Files:**
- Test: `tests/services/action_report/snapshot_backed/test_generator.py` (extend; reuse existing `_FakeEnsureService`, `_FakeIngestionService`, `_FakeSnapshotsRepository`, `_ensure_response`, `_make_request` helpers at the top of the file)

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/snapshot_backed/test_generator.py`:

```python
@pytest.mark.asyncio
async def test_rob323_external_only_unavailable_does_not_block_published() -> None:
    """ROB-323 — toss/naver/browser all unavailable + every critical kind
    fresh + bundle_status='partial' → published report generates, overall is
    NOT 'unavailable', and no PublishBlockedByStaleGateError is raised."""
    ensure = _FakeEnsureService(
        _ensure_response(
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
            missing_sources=[
                "toss_remote_debug",
                "naver_remote_debug",
                "browser_probe",
            ],
        )
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request(status="published"))
    assert response.snapshot_freshness_summary["overall"] == "partial"
    assert response.stale_gate["reject"] is False


@pytest.mark.asyncio
async def test_rob323_critical_unavailable_still_blocks_published() -> None:
    """ROB-323 — a CORE kind unavailable must keep failing closed even though
    the external sources are healthy."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="failed",
            freshness_summary={
                "portfolio": {"status": "unavailable"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
                "toss_remote_debug": {"status": "fresh"},
            },
            missing_sources=["portfolio"],
        )
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    with pytest.raises(PublishBlockedByStaleGateError):
        await gen.generate(_make_request(status="published"))
```

If `PublishBlockedByStaleGateError` is not already imported at the top of the test file, add it to the existing import from `app.services.action_report.snapshot_backed.generator`.

- [ ] **Step 2: Run test to verify it passes (fix already landed in 1.2/1.3)**

Run: `uv run pytest tests/services/action_report/snapshot_backed/test_generator.py -v -k rob323`
Expected: PASS for both. (These guard the PR1 fix — if `test_rob323_external_only_unavailable_does_not_block_published` fails with `PublishBlockedByStaleGateError` or `overall == "unavailable"`, Task 1.3 is incomplete.)

- [ ] **Step 3: Run the full action_report suite for regressions**

Run: `uv run pytest tests/services/action_report/ tests/test_investment_reports_stale_gate_flag.py -v`
Expected: PASS. If any pre-existing test asserted `overall == "unavailable"` for a fresh (non-reused) bundle whose only failures were optional, update its expectation to the core-aware value and note it in the commit body.

- [ ] **Step 4: Commit**

```bash
git add tests/services/action_report/snapshot_backed/test_generator.py
git commit -m "test(rob-323): published not blocked by external-only unavailable; core unavailable still blocks

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1.5: PR1 verification gate + open PR

**Files:** none (CI gate per `feedback_premerge_full_ci_gate` — branch protection does NOT run lint/tests).

- [ ] **Step 1: Lint**

Run: `uv run ruff check app/ tests/`
Expected: no errors.

- [ ] **Step 2: Targeted test sweep**

Run: `uv run pytest tests/services/action_report/ tests/test_investment_reports_stale_gate_flag.py -q`
Expected: all pass.

- [ ] **Step 3: Open PR (base `main`)**

```bash
git push -u origin rob-323
gh pr create --base main --title "feat(rob-323): /invest/reports core generation not gated by optional external sources (PR1/3)" --body "$(cat <<'EOF'
## What
Optional external cross-check sources (toss/naver/browser remote-debug) no longer escalate `freshness_summary.overall`, the gate-relevant bundle status, the stale gate, or the publish path.

## Root cause
The ensure service already computes a core-aware `bundle_status` (escalates only from the `required` coverage bucket), but the generator derived `overall` via a worst-across-ALL-kinds scan, so the 3 always-`unavailable` stub collectors pushed `overall` to `"unavailable"` → `_infer_bundle_status` re-mapped it to `"stale_fallback"` → action-language + publish block, while `report_quality_summary` reported `high_confidence`.

## Change (gate semantics)
- `EXTERNAL_AUDIT_KINDS` constant added next to `CRITICAL_SNAPSHOT_KINDS`.
- `overall` now mirrors the authoritative core-aware `bundle_status`; per-kind fallback (reused bundles only) excludes optional/external kinds.

## Tests
- Unit: `_derive_overall_from_kind_statuses` / `_enrich_freshness_summary` core-awareness.
- Regression: published report with all 3 external sources unavailable + core fresh generates (no `PublishBlockedByStaleGateError`); a core kind unavailable still fails closed.

## Follow-up
- PR2: diagnostics core/optional/external split + fail-open data-quality audit (embedded in `snapshot_report_diagnostics`).
- PR3: `/invest/reports` UI separation of core data state vs external cross-check state.
- The real `127.0.0.1:9222` remote-debug collector remains an out-of-scope fail-open stub.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Confirm the Test workflow is green before merge** (per `feedback_premerge_full_ci_gate` — do not `--auto` merge a red main).

---

# PR2 — Diagnostics core/external split + data-quality audit shape

> Branch from latest `origin/main` after PR1 merges (per CLAUDE.md follow-up rule): `git fetch --prune origin && git switch -c rob-323-pr2 origin/main`.

### Task 2.1: `build_external_cross_checks` — per-source external audit view (fail-open)

**Files:**
- Modify: `app/services/action_report/common/diagnostics.py` (add function; uses existing `EXTERNAL_AUDIT_KINDS` import)
- Test: `tests/services/action_report/common/test_diagnostics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/common/test_diagnostics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py -v -k external_cross_checks`
Expected: FAIL — `ImportError: cannot import name 'build_external_cross_checks'`.

- [ ] **Step 3: Implement**

In `app/services/action_report/common/diagnostics.py`, update the `critical_kinds` import (top of file, currently lines 40-43) to add `EXTERNAL_AUDIT_KINDS`:

```python
from app.services.action_report.common.critical_kinds import (
    CRITICAL_KIND_DEGRADING_STATUSES,
    CRITICAL_SNAPSHOT_KINDS,
    EXTERNAL_AUDIT_KINDS,
)
```

Then add, after `build_data_sufficiency_by_source` (after line 233):

```python


def build_external_cross_checks(
    freshness_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """ROB-323 — per-source view of the external cross-check / audit kinds.

    These are operator-driven reference probes, never report-generation
    sources, so every entry carries ``affects_report_generation=False``. Only
    external kinds present in the summary are included; the function is
    fail-open (an unavailable probe is reported, not raised).
    """
    summary = freshness_summary or {}
    out: dict[str, Any] = {}
    for kind in sorted(EXTERNAL_AUDIT_KINDS):
        info = summary.get(kind)
        if not isinstance(info, Mapping):
            continue
        status = info.get("status")
        entry: dict[str, Any] = {
            "status": status,
            "reason_code": reason_code_for(status, info),
            "affects_report_generation": False,
        }
        for key in ("reason", "as_of"):
            if key in info:
                entry[key] = info[key]
        out[kind] = entry
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py -v -k external_cross_checks`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/common/diagnostics.py tests/services/action_report/common/test_diagnostics.py
git commit -m "feat(rob-323): build_external_cross_checks (fail-open, affects_report_generation=false)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2.2: Split core/optional/external coverage in `build_report_quality_summary`

**Files:**
- Modify: `app/services/action_report/common/diagnostics.py:236-278`
- Test: `tests/services/action_report/common/test_diagnostics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/common/test_diagnostics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py -v -k "quality_summary_splits or quality_summary_external"`
Expected: FAIL — `KeyError: 'core_fresh_coverage_pct'`.

- [ ] **Step 3: Implement**

Replace `build_report_quality_summary` (lines 236-278) with:

```python
def build_report_quality_summary(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
) -> dict[str, Any]:
    """Report-level quality rollup: a grade + per-status counts.

    ROB-323 — coverage is split three ways so optional/external sources never
    distort the "is the report generatable" signal:
    * ``core_fresh_coverage_pct`` — CRITICAL_SNAPSHOT_KINDS only.
    * ``optional_fresh_coverage_pct`` — internal optional kinds (news, symbol,
      candidate_universe, ...), excluding external audit kinds.
    * ``external_cross_check_status`` — worst status across the external audit
      kinds present, or ``None`` if none were attempted.

    Grade (unchanged basis — critical kinds + bundle_status):
    * ``no_action`` — bundle failed or fell back to stale data.
    * ``informational_only`` — a critical kind is degrading.
    * ``high_confidence`` — all critical kinds usable.
    """
    summary = freshness_summary or {}
    counts: dict[str, int] = {}
    critical_statuses: list[str | None] = []
    core_fresh = core_total = 0
    optional_fresh = optional_total = 0
    for kind, info in summary.items():
        if kind == "overall" or not isinstance(info, Mapping):
            continue
        status = info.get("status")
        counts[str(status)] = counts.get(str(status), 0) + 1
        if kind in EXTERNAL_AUDIT_KINDS:
            continue  # surfaced via external_cross_check_status, not coverage
        if kind in CRITICAL_SNAPSHOT_KINDS:
            critical_statuses.append(status)
            core_total += 1
            if status == "fresh":
                core_fresh += 1
        else:
            optional_total += 1
            if status == "fresh":
                optional_fresh += 1

    total = sum(counts.values())
    fresh = counts.get("fresh", 0)
    fresh_pct = round(100 * fresh / total) if total else 0
    core_pct = round(100 * core_fresh / core_total) if core_total else 0
    optional_pct = round(100 * optional_fresh / optional_total) if optional_total else 0

    external = build_external_cross_checks(freshness_summary)
    external_status = _worst_external_status(external)

    grade: ReportQualityGrade
    if bundle_status in ("failed", "stale_fallback"):
        grade = "no_action"
    elif any(s in CRITICAL_KIND_DEGRADING_STATUSES for s in critical_statuses):
        grade = "informational_only"
    else:
        grade = "high_confidence"

    return {
        "grade": grade,
        "bundle_status": bundle_status,
        "freshness_overall": summary.get("overall"),
        "kind_status_counts": counts,
        "fresh_coverage_pct": fresh_pct,
        "core_fresh_coverage_pct": core_pct,
        "optional_fresh_coverage_pct": optional_pct,
        "external_cross_check_status": external_status,
    }


_EXTERNAL_STATUS_RANK: dict[str, int] = {
    "fresh": 0,
    "soft_stale": 1,
    "partial": 2,
    "hard_stale": 3,
    "failed": 4,
    "unavailable": 5,
}


def _worst_external_status(external: Mapping[str, Any]) -> str | None:
    worst: str | None = None
    worst_rank = -1
    for entry in external.values():
        status = entry.get("status") if isinstance(entry, Mapping) else None
        if not isinstance(status, str):
            continue
        rank = _EXTERNAL_STATUS_RANK.get(status, -1)
        if rank > worst_rank:
            worst_rank = rank
            worst = status
    return worst
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py -v -k quality`
Expected: PASS (including the pre-existing `test_quality_grade_*` tests — grade basis is unchanged).

- [ ] **Step 5: Commit**

```bash
git add app/services/action_report/common/diagnostics.py tests/services/action_report/common/test_diagnostics.py
git commit -m "feat(rob-323): split core/optional/external coverage in report_quality_summary

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2.3: `build_data_quality_audit` + thread it through `build_report_diagnostics`

**Files:**
- Modify: `app/services/action_report/common/diagnostics.py` (add `build_data_quality_audit`; extend `build_report_diagnostics` at lines 281-302)
- Modify: `app/services/action_report/snapshot_backed/generator.py:275-279` (pass `snapshot_bundle_uuid`)
- Test: `tests/services/action_report/common/test_diagnostics.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/action_report/common/test_diagnostics.py`:

```python
def test_build_data_quality_audit_shape() -> None:
    from app.services.action_report.common.diagnostics import build_data_quality_audit

    audit = build_data_quality_audit(
        freshness_summary={
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
            "toss_remote_debug": {"status": "unavailable", "reason_code": "unavailable"},
        },
        bundle_status="partial",
        snapshot_bundle_uuid="b-123",
    )
    assert audit["snapshot_bundle_uuid"] == "b-123"
    assert audit["core"]["status"] == "usable"
    assert audit["core"]["blocking_gaps"] == []
    assert audit["core"]["fresh_coverage_pct"] == 100
    assert audit["external_cross_checks"]["toss_remote_debug"][
        "affects_report_generation"
    ] is False
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py -v -k "data_quality_audit or report_diagnostics_includes"`
Expected: FAIL — `ImportError: cannot import name 'build_data_quality_audit'`, and `build_report_diagnostics() got an unexpected keyword argument 'snapshot_bundle_uuid'`.

- [ ] **Step 3: Implement `build_data_quality_audit`**

In `app/services/action_report/common/diagnostics.py`, add after `build_report_quality_summary` / `_worst_external_status`:

```python


def build_data_quality_audit(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
    snapshot_bundle_uuid: str | None = None,
) -> dict[str, Any]:
    """ROB-323 — the report's data-quality audit (embedded in
    ``snapshot_report_diagnostics``).

    Separates the "can we generate a report" core verdict from the external
    cross-check / reference signal. External probes are fail-open: an
    unavailable probe is an info-severity gap, never a blocker. Keyed by
    ``snapshot_bundle_uuid`` so the audit is reproducible from the bundle.
    """
    summary = freshness_summary or {}
    blocking_gaps = [
        kind
        for kind in CRITICAL_SNAPSHOT_KINDS
        if isinstance(summary.get(kind), Mapping)
        and summary[kind].get("status") in CRITICAL_KIND_DEGRADING_STATUSES
    ]
    quality = build_report_quality_summary(
        freshness_summary=freshness_summary, bundle_status=bundle_status
    )
    core_usable = (
        not blocking_gaps and bundle_status not in ("failed", "stale_fallback")
    )
    external = build_external_cross_checks(freshness_summary)

    gaps: list[dict[str, Any]] = []
    unavailable_external = sorted(
        kind
        for kind, entry in external.items()
        if entry.get("status") in ("unavailable", "failed")
    )
    if unavailable_external:
        gaps.append(
            {
                "severity": "info",
                "kind": "external_cross_check_unavailable",
                "sources": unavailable_external,
                "message": (
                    "외부 교차검증 소스 미수행 — 리포트 생성에는 영향 없음 "
                    "(operator remote-debug smoke로만 확인)"
                ),
            }
        )

    return {
        "snapshot_bundle_uuid": snapshot_bundle_uuid,
        "core": {
            "status": "usable" if core_usable else "degraded",
            "blocking_gaps": blocking_gaps,
            "fresh_coverage_pct": quality["core_fresh_coverage_pct"],
        },
        "external_cross_checks": external,
        "gaps": gaps,
    }
```

- [ ] **Step 4: Extend `build_report_diagnostics`**

Replace `build_report_diagnostics` (lines 281-302) with:

```python
def build_report_diagnostics(
    *,
    freshness_summary: Mapping[str, Any] | None,
    bundle_status: str | None,
    why_no_action: dict[str, Any] | None,
    snapshot_bundle_uuid: str | None = None,
) -> dict[str, Any]:
    """Assemble the ``snapshot_report_diagnostics`` JSONB payload.

    ROB-318 PR-B rollups + the ROB-323 ``data_quality_audit`` (core verdict vs
    fail-open external cross-checks). ``why_no_action`` is computed by the
    caller (it needs to know whether action items were produced).
    """
    return {
        "why_no_action": why_no_action,
        "data_sufficiency_by_source": build_data_sufficiency_by_source(
            freshness_summary
        ),
        "report_quality_summary": build_report_quality_summary(
            freshness_summary=freshness_summary,
            bundle_status=bundle_status,
        ),
        "data_quality_audit": build_data_quality_audit(
            freshness_summary=freshness_summary,
            bundle_status=bundle_status,
            snapshot_bundle_uuid=snapshot_bundle_uuid,
        ),
    }
```

- [ ] **Step 5: Pass `snapshot_bundle_uuid` from the generator**

In `app/services/action_report/snapshot_backed/generator.py`, update the `build_report_diagnostics(...)` call (lines 275-279) to:

```python
        report_diagnostics = build_report_diagnostics(
            freshness_summary=freshness_summary,
            bundle_status=ensure_response.status,
            why_no_action=why_no_action,
            snapshot_bundle_uuid=str(ensure_response.bundle_uuid),
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/services/action_report/common/test_diagnostics.py tests/services/action_report/snapshot_backed/test_generator.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/action_report/common/diagnostics.py app/services/action_report/snapshot_backed/generator.py tests/services/action_report/common/test_diagnostics.py
git commit -m "feat(rob-323): data_quality_audit embedded in snapshot_report_diagnostics

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2.4: PR2 verification gate + open PR

- [ ] **Step 1: Lint + targeted tests**

Run: `uv run ruff check app/ tests/ && uv run pytest tests/services/action_report/ -q`
Expected: all pass.

- [ ] **Step 2: Open PR (base `main`)**

```bash
git push -u origin rob-323-pr2
gh pr create --base main --title "feat(rob-323): diagnostics core/external split + fail-open data-quality audit (PR2/3)" --body "$(cat <<'EOF'
## What
`report_quality_summary` now reports `core_fresh_coverage_pct`, `optional_fresh_coverage_pct`, and `external_cross_check_status` separately. A new `data_quality_audit` block (core verdict + fail-open `external_cross_checks` + info-severity gaps) is embedded in the existing `snapshot_report_diagnostics` JSONB — **no DB migration, no schema change** (rides inside the JSONB column already serialized by `InvestmentReportResponse`).

## Safety
External cross-checks always carry `affects_report_generation=false`; an unavailable external probe is an `info` gap, never `blocking`. Grade basis is unchanged (critical kinds + bundle_status).

## Tests
External cross-check shape, core/optional/external coverage split, audit shape (usable vs degraded), diagnostics threading.

## Follow-up
PR3: `/invest/reports` UI renders core data state separately from external cross-check state ("리포트 생성에 영향 없음").

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Confirm Test workflow green before merge.**

---

# PR3 — Frontend `/invest/reports` core-vs-external rendering

> Branch from latest `origin/main` after PR2 merges.

### Task 3.1: Extend TS types for the new diagnostics fields

**Files:**
- Modify: `frontend/invest/src/types/investmentReports.ts:114-138`

- [ ] **Step 1: Extend `ReportQualitySummary` and add audit types**

In `frontend/invest/src/types/investmentReports.ts`, replace the `ReportQualitySummary` interface (lines 119-125) with:

```typescript
export interface ReportQualitySummary {
  grade: ReportQualityGrade;
  bundle_status?: string | null;
  freshness_overall?: SnapshotFreshnessStatus | string | null;
  kind_status_counts?: Record<string, number>;
  fresh_coverage_pct?: number;
  // ROB-323 — core vs optional vs external split.
  core_fresh_coverage_pct?: number;
  optional_fresh_coverage_pct?: number;
  external_cross_check_status?: SnapshotFreshnessStatus | string | null;
}

// ROB-323 — external cross-check / data-quality audit (embedded in
// snapshot_report_diagnostics). External probes never affect report generation.
export interface ExternalCrossCheck {
  status?: SnapshotFreshnessStatus | string | null;
  reason_code?: string | null;
  reason?: string | null;
  as_of?: string | null;
  affects_report_generation: false;
}

export interface DataQualityGap {
  severity: "info" | "warning" | "blocking";
  kind: string;
  sources?: string[];
  message: string;
}

export interface DataQualityAudit {
  snapshot_bundle_uuid?: string | null;
  core: {
    status: "usable" | "degraded";
    blocking_gaps: string[];
    fresh_coverage_pct?: number;
  };
  external_cross_checks: Record<string, ExternalCrossCheck>;
  gaps: DataQualityGap[];
}
```

Then extend the `SnapshotReportDiagnostics` interface (lines 134-138) to add the audit field:

```typescript
export interface SnapshotReportDiagnostics {
  why_no_action?: WhyNoAction | null;
  data_sufficiency_by_source?: Record<string, DataSufficiencySource>;
  report_quality_summary?: ReportQualitySummary | null;
  data_quality_audit?: DataQualityAudit | null;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: no errors (additive optional fields).

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/types/investmentReports.ts
git commit -m "feat(rob-323): TS types for external cross-check + data-quality audit

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.2: Add external-kind labels + "no impact" copy

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/reportDiagnosticsLabels.ts`

- [ ] **Step 1: Add labels**

In `frontend/invest/src/components/investment-reports/reportDiagnosticsLabels.ts`, extend `DIAGNOSTIC_KIND_LABELS` (lines 35-43) with the external kinds and add an export for the section copy:

```typescript
// Per-kind Korean labels (mirror snapshot_kind on the backend).
export const DIAGNOSTIC_KIND_LABELS: Record<string, string> = {
  portfolio: "포지션",
  journal: "거래일지",
  watch_context: "감시",
  market: "시장",
  news: "뉴스",
  candidate_universe: "후보군",
  symbol: "종목",
  // ROB-323 — external cross-check sources.
  toss_remote_debug: "토스증권 교차검증",
  naver_remote_debug: "네이버증권 교차검증",
  browser_probe: "브라우저 교차검증",
};

// ROB-323 — external cross-check section copy.
export const EXTERNAL_CROSS_CHECK_TITLE = "외부 교차검증";
export const EXTERNAL_CROSS_CHECK_NOTE = "리포트 생성에는 영향 없음";
```

- [ ] **Step 2: Typecheck**

Run: `cd frontend/invest && npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/invest/src/components/investment-reports/reportDiagnosticsLabels.ts
git commit -m "feat(rob-323): external cross-check labels + no-impact copy

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.3: Render core data state separately from external cross-check state

**Files:**
- Modify: `frontend/invest/src/components/investment-reports/ReportDiagnosticsPanel.tsx`
- Test: `frontend/invest/src/__tests__/ReportDiagnosticsPanel.test.tsx`

- [ ] **Step 1: Write the failing tests**

Append to `frontend/invest/src/__tests__/ReportDiagnosticsPanel.test.tsx`:

```typescript
it("renders external cross-checks in a separate 'no impact' section, not the core chip row", () => {
  const diagnostics: SnapshotReportDiagnostics = {
    data_sufficiency_by_source: {
      portfolio: { status: "fresh" },
      // External sources also appear here today; they must NOT show as core chips.
      toss_remote_debug: { status: "unavailable", reason_code: "unavailable" },
    },
    data_quality_audit: {
      core: { status: "usable", blocking_gaps: [], fresh_coverage_pct: 100 },
      external_cross_checks: {
        toss_remote_debug: {
          status: "unavailable",
          reason_code: "unavailable",
          affects_report_generation: false,
        },
      },
      gaps: [],
    },
  };
  render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);

  // External source is NOT rendered as a core degraded chip.
  expect(
    screen.queryByTestId("report-diagnostics-source-toss_remote_debug"),
  ).toBeNull();

  // It appears in the dedicated external section, with the no-impact note.
  const ext = screen.getByTestId("report-diagnostics-external");
  expect(ext).toHaveTextContent("외부 교차검증");
  expect(ext).toHaveTextContent("리포트 생성에는 영향 없음");
  expect(
    screen.getByTestId("report-diagnostics-external-toss_remote_debug"),
  ).toHaveTextContent("토스증권 교차검증");
});

it("does not render the external section when no external cross-checks exist", () => {
  const diagnostics: SnapshotReportDiagnostics = {
    report_quality_summary: { grade: "high_confidence" },
  };
  render(<ReportDiagnosticsPanel diagnostics={diagnostics} />);
  expect(screen.queryByTestId("report-diagnostics-external")).toBeNull();
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend/invest && npx vitest run src/__tests__/ReportDiagnosticsPanel.test.tsx`
Expected: FAIL — `report-diagnostics-external` not found, and `report-diagnostics-source-toss_remote_debug` currently renders (the existing degraded-chip row includes it).

- [ ] **Step 3: Update the component**

In `frontend/invest/src/components/investment-reports/ReportDiagnosticsPanel.tsx`:

(a) Extend the label import (lines 21-26):

```typescript
import {
  DIAGNOSTIC_KIND_LABELS,
  EXTERNAL_CROSS_CHECK_NOTE,
  EXTERNAL_CROSS_CHECK_TITLE,
  QUALITY_GRADE_LABELS,
  REASON_CODE_LABELS,
  WHY_NO_ACTION_LABELS,
} from "./reportDiagnosticsLabels";
```

(b) Add an external-kind guard constant after `DEGRADED_STATUSES` (line 36):

```typescript
const EXTERNAL_AUDIT_KINDS = new Set([
  "toss_remote_debug",
  "naver_remote_debug",
  "browser_probe",
]);
```

(c) In the component body, change the `degraded` filter (lines 59-61) to exclude external kinds, and derive the external entries from the audit:

```typescript
  const sufficiency = diagnostics.data_sufficiency_by_source ?? {};
  const audit = diagnostics.data_quality_audit ?? null;

  // Core degraded sources only — external cross-checks render in their own
  // section so an unavailable probe never reads as a broken report.
  const degraded: [string, DataSufficiencySource][] = Object.entries(
    sufficiency,
  ).filter(
    ([kind, info]) =>
      !EXTERNAL_AUDIT_KINDS.has(kind) &&
      info?.status != null &&
      DEGRADED_STATUSES.has(info.status),
  );

  const externalChecks = Object.entries(audit?.external_cross_checks ?? {});
```

(d) Update the early-return guard (line 65) so the panel still renders when only external checks exist:

```typescript
  const hasQuality = quality?.grade != null;
  const hasWhy = why?.kind != null;
  if (
    !hasQuality &&
    !hasWhy &&
    degraded.length === 0 &&
    externalChecks.length === 0
  )
    return null;
```

(e) Add the external section as the last child inside the root `<div>` (after the `degraded.length > 0 ? (...) : null` block at line 135, before the closing `</div>`):

```typescript
      {externalChecks.length > 0 ? (
        <div
          data-testid="report-diagnostics-external"
          style={{ display: "grid", gap: 4 }}
        >
          <span style={{ fontSize: 12, color: "var(--fg-3)" }}>
            {EXTERNAL_CROSS_CHECK_TITLE} · {EXTERNAL_CROSS_CHECK_NOTE}
          </span>
          <ul
            aria-label="외부 교차검증 상태"
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
            }}
          >
            {externalChecks.map(([kind, info]) => (
              <li
                key={kind}
                data-testid={`report-diagnostics-external-${kind}`}
                style={{ ...chipStyle, opacity: 0.7 }}
              >
                {DIAGNOSTIC_KIND_LABELS[kind] ?? kind} ·{" "}
                {statusLabel(info.status)}
                {info.reason_code
                  ? ` (${REASON_CODE_LABELS[info.reason_code] ?? info.reason_code})`
                  : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend/invest && npx vitest run src/__tests__/ReportDiagnosticsPanel.test.tsx`
Expected: PASS (new + pre-existing tests). The pre-existing `renders degraded source chips ...` test uses `portfolio` (core), so it is unaffected.

- [ ] **Step 5: Typecheck + full frontend test run**

Run: `cd frontend/invest && npm run typecheck && npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/components/investment-reports/ReportDiagnosticsPanel.tsx frontend/invest/src/__tests__/ReportDiagnosticsPanel.test.tsx
git commit -m "feat(rob-323): render external cross-checks separately from core data state

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3.4: PR3 verification gate + open PR

- [ ] **Step 1: Frontend gate**

Run: `cd frontend/invest && npm run typecheck && npm test`
Expected: all pass.

- [ ] **Step 2: Open PR (base `main`)**

```bash
git push -u origin rob-323-pr3
gh pr create --base main --title "feat(rob-323): /invest/reports separates core data state from external cross-check state (PR3/3)" --body "$(cat <<'EOF'
## What
`ReportDiagnosticsPanel` now renders external cross-check sources (toss/naver/browser) in a dedicated "외부 교차검증 · 리포트 생성에는 영향 없음" section, dimmed, instead of mixing them into the core degraded-source chip row. Core data state and external cross-check state are visually distinct.

## Tests
External cross-checks render in the separate section (not as core chips); section is hidden when no external checks exist; pre-existing core-chip tests unaffected.

## Closes
ROB-323 backend semantics (PR1) + diagnostics/audit (PR2) + this UI separation. Real `127.0.0.1:9222` remote-debug collector remains out-of-scope fail-open stub (documented follow-up).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Confirm Test workflow green before merge.**

---

## Acceptance Criteria Coverage Map

| AC (from ROB-323) | Task |
|---|---|
| Backend: external unavailable alone never makes `overall=unavailable`/`stale_fallback` | 1.2, 1.3 |
| Backend: core usable ⇒ generation/publish not blocked by external | 1.3, 1.4 |
| Backend: core unavailable/hard-stale/failed ⇒ fail-closed preserved | 1.4 (`test_rob323_critical_unavailable_still_blocks_published`) |
| Backend: `report_quality_summary` splits core vs optional/external | 2.2 |
| Backend: optional unavailable marked `affects_report_generation=false` | 2.1, 2.3 |
| Backend: no `freshness_overall=unavailable` + `grade=high_confidence` contradiction | 1.3 (overall now mirrors core-aware status) |
| Audit: runnable keyed by `snapshot_bundle_uuid` | 2.3 |
| Audit: Toss/Naver/browser fail-open | 2.1 (stubs already fail-open) + 2.3 (info gap, never blocking) |
| Audit: failure = warning/gap, not generation failure | 2.3 |
| Audit: includes source, as_of, reason_code, affects_report_generation | 2.1, 2.3 |
| UI: core vs external state separated | 3.3 |
| UI: external unavailable shows "리포트 생성에는 영향 없음" | 3.2, 3.3 |
| UI: draft fallback shows the core blocker; optional-only never causes draft fallback | 1.3 (overall no longer downgrades from optional) + existing `why_no_action` (`blocking_sources` = critical kinds only) |
| Tests: 3 external unavailable + core usable ⇒ not blocked | 1.4 |
| Tests: critical unavailable ⇒ block/degrade | 1.4 |
| Tests: diagnostics split core/optional coverage | 2.2 |
| Tests: frontend renders optional unavailable as external audit warning | 3.3 |

## Notes / open follow-ups
- **`report_uuid` in the audit:** at generation time only `snapshot_bundle_uuid` is known (the report row is inserted afterward). The audit is keyed by `snapshot_bundle_uuid`, which the issue accepts ("`report_uuid` 또는 `snapshot_bundle_uuid` 기준"). If a `report_uuid`-keyed audit is later required, add it in the query/serializer layer where the row is already loaded.
- **Real remote-debug collector:** out of scope. The 3 stubs stay fail-open. Wiring an actual `127.0.0.1:9222` collector (`$HOME/.hermes/chrome-toss-debug` profile) is a separate operator-smoke task; all tests here use fixtures/fakes.
- **DB CHECK alignment:** with `overall` now mirroring a core-aware `partial` (instead of `unavailable`) for optional-only failures, published reports that were previously rejected by `ck_investment_reports_no_published_on_hard_stale` (overall must be in {fresh, soft_stale, partial}) now pass — this is the intended behavior, not a constraint relaxation.
