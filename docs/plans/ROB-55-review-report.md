# ROB-55 Review Report — Preopen news readiness + latest news preview

**Reviewer:** Claude Opus
**Implementer:** Claude Sonnet
**Branch:** `feature/ROB-55-preopen-news-readiness`
**Plan:** `docs/plans/ROB-55-preopen-news-readiness-plan.md`
**Verdict:** **PASS — ready for PR.** No must-fix issues.

---

## Verification evidence (provided)

| Check | Result |
|---|---|
| `uv run pytest tests/test_llm_news_preview.py tests/test_preopen_dashboard_service.py tests/test_router_preopen.py -q` | 19 passed (6 pre-existing warnings) |
| `npm test -- --run src/__tests__/PreopenPage.test.tsx` | 9 passed |
| `npm run typecheck` | passed |
| `uv run ruff check ...` | passed |
| `uv run ruff format --check ...` | passed |
| `git diff --check` | passed |

---

## Diff inventory

| Area | File | Change |
|---|---|---|
| Backend schema | `app/schemas/preopen.py` | +`NewsReadinessStatus`, `NewsArticlePreview`, `NewsReadinessSummary`; +`news`, `news_preview` on `PreopenLatestResponse` |
| Backend service | `app/services/llm_news_service.py` | +`get_latest_news_preview()` helper |
| Backend service | `app/services/preopen_dashboard_service.py` | `_merge_news_readiness` replaced by `_build_news_section`; `_derive_news_status` added; `_FAIL_OPEN` extended |
| Backend tests | `tests/test_llm_news_preview.py` | new file, 2 tests |
| Backend tests | `tests/test_preopen_dashboard_service.py` | +4 news-summary tests |
| Backend tests | `tests/test_router_preopen.py` | +2 router-level shape tests |
| Frontend types | `frontend/trading-decision/src/api/types.ts` | +`PreopenNewsReadinessStatus`, `PreopenNewsReadinessSummary`, `PreopenNewsArticlePreview`; extend `PreopenLatestResponse` |
| Frontend components | `ReadinessStatusBadge.tsx` + css | new |
| Frontend components | `NewsReadinessSection.tsx` + css | new |
| Frontend page | `pages/PreopenPage.tsx` | renders `NewsReadinessSection` between source warnings and candidates |
| Frontend tests | `__tests__/PreopenPage.test.tsx` | +4 news-state render tests |
| Frontend fixtures | `test/fixtures/preopen.ts` | +ready/stale/unavailable/article builders; existing `makePreopenResponse` / `makePreopenFailOpen` extended |
| Plan | `docs/plans/ROB-55-preopen-news-readiness-plan.md` | added (planner artifact) |

`app/routers/preopen.py` — unchanged. The route response model is `PreopenLatestResponse`, and extending the schema is enough.

---

## Backend audit

### Schema (`app/schemas/preopen.py`)

- New optional `news: NewsReadinessSummary | None = None` and `news_preview: list[NewsArticlePreview] = []` are appended; **all existing fields are preserved with the same types** (verified vs. `git diff`). `PreopenLatestResponse` consumers that ignore the new keys keep working.
- `NewsReadinessSummary.latest_run_uuid` is typed `str | None`. `_news_readiness_payload()` (`app/services/llm_news_service.py:228`) returns `latest_run.run_uuid`, which is a `UUID`; the dashboard service stringifies it (`str(readiness.latest_run_uuid)` at `preopen_dashboard_service.py:223`). Pydantic accepts the str. ✓
- Datetimes are tz-naive KST per `_news_readiness_payload._news_readiness_payload` calling `to_kst_naive()`. The schema's `datetime | None` accepts naive datetimes; the plan explicitly flagged this and asked not to re-attach UTC. ✓

### `get_latest_news_preview` (`app/services/llm_news_service.py:356`)

- Read-only. Single `select(NewsArticle)` ordered by `article_published_at DESC NULLS LAST`, capped by `limit` (default 5). No LLM, no summarization — `summary` is passed through from the column.
- Empty / non-positive `limit` short-circuits to `[]` without hitting the DB (verified by `tests/test_llm_news_preview.py::test_get_latest_news_preview_empty_when_no_feed_sources`).
- Local import of `NewsArticlePreview` from `app.schemas.preopen` avoids a possible top-level cycle. Functional, but the return type is annotated as bare `list:` rather than `list[NewsArticlePreview]`; this is a minor typing weakness only — call sites (`preopen_dashboard_service._build_news_section`) and the test both verify the element type. **Not a blocker.**

