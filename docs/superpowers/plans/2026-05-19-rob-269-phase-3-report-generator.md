# ROB-269 Phase 3 — Report Generator Integration + Lift (Short Plan)

> Phase 3 of ROB-269. Stacked draft on top of Phase 2 (PR #875). PR base: `rob-269-phase2-mcp-api`. Once PR 875 merges into `main`, this PR will rebase + retarget to `main`.
>
> **Branch:** `rob-269-phase3-report-generator` (off `origin/rob-269-phase2-mcp-api` head `242bc434`)
> **Worktree:** `/Users/mgh3326/work/auto_trader.rob-269-phase3-report-generator`
> **Goal:** Wire the Phase 2 snapshot foundation into investment-report creation. Add the 3-layer stale gate (DB CHECK + pre-gen constraints + post-gen linter). Lift `us_action_report/` into `action_report/us/` per pre-plan Decision 2 (clean-cut, no transitional re-exports). All gated by `ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED` (default off).

---

## 1. Migration / schema / model

**Migration** `alembic/versions/<rev>_add_snapshot_metadata_to_investment_reports_rob_269.py` — additive only, nullable:

```text
ALTER TABLE review.investment_reports ADD COLUMN snapshot_bundle_uuid UUID NULL;
ALTER TABLE review.investment_reports ADD COLUMN snapshot_policy_version TEXT NULL;
ALTER TABLE review.investment_reports ADD COLUMN snapshot_coverage_summary JSONB NULL;
ALTER TABLE review.investment_reports ADD COLUMN snapshot_freshness_summary JSONB NULL;
ALTER TABLE review.investment_reports ADD COLUMN source_conflicts JSONB NULL;
ALTER TABLE review.investment_reports ADD COLUMN unavailable_sources JSONB NULL;

CREATE INDEX ix_investment_reports_snapshot_bundle_uuid
  ON review.investment_reports (snapshot_bundle_uuid);

-- ★ Decision 4 layer (i) — DB-level guard
ALTER TABLE review.investment_reports
  ADD CONSTRAINT ck_investment_reports_no_published_on_hard_stale
  CHECK (
    status <> 'published'
    OR snapshot_freshness_summary IS NULL  -- legacy report compat
    OR (snapshot_freshness_summary->>'overall') IN ('fresh', 'soft_stale', 'partial')
  );
```

Existing reports stay readable (all new columns NULLABLE; CHECK has legacy clause).

**Model + schema** — add fields to `InvestmentReport`, `IngestReportRequest`. Repository `insert_report` accepts them.

## 2. Stale gate — 3 layers (Decision 4)

| Layer | Where | Catches |
|---|---|---|
| (i) DB CHECK | migration above | `published` row with `freshness_summary.overall ∈ {hard_stale, failed, unavailable}` |
| (ii) Generator gate | `action_report/common/snapshot_bundle.py::derive_generator_constraints(bundle) -> GeneratorConstraints` | Pre-LLM directive: "no executable action language" + forced action mode |
| (iii) Post-gen linter | `action_report/common/stale_gate.py::lint_action_language(text, bundle_status, freshness_summary, account_scope) -> StaleLintResult` | Deterministic regex against KR/EN action verbs |

Action verb sets (locked):
- KR forbidden: `매수`, `매도`, `사세요`, `파세요`, `추격`, `분할매수`, `분할매도`, `익절`, `손절`
- EN forbidden: `buy`, `sell`, `long`, `short`, `add`, `trim`, `stop`
- KR allowed: `관망`, `보유 유지`, `지켜보기`, `확인 불가`

**8 lint test cases** from pre-plan §4 are TDD-first.

## 3. `us_action_report/` → `action_report/us/` lift (clean-cut, Decision 2)

- Move 5 files + `__init__.py` from `app/services/us_action_report/` to `app/services/action_report/us/`.
- Update import sites (grep `from app.services.us_action_report` and `import app.services.us_action_report`).
- Move corresponding tests to `tests/services/action_report/us/`.
- Delete old `app/services/us_action_report/` directory in the same commit.
- **No transitional re-export shim** (memory: clean-cut preference, no transitional layers when legacy is out of future product).

## 4. Bundle-aware report creation

`IngestReportRequest` gains optional fields:

```python
snapshot_bundle_uuid: uuid.UUID | None = None
snapshot_policy_version: str | None = None
snapshot_coverage_summary: dict[str, Any] | None = None
snapshot_freshness_summary: dict[str, Any] | None = None
source_conflicts: dict[str, Any] | None = None
unavailable_sources: dict[str, Any] | None = None
```

Persisted by `repository.insert_report` and read back via `query_service`.

`SnapshotBundleEnsureService` + ingestion stay **separately composable** — callers can ensure-first then ingest, or pass an already-ensured bundle UUID. **No automatic ensure** inside ingestion (keeps the snapshot write boundary clean per Phase 2 plan §4).

Convenience helper in `action_report/common/`: `attach_bundle_to_ingest_request(bundle: BundleHeaderView, request: IngestReportRequest) -> IngestReportRequest` — pure transformation, no DB call.

## 5. Feature flag

`ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED: bool = False` in `app/core/config.py`.

When `False`:
- Existing report ingestion path is unchanged. Old-shape requests (no snapshot fields) still work.
- New snapshot-aware path is registered but treats stale_gate output as advisory only.

When `True`:
- Stale-gate enforcement at generator layer for any request with `account_scope == 'kis_live'` and `snapshot_bundle_uuid`.
- DB CHECK is always live (additive, not flag-gated) — once a row tries to violate, the INSERT fails regardless of flag. Flag only controls the **service-side enforcement** that prevents the bad row from being submitted.

## 6. Tests

**TDD-first:**
- `tests/services/action_report/common/test_stale_gate.py` — 8 cases from pre-plan §4 verbatim (fresh/complete OK → block on hard_stale + kis_live + 매수, allow 관망, etc.).
- `tests/services/action_report/common/test_generator_constraints.py` — `derive_generator_constraints(bundle)` returns expected `GeneratorConstraints` for each bundle status.

**Existing surface:**
- Bundle-aware fields round-trip through `IngestReportRequest` → `insert_report` → query.
- Legacy reports (no snapshot fields) still ingest and query cleanly.
- DB CHECK constraint test: forcing `status='published'` + `snapshot_freshness_summary.overall='hard_stale'` raises `IntegrityError`.

**Lift regression:**
- All existing `tests/services/test_us_action_report*` continue to pass after lift (the import paths inside the tests change but behaviour is identical).
- A focused "no lingering us_action_report imports" grep test ensures the clean-cut.

**Mutation boundary update:**
- `tests/services/investment_snapshots/test_mutation_boundary.py` scope list extended to include the new `stale_gate.py` + `action_report/us/*` + `action_report/kr/*` (if any added) files.
- No new broker/order mutation imports introduced.

## 7. Expected files

**Create (~12 new):**
- `alembic/versions/<rev>_add_snapshot_metadata_to_investment_reports_rob_269.py`
- `app/services/action_report/common/stale_gate.py`
- `app/services/action_report/common/generator_constraints.py` (or extend `snapshot_bundle.py`)
- `app/services/action_report/us/__init__.py` (lift target — same content as old `us_action_report/__init__.py`)
- `app/services/action_report/us/account_snapshot.py`
- `app/services/action_report/us/action_classifier.py`
- `app/services/action_report/us/new_buy_candidates.py`
- `app/services/action_report/us/order_preview.py`
- `app/services/action_report/us/discord_formatter.py`
- `tests/services/action_report/common/test_stale_gate.py`
- `tests/services/action_report/common/test_generator_constraints.py`
- `tests/services/action_report/us/__init__.py` + the lifted us-action-report tests

**Modify (~6):**
- `app/core/config.py` — add `ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED`
- `app/models/investment_reports.py` — add 6 new columns + CHECK
- `app/schemas/investment_reports.py` — add 6 new fields to `IngestReportRequest`
- `app/services/investment_reports/repository.py` — `insert_report` accepts new fields
- Import-site updates wherever `us_action_report` is referenced (routers / MCP tooling)
- `tests/services/investment_snapshots/test_mutation_boundary.py` scope list

**Delete:**
- `app/services/us_action_report/` (entire directory, lifted to `action_report/us/`)
- Corresponding old test paths (moved, not duplicated)

## Non-goals (explicit out-of-scope for Phase 3)

- ❌ `action_report/kr/` concrete generator — Phase 4 (this PR establishes the snapshot+report seam; the KR generator that consumes it can land separately)
- ❌ `/invest` frontend provenance UI — Phase 4
- ❌ Prefect/TaskIQ scheduler enablement — Phase 4
- ❌ Production deploy / unpause checklists
- ❌ Real broker / order / watch-intent mutation (still banned, guarded by extended `test_mutation_boundary.py`)
- ❌ Live KIS / Upbit / Alpaca HTTP collectors — Phase 4 (production collectors register into the Phase 2 registry)
- ❌ Existing report compat break — all changes are additive; old reports stay readable
- ❌ ROB-269 Linear issue split — single issue, four PRs
- ❌ Draft-flip on PR 875 or PR for this branch — owner area
- ❌ Feature flag flip on either PR — owner area

---

## Implementation order

1. `feat(rob-269-p3): ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED + snapshot metadata schema (additive, default off)` — config flag + migration + model + schema + repository all in one focused commit (additive)
2. `feat(rob-269-p3): stale_gate post-generation linter` — `stale_gate.py` + 8 TDD test cases
3. `feat(rob-269-p3): derive_generator_constraints (pre-LLM gate)` — module + tests
4. `feat(rob-269-p3): bundle metadata round-trip on ingestion` — extend ingestion service + tests
5. `feat(rob-269-p3): DB CHECK constraint smoke test` — integrity test ensuring the migration's CHECK rejects published+hard_stale
6. `feat(rob-269-p3): lift us_action_report → action_report/us (clean-cut)` — file moves + import-site updates + delete old dir + regression test
7. `test(rob-269-p3): mutation boundary scope updated` — new files in scope
8. `chore(rob-269-p3): ruff cleanup` if needed

Each step ends with a focused local commit. Final push is one `git push -u origin rob-269-phase3-report-generator`; PR with base `rob-269-phase2-mcp-api`, draft.

## Handoff format

- Branch: `rob-269-phase3-report-generator` (off `origin/rob-269-phase2-mcp-api`)
- Migration filename: noted in handoff
- Tests: pytest summary line (Phase 1+2+3 combined target ≥ 165 passing)
- Lint: clean or list of fixes
- Local commits made? Yes; push to origin done; PR created as draft against `rob-269-phase2-mcp-api`
- After PR 875 merges to main: rebase + retarget this branch to main (same procedure as PR 875)
- Open `# TODO(rob-269 reviewer):` notes: file + line if any
