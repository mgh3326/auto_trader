# ROB-269 Phase 4 — UI Provenance + Reviewer + Scheduler (Short Plan)

> Phase 4 of ROB-269. **Single feature branch off `main`**, single PR (no sub-stacking). All previous phases (PR 874 / 875 / 876) are merged.
>
> **Branch:** `rob-269-phase4` (off `origin/main` @ `0c50643e`)
> **Worktree:** `/Users/mgh3326/work/auto_trader.rob-269-phase4`
> **Goal:** Close the ROB-269 loop — surface bundle freshness in the `/invest` UI, register a Prefect snapshot-refresh flow (importable only, no deployment), and document the reviewer-handoff path (existing Phase 2 MCP tools serve as the surface). All gated by `ACTION_REPORT_BUNDLE_UI_ENABLED` (default off) and Prefect-scheduleless in code.

---

## 1. Frontend `/invest` UI surface

* New typed fields on `frontend/invest/src/api/investmentReports.ts` parser:
  - `snapshotBundleUuid`, `snapshotPolicyVersion`, `snapshotCoverageSummary`,
    `snapshotFreshnessSummary`, `sourceConflicts`, `unavailableSources`
* New component `frontend/invest/src/components/investment-reports/SnapshotBundleFreshnessChip.tsx`:
  - Inputs: `freshnessSummary` (the JSON shape from Phase 3) + the new feature-flag value.
  - Renders a Korean-facing chip per source with status colour: `신선` (green) / `일부 지연` (yellow) / `오래됨` (orange) / `확인 불가` (gray) / `실패` (red).
  - Bundle-level summary line: e.g. `스냅샷 11:11 · 부분 (news 확인 불가)`.
  - When freshness summary is `null` (legacy report) → render nothing (no chip).
* Integrate in `InvestmentReportBundleContent.tsx` at the top of the bundle header card.
* Korean copy lock — labels are constants in the new component, not magic strings.
* Tests via Vitest in `frontend/invest/src/__tests__/SnapshotBundleFreshnessChip.test.tsx`:
  - flag off → component is hidden (no DOM render even if data present).
  - flag on + legacy report → no chip.
  - flag on + fresh complete → green chip with `신선` label.
  - flag on + partial with optional unavailable → yellow chip + per-source dim chips.
  - flag on + hard_stale critical kind → orange/red chip.

## 2. Reviewer handoff

The existing Phase 2 MCP tools (`investment_snapshot_bundle_ensure / _get / _list / _refresh_request`) are the reviewer surface — they already accept `requested_by: 'reviewer'` and expose `snapshot_bundle_uuid` on responses. No new reviewer service or endpoint in this PR.

* `docs/runbooks/snapshot-reviewer-handoff.md` — concise reviewer playbook documenting which MCP tools to call for: (a) reading a report's bundle context, (b) requesting a refresh, (c) interpreting freshness/coverage summaries. **Doc-only, no code.**
* Phase 3 `InvestmentReportResponse` already exposes the 6 snapshot metadata fields → reviewer agents (Claude / Codex / Gemini) consuming the HTTP read surface get bundle context for free.

## 3. Prefect scheduler

* New flow `app/flows/investment_snapshots_refresh_flow.py`:
  - Mirrors the `invest_screener_snapshots_us_flow.py` pattern: **importable only, no deployment registered in this PR**.
  - Wraps `SnapshotBundleEnsureService.ensure` with `mode='ensure_fresh'` for `purpose='kr_action_report'`.
  - **No env flag for the flow itself** — the production registry is empty (Phase 2 design), so a manual or scheduled run effectively no-ops in `ensure_fresh` mode until Phase 5+ collectors register. The Prefect deployment registration (paused or active) is a separate ops change, not part of this PR.
* Tests: `tests/flows/test_investment_snapshots_refresh_flow.py` — importability + a single dry-run that hits the empty-collector path and returns the bundle-status payload.

## 4. Feature flag behavior

`ACTION_REPORT_BUNDLE_UI_ENABLED: bool = False` in `app/core/config.py`.

* Exposed to the frontend via the existing user-settings / feature-flags surface (extend whichever endpoint already exposes flags to the SPA — discover during implementation).
* Frontend's `SnapshotBundleFreshnessChip` reads the flag at render time; flag off → component returns `null`.
* Backend has no enforcement tied to this flag (the chip is purely a UI surface; Phase 3 stale-gate already gates publication semantics).