### Dashboard service (`app/services/preopen_dashboard_service.py`)

- **Status derivation matches existing readiness semantics:** `_derive_news_status()` at `:161-169` follows the plan rules — `unavailable` if `news_unavailable` warning present or `latest_run_uuid is None`; `stale` if `is_stale` or `news_stale` warning; `ready` if `is_ready`; defensive `stale` fallback. No silent path. The source of truth remains `_news_readiness_payload()`.
- **Back-compat preserved:** `_build_news_section()` still populates the legacy dict-form `source_freshness["news"]` (`:198-213`) and merges `readiness.warnings` into `source_warnings` (`:214-217`) before constructing the typed summary. The bulk-ingest stale-warning integration test path (`tests/test_news_ingestor_bulk.py::test_preopen_dashboard_adds_news_stale_warning`) reads `source_freshness["news"]` and `source_warnings`; both are intact.
- **Degraded path is explicit:** when `get_news_readiness()` raises, the service returns `news=None`, `news_preview=[]`, and adds `news_readiness_unavailable` to `source_warnings` (verified by `test_news_summary_none_when_readiness_lookup_raises`). The deduplication guard (`if "news_readiness_unavailable" not in merged_warnings`) is present.
- **Preview failure is also fail-open:** the inner `try`/`except` around `get_latest_news_preview` (`:235-245`) returns `[]` on any DB hiccup. Reasonable given the page-level criticality of preopen.
- **`_FAIL_OPEN` extended:** `news=None`, `news_preview=[]` (`:53-54`). When no run exists, the new fields are still well-defined.

### Forbidden imports invariant

`tests/test_preopen_dashboard_service.py::test_no_forbidden_imports` is intact. The new imports in `preopen_dashboard_service.py` are:

- `from app.schemas.preopen import (..., NewsArticlePreview, NewsReadinessSummary, ...)` — schema only.
- `from app.services.llm_news_service import (get_latest_news_preview, get_news_readiness,)` — same module already permitted.

No new module name contains `kis`, `upbit`, `broker`, `order_service`, `order_tool`, `trading_service`, `watch`, `alert`, `intent`, `credential`, or `token_manager`. ✓

### Router (`app/routers/preopen.py`)

- Untouched (verified — empty diff).
- Auth dependency (`get_current_user`) and `Literal["kr"]` market_scope validation unchanged. The plan's auth + 422 invariants still hold.

### Backend tests

- `test_news_summary_ready_and_preview_attached` — exercises the `ready` branch, asserts `source_counts` flow-through and 1-element preview.
- `test_news_summary_stale_status_when_warning_present` — `is_stale=True` + `news_stale` warning ⇒ `status == "stale"` and warning preserved.
- `test_news_summary_unavailable_when_no_run` — `latest_run_uuid=None` + `news_unavailable` ⇒ `status == "unavailable"`, preview empty.
- `test_news_summary_none_when_readiness_lookup_raises` — degraded fallback: `news=None`, `news_preview=[]`, `news_readiness_unavailable` present.
- Router-level: `test_get_latest_preopen_returns_news_section` (positive shape), `test_get_latest_preopen_news_null_when_readiness_unavailable` (degraded shape).

The plan's "must-pass" matrix (ready / stale / unavailable / degraded / shape) is fully covered. ✓

---

## Frontend audit

### Types (`frontend/trading-decision/src/api/types.ts`)

- New types match the backend schema 1:1 (`status`, `is_ready`, `is_stale`, `latest_run_uuid: string | null`, `latest_status: string | null`, `latest_finished_at: IsoDateTime | null`, etc.). Extends `PreopenLatestResponse` without altering existing fields.

### `ReadinessStatusBadge`

- Single-purpose `<span role="status" data-status={status}>` keyed off CSS Modules. Mirrors existing `StatusBadge` style. Aria-friendly via `role="status"` and visible label text.

### `NewsReadinessSection`

- Three rendering paths: `null` (degraded), `ready`, and `stale`/`unavailable` (with explicit warning line). Stale and unavailable are **never silently hidden** — both render visible warning copy. ✓
- External article links use `rel="noreferrer noopener" target="_blank"` — safe.
- Empty source counts and empty preview each fall back to `"No source counts available."` / `"No recent articles to preview."` rather than rendering nothing. ✓

### `PreopenPage.tsx`

- Inserts `<NewsReadinessSection news={data.news} preview={data.news_preview} />` after the source-warnings block and before the candidates table — matches plan placement.
- **Existing CTA / linked-session flow is byte-for-byte intact:** the `Open session` link, two-click `Create decision session` confirm, `Confirm` / `Cancel` controls, `creating`/`confirmPending` state, and 401 redirect all read identically before vs. after this branch.
- Fail-open branch (no run) does **not** render the section — it returns the `No preopen research run available` banner only. Matches plan.

