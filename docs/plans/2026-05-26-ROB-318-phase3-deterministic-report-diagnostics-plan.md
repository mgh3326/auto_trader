# ROB-318 Phase 3 — deterministic report diagnostics (eng-review plan)

- **Date**: 2026-05-26 (KST)
- **Branch**: follow-up to `rob-318` (PR #953 = Slice A + audit)
- **Predecessor doc**: `2026-05-26-ROB-318-invest-reports-reference-audit-and-bugfix.md` §4
- **Status**: DRAFT for eng-review

## Problem

After the Slice A fix, KR/kis_live reports can collect a live portfolio, but
`/invest/reports` still surfaces only a **generic** Korean reason
("포지션 데이터 확인 불가 — 매수/매도 권고 불가") when a required source is degraded,
and it cannot tell the operator whether a no-action conclusion is a **real**
no-action or a **data-insufficient / stale-gated** one. The collector already
records a specific reason (`errors_json.reason`), but it is dropped before it
reaches the operator-visible `freshness_summary`.

## Goal (Phase 3 scope = report-level diagnostics)

Deterministic, structured evidence the report can show and Hermes can compose
from. **No in-process LLM** (ROB-287 / PR #898 guard). Narrative is Hermes;
deterministic fallback templates are allowed.

In scope (this plan):
1. Source-specific diagnostics (per-kind `reason_code` + `reason`).
2. Report-level `why_no_action` (real vs stale_gated vs data_insufficient).
3. `report_quality_summary` + `data_sufficiency_by_source` rollups.
4. Export the above to Hermes (optional fields).
5. Frontend rendering on `/invest/reports` detail.

**Deferred to Phase 3b (separate issue/PR)** — candidate/holding-level evidence
(`new_candidate_reference_signals`, `held_strategy_change_signals`,
`external_reference_checks`, `screener_condition_provenance`). These touch the
candidate / journal / watch / screener / browser-reference collectors and are a
larger surface; they consume the same deterministic-container pattern. Out of
this plan to keep the review tight.

## Architecture & data flow

```
collector (errors_json={reason_code, reason})           ← Slice 1 source
  → snapshot_bundle.ensure(): freshness_summary[kind]    snapshot_bundle.py:181,229
      {status, as_of, result_count, reason_code?, reason?}
  → derive_generator_constraints()                       generator_constraints.py:44
      → GeneratorConstraints(forced_action_mode, reason_ko)
  → generator.generate(): build why_no_action +          generator.py:251-272
      report_quality_summary + data_sufficiency_by_source
  → persist on InvestmentReport (new JSONB column)        models/investment_reports.py
  → HermesContextExporter.export(): expose to Hermes      hermes_context.py:247-263
  → GET report detail → frontend renders                  frontend/invest/...
```

The deterministic boundary: collectors + assembler + generator produce
**structured facts**; Hermes consumes them for prose. The only strings the
deterministic layer emits are `reason_ko` (already exists) and enum-driven
fallback templates.

## Sub-slices (proposed ordering)

### Slice 1 — per-kind `reason_code` + `reason` (no migration, no Hermes break)
- `snapshot_bundle.py` (lines 181-184 empty-results branch; 229-233 populated
  branch): when status ∈ {unavailable, hard_stale, failed}, copy
  `result.errors_json.get("reason")` into `freshness_summary[kind]` and add a
  normalized `reason_code`.
- Collectors emit a `reason_code` enum alongside the human `reason`. Start with
  portfolio (`portfolio.py:273`): `user_id_missing`, `kis_fetch_failed`,
  `stale`. Other critical kinds get `unavailable`/`stale`/`failed` defaults.
- Backward compatible: existing consumers read `freshness_summary[kind]["status"]`;
  new keys are additive. `HermesContextPayload` re-exports freshness_summary as-is.
- Tests: extend `test_collectors.py`, `test_generator_constraints.py`,
  `test_stale_gate.py` fixtures with reason_code assertions.

### Slice 2 — `why_no_action` (report-level, deterministic)
- In `generator.py` after `derive_generator_constraints`, map
  `forced_action_mode` + blocking kinds → `why_no_action`:
  `{kind: real_no_action | stale_gated | data_insufficient,
    blocking_sources: [kind...], reason_ko}`.
  - `data_insufficient` = required source unavailable (e.g. portfolio
    user_id_missing/kis_fetch_failed).
  - `stale_gated` = required source hard_stale / bundle stale_fallback.
  - `real_no_action` = all critical fresh, generator still emits no action.
- Added to `ReportGenerationResponse` (request.py) and the Hermes context.
- Tests: `test_generator.py` / `test_generator_constraints.py`.

### Slice 3 — `report_quality_summary` + `data_sufficiency_by_source` (+ migration)
- `report_quality_summary` (report-level rollup): coverage %, fresh/stale/
  unavailable counts, grade ∈ {high_confidence, informational_only, no_action}.
- `data_sufficiency_by_source`: per-source `{status, reason_code, as_of, origin}`
  derived from `unavailable_sources` + `freshness_summary`.
- **Persistence**: new nullable JSONB column `snapshot_quality_summary` on
  `investment_reports`. Alembic migration mirrors
  `alembic/versions/20260519_rob269_p3_add_snapshot_metadata_to_investment_reports.py`.
  `data_sufficiency_by_source` can live inside this column (no second migration).
- **Open question for review**: persist as a first-class column (queryable
  "show low-quality reports") vs nest in an existing JSON metadata field
  (no migration). Recommend first-class column for future filtering.
- Migration is included in the PR but `alembic upgrade head` is operator-run
  (cutover gate, per repo convention).

### Slice 4 — Hermes context export (optional fields)
- `hermes_context.py` / `app/schemas/hermes_composition.py`
  (`HermesContextPayload`): add `data_sufficiency_by_source`, `why_no_action`,
  `report_quality_summary` as `... | None` (graceful degradation if Hermes lags).
- Tests: `test_hermes_*` context/roundtrip.

### Slice 5 — frontend `/invest/reports` detail
- Render: quality grade badge, per-source data-sufficiency chips with
  reason_code, and the real-vs-stale-gated no-action distinction.
- `external_reference_checks` remains optional/fail-open (Phase 3b) — if absent,
  render "확인 불가", never infer.
- Tests: frontend component tests alongside existing snapshot-evidence rendering.

## Edge cases / invariants

- Fail-open: missing optional reference data must never crash generation.
- No in-process LLM anywhere in the assembler/generator path (PR #898 guard
  must stay green — verify import guard covers new code paths).
- `reason_code` is a closed enum; unknown collector reasons map to a generic
  code, never a free-form code, so the frontend can switch on it.
- Backward compat: reports generated before Phase 3 have null new fields; UI
  degrades gracefully.
- No broker/order/watch/order-intent mutation (side-effect guards stay green).

## Test coverage plan + coverage map

```
CODE PATHS                                                  STATUS
[+] snapshot_bundle.ensure (Slice 1)
  ├── reason_code/reason copied when status degraded         [GAP] unit — test_collectors/test_snapshot_bundle
  └── status fresh → no reason key (no leakage)              [GAP] unit
[+] generator_constraints / generator (Slice 2)
  ├── why_no_action = data_insufficient (required unavail)   [GAP] unit — classification table
  ├── why_no_action = stale_gated (hard_stale/fallback)      [GAP] unit
  └── why_no_action = real_no_action (all fresh, no action)  [GAP] unit
[+] diagnostics.ReasonCode (shared enum)
  └── unmatched reason → 'unknown', never free-form code      [GAP] unit
[+] new column + quality grading (Slice 3)
  ├── grade high_confidence / informational_only / no_action [GAP] unit
  └── data_sufficiency_by_source per source                  [GAP] unit
[+] Hermes export (Slice 4)
  └── optional fields present when set, omitted when None     [GAP] integration — test_hermes_*
[+] LEGACY report (null new fields) — REGRESSION  [CRITICAL]
  ├── detail endpoint renders                                [GAP] regression
  ├── serializer / Hermes export tolerate null               [GAP] regression
  └── frontend renders (PR-C)                                 [GAP] regression
[+] reason free-text sanitation
  └── account/credential-shaped token stripped + length cap   [GAP] unit (safety)

COVERAGE TARGET: 100% of new branches. Guards: no_internal_llm_imports +
import_contracts + side-effect guards stay green.
```

- **CRITICAL regression**: legacy pre-Phase-3 reports (null diagnostics) render
  on every surface (endpoint, serializer, Hermes export, frontend). IRON RULE —
  in the plan, no opt-out.
- Unit: reason_code derivation per collector; why_no_action 3-branch table;
  quality grading; enum fallback; reason sanitation.
- Integration: generate → (PR-B) persist → re-read → Hermes export carries fields.
- Frontend: badge/chip per grade + reason_code; null-safety.

## What already exists (reuse, do not rebuild)

- `snapshot_freshness_summary[kind]` JSONB (status/as_of/result_count) — Slice 1
  extends it; existing column, no migration.
- `generator_constraints.derive_generator_constraints` + `reason_ko` — Slice 2
  reuses; `why_no_action` wraps it, does not replace.
- `HermesContextPayload` re-exports freshness_summary — Slice 4 adds optional
  fields alongside.
- alembic pattern `20260519_rob269_p3_*` — Slice 3 migration mirrors it.
- `investment_stage_artifacts` (analysis-axis) + ROB-301 symbol-axis reports —
  Phase 3 does NOT touch either; report-level only.

## NOT in scope (explicitly deferred)

- **Phase 3b — candidate/held-signal evidence** (`new_candidate_reference_signals`,
  `held_strategy_change_signals`, `external_reference_checks`,
  `screener_condition_provenance`): touches candidate/journal/watch/screener/
  browser-reference collectors. Separate Linear issue. Rationale: bigger surface,
  different (symbol/candidate) axis; report-level diagnostics ship value first.
- **/invest/screener freshness semantics** → ROB-277/280/281.
- **Crypto Upbit/Naver market-context cards + column parity** → ROB-304/280.
- **ROB-301 symbol-intermediate-reports** → its own (already eng-reviewed) track.

## Failure modes (per new codepath)

| codepath | realistic failure | test? | error handling? | user sees |
|---|---|---|---|---|
| reason_code copy | collector emits unknown reason | unit | map → `unknown` | enum chip `unknown` (not crash) |
| why_no_action | bundle status combos not covered | unit table | default → most-degrading | correct classification |
| new column read | legacy report null diagnostics | **regression** | null-safe render | nothing / 확인 불가 |
| free-text reason | raw KIS error leaks account info | unit | sanitize + cap | bounded enum-led message |
| Hermes export | Hermes version lags new fields | integration | optional `\|None` | graceful degradation |

No critical gap (silent + untested + unhandled): all failure modes above have a
planned test AND deterministic handling.

## Parallelization

PR-A and PR-B share `generator.py` + `request.py` + the diagnostics module, and
PR-B depends on PR-A's `why_no_action` computation. **Mostly sequential**:
Lane A: PR-A → PR-B (shared generator/request). Lane B: PR-C frontend can start
against PR-A's response shape in parallel, but final wiring waits on PR-B's
persisted column. Net: A sequential; C can prototype in parallel, integrate after B.

## Implementation Tasks
Synthesized from this review. Checkbox as shipped.

- [ ] **T1 (P1, human: ~3h / CC: ~20min)** — snapshot_bundle — thread `reason_code` + sanitized `reason` into `freshness_summary[kind]` when degraded
  - Surfaced by: Architecture/Slice 1 — collector `errors_json.reason` dropped at `snapshot_bundle.py:181,229`
  - Files: app/services/action_report/common/snapshot_bundle.py, app/services/action_report/common/diagnostics.py (new), app/services/action_report/snapshot_backed/collectors/portfolio.py
  - Verify: `uv run pytest tests/services/action_report/`
- [ ] **T2 (P1, human: ~2h / CC: ~15min)** — generator — compute `why_no_action` (3-branch) onto `ReportGenerationResponse`
  - Surfaced by: Slice 2 / AC#5 — real vs stale_gated vs data_insufficient
  - Files: app/services/action_report/snapshot_backed/generator.py, request.py, generator_constraints.py
  - Verify: classification table unit test
- [ ] **T3 (P1, human: ~4h / CC: ~30min)** — model+migration — new `snapshot_report_diagnostics` JSONB; persist quality_summary + data_sufficiency + why_no_action
  - Surfaced by: D1 — first-class column
  - Files: app/models/investment_reports.py, app/schemas/investment_reports.py, alembic/versions/<new>.py
  - Verify: migration up/down + persist round-trip test
- [ ] **T4 (P1, human: ~2h / CC: ~15min)** — hermes — add 3 optional fields to `HermesContextPayload`
  - Surfaced by: D5 — push-only optional fields
  - Files: app/services/investment_stages/hermes_context.py, app/schemas/hermes_composition.py
  - Verify: `tests/test_hermes_*`
- [ ] **T5 (P1, human: ~2h / CC: ~15min)** — tests — CRITICAL legacy null-diagnostics regression across endpoint/serializer/export/frontend
  - Surfaced by: Test review IRON RULE + design-doc "legacy 깨지 않기"
  - Files: tests/test_investment_reports_mcp.py, tests/routers/..., frontend tests
  - Verify: load pre-Phase-3 report fixture, assert graceful render
- [ ] **T6 (P2, human: ~4h / CC: ~30min)** — frontend — quality badge + data-sufficiency chips + null-safety (PR-C)
  - Surfaced by: Slice 5 — AC render
  - Files: frontend/invest/src/...
  - Verify: component tests per grade/reason_code

## Locked decisions (eng-review 2026-05-26)

- **D1 persistence — new JSONB column.** Add one nullable JSONB column
  `snapshot_report_diagnostics` on `investment_reports` (alembic mirrors
  `20260519_rob269_p3_add_snapshot_metadata_to_investment_reports.py`). It holds
  `report_quality_summary` + `data_sufficiency_by_source` + `why_no_action`
  together. First-class column → queryable ("show blocked/low-quality reports"),
  decoupled from per-kind `snapshot_freshness_summary`. Operator runs
  `alembic upgrade head` (cutover gate).
- **D3 PR slicing — A → B → C.**
  - **PR-A (no migration)**: Slice 1 (`reason_code` in `freshness_summary[kind]`,
    persisted in the EXISTING `snapshot_freshness_summary` column) + Slice 2
    (`why_no_action` as a computed field on `ReportGenerationResponse`, **not**
    persisted yet — see reconciliation below).
  - **PR-B (migration + Hermes)**: Slice 3 (new `snapshot_report_diagnostics`
    column + `report_quality_summary` + `data_sufficiency_by_source`, and
    **persist** `why_no_action` into that column) + Slice 4 (Hermes export).
  - **PR-C**: Slice 5 frontend.
- **D4 scope — report-level only; coordinate with ROB-301.** Phase 3 stays
  report-level. `data_sufficiency_by_source` is the canonical "structured
  unavailable" feed the ROB-301 design doc requires. `why_no_action`
  (report-level no-action classification) references but does not duplicate
  ROB-301 `decision_bucket` (per-item 5-value). No fold-in.
- **D5 Hermes contract — optional fields.** All three new fields added to
  `HermesContextPayload` as `... | None = None`; graceful degradation if Hermes
  lags. Push-only contract preserved ([[hermes-push-only-not-pull]]).

### D1↔D3 reconciliation (why_no_action lifecycle)
`why_no_action` needs the new column (D1) but PR-A is no-migration (D3). Resolved
incrementally: **PR-A computes `why_no_action` and returns it on
`ReportGenerationResponse`** (ephemeral, available to the immediate
Hermes/operator caller, no DB write). **PR-B persists it** into
`snapshot_report_diagnostics` and the report-detail endpoint reads it from there.
Reversibility-friendly: PR-A delivers the signal in the API response before it
becomes durable.

## Eng-review refinements (mechanical, batched)

- **`reason_code` is a shared closed enum.** Define once in
  `app/services/action_report/common/diagnostics.py` (e.g. `ReasonCode` Literal/
  Enum) so collector, `generator_constraints`, Hermes export, and the frontend
  switch on the same values. DRY — no per-collector string duplication.
  Start set: `user_id_missing`, `kis_fetch_failed`, `stale`, `unavailable`,
  `failed`; unknown → `unknown`.
- **Sanitize the free-form `reason`.** The collector's `errors_json.reason` is
  free text (e.g. a raw KIS error) and is operator-visible. Surface the
  `reason_code` (closed enum, safe) as the primary signal; bound/sanitize the
  free `reason` string (length cap, strip account/credential-shaped tokens)
  before it reaches the report/UI. Never infer a code from unmatched text.
- **CRITICAL regression (IRON RULE, no opt-out).** Legacy reports generated
  before Phase 3 have `null` new fields. The report-detail endpoint, serializer,
  Hermes export, and frontend must render them without error. Add a regression
  test per surface that loads a pre-Phase-3 report (null diagnostics) and asserts
  graceful handling. Mandated by the design-doc constraint "legacy 리포트 깨지
  않기."
- **Frontend null-safety.** PR-C must treat all three fields as optional; absent
  → render nothing / "확인 불가", never a crash or an inferred value.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 4 decisions locked (D1/D3/D4/D5), 1 critical regression mandated, 0 unresolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **Scope:** accepted with report-level boundary (Phase 3b deferred to a separate issue).
- **Decisions locked:** D1 new `snapshot_report_diagnostics` JSONB column; D3 PR-A(no-migration)→PR-B(migration+Hermes)→PR-C(frontend); D4 report-level only, coordinate with ROB-301; D5 optional `|None` Hermes fields.
- **Critical gap flagged:** 1 — legacy null-diagnostics regression (IRON RULE, mandated in plan, no opt-out).
- **Failure modes:** 5 codepaths, all with planned test + deterministic handling; no silent+untested+unhandled gap.
- **Outside voice:** skipped (additive plan, well-scoped; available via `/codex` if desired).
- **Parallelization:** Lane A sequential (PR-A→PR-B, shared generator); PR-C frontend can prototype in parallel, integrate after PR-B.
- **UNRESOLVED:** 0.
- **VERDICT:** ENG CLEARED — ready to implement PR-A. CEO/Design reviews not required (backend diagnostics; PR-C frontend is render-only of deterministic fields).