## 5. Tests

* **Backend** (~3 new):
  - `tests/test_config_flags.py` — defaults for `ACTION_REPORT_BUNDLE_UI_ENABLED` (locks at False).
  - `tests/flows/test_investment_snapshots_refresh_flow.py` — flow importable + dry-run.
  - Mutation boundary scope updated to include the new flow file (no broker mutation).
* **Frontend** (~5 new) — Vitest cases for the chip component (flag matrix × data states).
* **Regression**: Phase 1+2+3 full sweep (370+ tests including Phase 4 additions) plus existing investment_reports tests stay green.

## 6. Expected files

**Create (~7 new):**
* `app/flows/investment_snapshots_refresh_flow.py`
* `docs/runbooks/snapshot-reviewer-handoff.md`
* `frontend/invest/src/components/investment-reports/SnapshotBundleFreshnessChip.tsx`
* `frontend/invest/src/__tests__/SnapshotBundleFreshnessChip.test.tsx`
* `tests/flows/test_investment_snapshots_refresh_flow.py` (+ `__init__.py` if needed)
* `tests/flows/__init__.py`

**Modify (~5):**
* `app/core/config.py` — add `ACTION_REPORT_BUNDLE_UI_ENABLED`.
* `frontend/invest/src/api/investmentReports.ts` — parser additions for 6 snapshot fields.
* `frontend/invest/src/types/investmentReports.ts` — type additions.
* `frontend/invest/src/components/investment-reports/InvestmentReportBundleContent.tsx` — render the chip at the header.
* `tests/services/investment_snapshots/test_mutation_boundary.py` — add flow file to scope (string-only audit, no broker mutation expected).
* Whichever backend endpoint surfaces flags to SPA (discover; minimal additive change).

## 7. Non-goals (explicit out-of-scope for Phase 4)

* ❌ Prefect deployment registration (paused or active) — this is an ops change.
* ❌ Production collectors (KIS/Upbit/Naver/Toss/news) for `SnapshotCollectorRegistry` — Phase 5 / future work.
* ❌ Reviewer-agent automation (Claude/Codex/Gemini orchestration) — runbook documents the handoff but no agent code.
* ❌ Feature flag flip (`ACTION_REPORT_BUNDLE_UI_ENABLED`, others) — owner-area, post-merge.
* ❌ Production deploy / service restart.
* ❌ Broker / order / watch-intent mutation (always banned).
* ❌ Live KIS / Upbit / Alpaca HTTP calls — Phase 2 boundary still enforced.
* ❌ Existing UI regression — additive only; no edits to non-`investment-reports` views.

---

## Implementation order (single branch, multiple commits, one PR)

1. `feat(rob-269-p4): ACTION_REPORT_BUNDLE_UI_ENABLED flag (default off)` — config + 1 test.
2. `feat(rob-269-p4): typed API + types for snapshot metadata on InvestmentReportResponse` — frontend parser/types only.
3. `feat(rob-269-p4): SnapshotBundleFreshnessChip component + Vitest` — new component + tests.
4. `feat(rob-269-p4): render freshness chip in InvestmentReportBundleContent` — integration + render guard on flag.
5. `feat(rob-269-p4): Prefect snapshot-refresh flow (importable only, no deployment)` — flow + flow test.
6. `docs(rob-269-p4): reviewer handoff runbook` — `docs/runbooks/snapshot-reviewer-handoff.md`.
7. `test(rob-269-p4): mutation boundary scope` — add flow file.
8. `chore(rob-269-p4): ruff/format/eslint cleanup` if needed.

Final: push `rob-269-phase4` to origin, single PR base `main`, draft until CI green + owner review.

## Handoff format

* Branch: `rob-269-phase4` (off `origin/main` head)
* Tests: backend pytest summary + frontend vitest summary
* Lint: ruff clean + (skip eslint if not configured) report
* Local commits: list
* No push other than this branch.
* Open `# TODO(rob-269 reviewer):` notes: file + line if any.

After completion: owner draft-flip + approval + squash merge close Phase 4 → ROB-269 fully landed on main.