### Frontend tests

- `renders Ready badge with source counts and a news preview link` — asserts `Ready` label, source-count chip text, and external link href.
- `renders Stale badge with explicit warning text` — asserts `Stale` label and the visible "News is older than 180 min …" copy.
- `renders Unavailable badge when news section reports no data` — asserts `Unavailable` label and "No recent articles to preview" copy.
- `renders Unavailable badge with degraded message when news is null` — asserts `Unavailable` label and "News readiness lookup failed" copy.

Plus the existing CTA / fail-open / 401 / 422 tests pass unmodified — `npm test` reports 9/9.

---

## Safety constraints

| Constraint | Status |
|---|---|
| No orders (real / paper / dry-run) placed | ✓ |
| No watch / alert / intent registration | ✓ |
| No Decision Session auto-create | ✓ (CTA still requires two clicks) |
| No scheduler / Prefect changes | ✓ |
| No `push-pending --execute` | ✓ |
| No NewsSignal extraction | ✓ |
| No LLM / Hermes summarization | ✓ (`summary` is the existing column passthrough) |
| No new credential / token reads | ✓ |
| No `app/routers/preopen.py` route signature change | ✓ |
| Forbidden-imports AST test still passes | ✓ (verified) |
| Auth invariant: 401 for unauthenticated | ✓ (router unchanged) |
| `market_scope` validation unchanged (rejects `us`) | ✓ (router unchanged) |
| Back-compat: `source_freshness["news"]` and `news_*` warnings preserved | ✓ |

---

## Findings

### Blocking

None.

### Non-blocking observations (FYI only — no fix required for this PR)

1. **`get_latest_news_preview` return type is bare `list:`** rather than `list[NewsArticlePreview]`. The implementer chose a function-local import to avoid coupling `llm_news_service` to `app.schemas.preopen` at module load. The behavior is correct and the test asserts element type via `isinstance`. A future tweak could promote the import (no cycle exists today) and tighten the annotation.
2. **`_news_readiness_payload()` produces tz-naive KST datetimes,** which Pydantic v2 will serialize as naive ISO strings. The frontend `formatDateTime` already accepts that shape and the plan called this out. Worth keeping in mind if a downstream consumer ever needs UTC.
3. **No tiebreaker in the article ordering** (`order_by(article_published_at desc nulls_last)`). For a 5-row preview this is acceptable; if future product tweaks add bigger paging, add `id DESC` as a stable secondary key.
4. **Plan compliance with `tests/test_news_ingestor_bulk.py` was not part of the supplied verification batch.** The code-level review confirms back-compat is preserved (`source_freshness["news"]` shape and `news_*` warning merging are byte-equivalent), so the existing `test_preopen_dashboard_adds_news_stale_warning` should still pass. Optional: re-run that file before merging for completeness.
5. **CSS hex literals are inlined** rather than referenced from a token file. The plan accepted this ("reuse existing palette") and the rest of `frontend/trading-decision/src/components/*.module.css` follows the same idiom.

---

## Acceptance-criteria check (from the plan)

| Criterion | Result |
|---|---|
| `news` (object or null) and `news_preview` (array, ≤5) on the response | ✓ |
| Fresh ⇒ `news.status == "ready"`, `warnings == []`, preview ≤5 by `published_at DESC` | ✓ |
| `is_stale` / `news_stale` ⇒ `news.status == "stale"`, `news_stale` still in `source_warnings` | ✓ |
| No run / `news_unavailable` ⇒ `news.status == "unavailable"`, preview `[]` | ✓ |
| Readiness raises ⇒ `news=null`, `news_preview=[]`, `news_readiness_unavailable` in `source_warnings` | ✓ |
| Forbidden-imports test passes | ✓ |
| Auth 401 unchanged; `market_scope=us` ⇒ 422 | ✓ (router untouched) |
| Preopen page renders the section for all states; CTA flow unchanged | ✓ |

---

## Recommendation

**Create the PR against `main`.** Implementation matches the plan, all listed verifications are green, tests cover the four readiness states plus the router shape, and the safety invariants (forbidden imports, no orders/intent/scheduler, no LLM, no auth/route changes) hold.

PR title (suggested):
`feat: surface news readiness + latest news preview on preopen page (ROB-55)`

PR body should reuse the plan's "Out of scope" list verbatim and reference this report.
