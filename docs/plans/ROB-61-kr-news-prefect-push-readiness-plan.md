# ROB-61 — KR News Prefect Scheduled Push & Readiness Operations

**Path:** `docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md`
**Issue:** ROB-61 — auto_trader: KR news Prefect scheduled push/readiness operations
**Branch:** `feature/ROB-61-kr-news-prefect-push-readiness`
**Role:** planner (Opus)
**Date:** 2026-04-30

## 0. Context Recap

KR news ingestion now has two halves wired up:

- **News Ingestor side (out of repo):** `/Users/mgh3326/services/prefect/flows/auto_trader/news_ingestion.py` defines `news_ingestor_kr_core` (hourly KR feed crawl/store) and `news_ingestor_pending_push` (manual flush of pending articles into auto_trader).
- **auto_trader side (this repo):**
  - `app/services/llm_news_service.py::ingest_news_ingestor_bulk` persists `NewsArticle` rows via URL conflict and writes a `NewsIngestionRun` row (`run_uuid`, `market`, `feed_set`, `started_at`, `finished_at`, `status`, `source_counts`, `inserted_count`, `skipped_count`, `error_message`).
  - `app/services/llm_news_service.py::get_news_readiness(market='kr', max_age_minutes=180, db=None)` returns a `_news_readiness_payload(...)` summary used by `/trading/decisions/preopen`.
  - `app/services/preopen_dashboard_service.py` merges that readiness into `NewsReadinessSummary`, `source_freshness['news']`, and `source_warnings` and renders `news_preview`. It is fail-open on readiness/preview lookup.
  - Tests: `tests/test_news_ingestor_bulk.py`, `tests/test_preopen_dashboard_service.py` already cover schema, router, readiness, and preopen stale-warning paths.

**Reconfirmed Prefect deployment status (read-only, no mutation done):**

| Deployment | Schedule | Paused | Tags | Params | Status | Entrypoint |
|---|---|---|---|---|---|---|
| News Ingestor KR Core / hourly | `interval=3600s` (defined) | **true** | `news-ingestor`, `kr-core`, `no-push` | (defaults) | `NOT_READY` | `flows/auto_trader/news_ingestion.py:news_ingestor_kr_core` |
| News Ingestor Pending Push / manual | (none) | **true** | `news-ingestor`, `pending-push`, `manual`, `execute-explicit` | `{limit: 25, execute: false}` | `NOT_READY` | `flows/auto_trader/news_ingestion.py:news_ingestor_pending_push` |

Both deployments stay paused for this PR. The bulk ingest endpoint has the readiness contract that the push flow must keep healthy; this PR codifies the contract, the operational schedule, and the smoke procedure in auto_trader. The actual unpause/cron tuning lives in `robin-prefect-automations` and is tracked as an out-of-repo follow-up.

## 1. Scope Decision — Smallest Safe auto_trader PR Slice

**In scope (this PR, auto_trader):**

1. Operator runbook (Markdown) describing current Prefect deployment state, the proposed scheduled push contract (cadence, `limit`, freshness window, dry-run posture, failure alert, rollback), and the unpause checklist.
2. The plan document itself.
3. Read-only `/trading/decisions/preopen` news readiness smoke script and its docs section.
4. A small set of additive tests asserting the readiness/run-status contract that the Prefect side relies on (no behavior change, lock-in only).

**Out of scope (explicit non-goals):**

- Touching `/Users/mgh3326/services/prefect/...` flows or unpause/execute. Tracked as **OOR-1** below.
- ROB-62 (push automation tuning) and ROB-63 (any downstream advisory work).
- Any change to ingest, readiness, preopen, or trading code paths.
- Production DB writes, broker calls, watch alerts, order-intents, or any `--execute` path.
- Secret/connection-string output or storage.

**Out-of-repo follow-ups (linked, not implemented here):**

- **OOR-1 (robin-prefect-automations):** unpause `kr-core` hourly, register `pending-push` schedule per the contract in §3, wire failure alert hook.
- **OOR-2 (robin-prefect-automations):** parameterize `pending-push` `limit` and freshness via deployment params, default `execute=false`.

## 2. Proposed Files & Exact Changes

| File | Change | Notes |
|---|---|---|
| `docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md` | **add** | This document. |
| `docs/runbooks/news-ingestor-kr-scheduled-push.md` | **add** | Operator runbook (content in §3). |
| `scripts/smoke/preopen_news_readiness.sh` | **add** | Read-only `curl` smoke against `http://127.0.0.1:8000/trading/decisions/preopen` on the `current` symlinked deploy; prints the `news` readiness/`source_warnings` slice via `jq`. **No** `--execute`, **no** ingest invocation. |
| `tests/test_news_readiness_contract.py` | **add** | Pure-unit contract tests for `_news_readiness_payload` and `get_news_readiness` behavior the Prefect push flow depends on (status whitelist, `max_age_minutes` default, `source_counts` empty -> warning, latest-finished-at selection). Uses in-memory fakes; no DB or network. |
| `tests/test_preopen_dashboard_service.py` | **extend** (additive) | One new case asserting that `news_unavailable` does **not** demote non-news readiness signals (fail-open guard). Skip if equivalent already exists; do not modify existing tests. |
| `app/services/llm_news_service.py` | **no change** | Contract is stable; do not touch. |
| `app/services/preopen_dashboard_service.py` | **no change** | Same. |

**Not creating:** any module under `app/tasks/prefect*`, any new config flag, any scheduler shim. Prefect ownership stays in `robin-prefect-automations`.

## 3. Operator Runbook — `docs/runbooks/news-ingestor-kr-scheduled-push.md`

> Drop-in content. Implementer should write verbatim, adjusting only paths if the deploy symlink moves.

### 3.1 Purpose

Keep `/trading/decisions/preopen` `news` readiness fresh by running KR News Ingestor crawl + bulk push on a schedule, with bounded blast radius and a clear rollback.

### 3.2 Current Deployment Status (as of 2026-04-30)

```
News Ingestor KR Core / hourly
  schedule:   interval 3600s   (defined, INACTIVE)
  paused:     true
  tags:       news-ingestor, kr-core, no-push
  status:     NOT_READY
  entrypoint: flows/auto_trader/news_ingestion.py:news_ingestor_kr_core

News Ingestor Pending Push / manual
  schedule:   none
  paused:     true
  tags:       news-ingestor, pending-push, manual, execute-explicit
  params:     { limit: 25, execute: false }   # dry-run posture
  status:     NOT_READY
  entrypoint: flows/auto_trader/news_ingestion.py:news_ingestor_pending_push
```

Both deployments **must remain paused** until the unpause checklist (§3.7) is satisfied.

### 3.3 Readiness Contract auto_trader Exposes

`get_news_readiness(market='kr', max_age_minutes=180)` returns "ready" only when **all** of:

- The latest `NewsIngestionRun` for `market='kr'` has `status ∈ {success, partial}`.
- That run has `finished_at` set and `now - finished_at ≤ max_age_minutes` (default **180 min**).
- That run's `source_counts` is non-empty.

Warnings emitted otherwise: `news_unavailable`, `news_run_unfinished`, `news_sources_empty`, `news_stale`. `/trading/decisions/preopen` surfaces these in `source_warnings` and demotes only the `news` slot of `source_freshness`; it is fail-open on readiness lookup errors.

### 3.4 Proposed Safe Schedule (DEFINITION ONLY — DO NOT ACTIVATE IN THIS PR)

Track in `robin-prefect-automations` (OOR-1, OOR-2):

| Deployment | Cadence | Params | Freshness budget |
|---|---|---|---|
| `news-ingestor-kr-core/hourly` | `interval=3600s` (existing) | (defaults) | Crawl populates pending pool; budget consumed by push step. |
| `news-ingestor-pending-push/scheduled` | `interval=1800s` (every 30 min) | `{ limit: 25, execute: false }` first, then **`execute: true`** only after dry-run window passes | 30 min cadence with 25-row cap keeps `now - finished_at ≪ 180 min` even on one missed tick. |

Rationale:
- 30 min push cadence × `limit=25` → with 180 min readiness window we tolerate **5 missed ticks** before stale.
- `limit=25` caps DB write fan-out per run; combined with URL-conflict upsert in `ingest_news_ingestor_bulk`, repeated runs are idempotent.
- Keeping `pending-push` on its own deployment preserves the `execute-explicit` tag contract — production execution requires an explicit param flip, not a code path.

### 3.5 Dry-Run Preview (operator workflow, not in this PR's CI)

From an operator host with Prefect CLI access (NOT from this repo's CI):

```text
# 1) Inspect deployment without mutating
prefect deployment inspect 'news-ingestor-pending-push/manual'

# 2) Run pending-push in DRY-RUN (execute=false). This will NOT call /llm/news/ingestor/bulk.
prefect deployment run 'news-ingestor-pending-push/manual' \
  --param limit=25 --param execute=false

# 3) Confirm the flow logged: "dry-run: would push N articles" and exited 0.
```

`execute=false` MUST be the default until the unpause checklist (§3.7) is signed off. Any `execute=true` invocation is treated as a production push.

### 3.6 Failure Alert

Owned in `robin-prefect-automations` (OOR-1). Required signals:

- Flow run `Failed` or `Crashed` on `news-ingestor-pending-push/scheduled` → Telegram alert via existing `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_IDS_STR` channel.
- Two consecutive `kr-core/hourly` failures → same channel, separate message tag (`[kr-core]`).
- Auto_trader `/trading/decisions/preopen` returning `news_stale` for ≥ 2 consecutive smoke runs → human escalation (handled by smoke cron in `robin-prefect-automations`, **not** by this PR's smoke script which is on-demand only).

The smoke script in this PR is operator-invoked; no scheduled alerter ships in auto_trader.

### 3.7 Unpause Checklist (must all be ✅ before flipping `paused=false` in robin-prefect-automations)

1. ✅ `tests/test_news_readiness_contract.py` and `tests/test_news_ingestor_bulk.py` green on `main`.
2. ✅ `scripts/smoke/preopen_news_readiness.sh` against `current` returns HTTP 200 with `news` readiness payload present (ready or warned, but not 5xx).
3. ✅ Three consecutive `pending-push` dry-runs (`execute=false`) in Prefect succeed and log non-empty pending counts.
4. ✅ Telegram failure alert hook verified by deliberately failing one dry-run.
5. ✅ `news` readiness `max_age_minutes=180` confirmed in `app/services/llm_news_service.py` (no drift).
6. ✅ Operator on call acknowledged the rollback steps in §3.8.

### 3.8 Rollback / Disable

Order matters. Each step is reversible.

1. **Pause push** (highest priority, stops writes):
   ```text
   prefect deployment pause 'news-ingestor-pending-push/scheduled'
   ```
   The `scheduled` deployment slug applies after OOR-1 creates it; before that, pause the existing `News Ingestor Pending Push/manual` deployment if it has been unpaused for dry-run testing. Crawl can keep running; only push affects `NewsIngestionRun`.

2. **Pause crawl** (only if crawl itself misbehaving):
   ```text
   prefect deployment pause 'news-ingestor-kr-core/hourly'
   ```

3. **Confirm preopen still serves**: run `scripts/smoke/preopen_news_readiness.sh`. Expect `news_stale` warning to appear after 180 min — that is intended, not a regression. preopen remains fail-open and other source freshness signals are unaffected.

4. **No DB cleanup needed**: `ingest_news_ingestor_bulk` is idempotent on URL conflict and `NewsIngestionRun` rows are append-only. Do not delete rows.

5. **Re-enable** by unpausing in reverse order (crawl, then push) once the underlying issue is fixed and the unpause checklist is re-walked.

## 4. `/trading/decisions/preopen` News Readiness Smoke

`scripts/smoke/preopen_news_readiness.sh` — read-only, on-demand. Operator runs against the `current` symlinked deploy `/Users/mgh3326/services/auto_trader/current` (already running on `127.0.0.1:8000`).

Behavior:

- `curl -fsS http://127.0.0.1:8000/trading/decisions/preopen` (no auth headers logged, no body posted).
- Pipe through `jq '{news: .source_freshness.news, warnings: (.source_warnings // [] | map(select(startswith("news_"))))}'`.
- Exit 0 if HTTP 200 and JSON parses; non-zero otherwise. Print a short summary line: `READY` / `WARN: <warnings>` / `ERROR`.
- **Forbidden:** any POST, any reference to `/llm/news/ingestor/bulk`, any `execute=true`, any token output.

Smoke acceptance criteria:

- After an unrelated deploy: `READY` or `WARN: news_stale` (depending on push deployment state) — both acceptable, neither indicates regression.
- `ERROR` (non-200, JSON parse fail) is a release blocker.

## 5. Focused Tests & Quality Commands

### 5.1 New: `tests/test_news_readiness_contract.py`

Pure-unit, no DB, no network. Uses fakes for `NewsIngestionRun`-shaped objects. Asserts the contract `news-ingestor-pending-push` relies on:

- `status='success'` and fresh `finished_at` → ready.
- `status='partial'` and fresh `finished_at` → ready (partial is acceptable).
- `status='failed'` → `news_run_unfinished` warning, not ready.
- `finished_at = None` → `news_run_unfinished`, not ready.
- `now - finished_at > 180 min` (default) → `news_stale`.
- Empty `source_counts` (e.g., `{}`) → `news_sources_empty`, not ready.
- No runs at all → `news_unavailable`.
- `max_age_minutes` override is honored (e.g., 30 min still warns even if default 180 would pass).

**Style:** mirror existing `tests/test_news_ingestor_bulk.py` markers (`@pytest.mark.unit`).

### 5.2 Extend: `tests/test_preopen_dashboard_service.py`

One additive case: `test_news_unavailable_does_not_demote_other_freshness_signals` — fakes a `news_unavailable` readiness alongside healthy KIS/Upbit freshness; asserts only the `news` slot of `source_freshness` is demoted and other signals stay intact. Confirms fail-open semantics. Skip if an equivalent assertion already lives there; do **not** modify existing cases.

### 5.3 Quality commands (run by implementer)

```text
make lint
make typecheck
uv run pytest tests/test_news_readiness_contract.py -v
uv run pytest tests/test_news_ingestor_bulk.py -v
uv run pytest tests/test_preopen_dashboard_service.py -v
```

Full `make test` is recommended but not required if the three above + lint/typecheck are green and no other files were touched.

### 5.4 Forbidden in tests

- No real Prefect imports.
- No real DB connections.
- No `httpx` calls to `/llm/news/ingestor/bulk`.
- No environment variable reads beyond what the existing tests already mock.

## 6. Sonnet Implementer Prompt Section

> Paste this verbatim into the implementer kickoff.

You are Claude Sonnet acting as implementer for ROB-61. Plan path: `docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md`. Branch: `feature/ROB-61-kr-news-prefect-push-readiness`. Worktree: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-61-kr-news-prefect-push-readiness`.

Tasks, in order:

1. Create `docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md` with the plan content already produced.
2. Create `docs/runbooks/news-ingestor-kr-scheduled-push.md` from §3 verbatim.
3. Create `scripts/smoke/preopen_news_readiness.sh` per §4. `chmod +x` it. Shell script must be POSIX `bash`, set `-euo pipefail`, and depend only on `curl` + `jq`. Do NOT write any token, header, or body to stdout/stderr beyond the documented summary line.
4. Create `tests/test_news_readiness_contract.py` per §5.1, mirroring style of `tests/test_news_ingestor_bulk.py`. Use `@pytest.mark.unit`.
5. Add the single additive case in `tests/test_preopen_dashboard_service.py` per §5.2. If an equivalent assertion already exists, skip and note in PR description.
6. Run `make lint && make typecheck && uv run pytest tests/test_news_readiness_contract.py tests/test_news_ingestor_bulk.py tests/test_preopen_dashboard_service.py -v`. Fix only the new files until green.

Hard constraints (do not relax):

- Do **not** edit `app/services/llm_news_service.py`, `app/services/preopen_dashboard_service.py`, any router, or any model.
- Do **not** unpause Prefect deployments. Do **not** invoke `/llm/news/ingestor/bulk`. Do **not** call any broker, watch, or order code path.
- Do **not** write production DB.
- Do **not** print tokens, cookies, connection strings.
- Smoke script reads from `127.0.0.1:8000` only. Do not target a hostname or external URL.
- If a step requires touching `/Users/mgh3326/services/prefect`, **stop** and add it to PR description as OOR follow-up; do not modify out-of-repo files.

PR description must list: files added, tests added, quality commands run, the OOR-1/OOR-2 follow-up items, and the unpause checklist link.

Commit trailer: `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.

## 7. Risks & Non-Goals

### Risks (and mitigations)

| Risk | Mitigation |
|---|---|
| Prefect side activates schedule before contract tests merge → silent stale `news_stale` in preopen. | Unpause checklist §3.7 gates activation on contract tests + smoke. |
| `limit=25` plus 30 min cadence is too small under bursty news days → backlog grows. | Tracked as OOR-1; deployment params allow raising `limit` without code change. |
| Failure alert hook lives in `robin-prefect-automations`; if it regresses, auto_trader has no visibility. | Smoke script + preopen `source_warnings` give operators a manual fallback. |
| Test additions accidentally couple to real DB. | Plan mandates pure-unit fakes; review checklist enforces. |
| Out-of-repo (Prefect) drift from the documented contract. | Runbook lists exact `status`/`max_age_minutes`/`source_counts` semantics; contract tests pin them in this repo. |

### Non-Goals (explicit)

- ROB-62 push automation tuning beyond defining the safe schedule shape.
- ROB-63 advisory/decision integration of the news payload.
- Multi-market support (JP/US news scheduled push).
- Replacing or enhancing the existing readiness window (180 min stays as-is).
- Any change to the `execute-explicit` tagging convention.
- Dashboard or UI changes.

## 8. AOE Status

```
AOE_STATUS: plan_ready
AOE_ISSUE: ROB-61
AOE_ROLE: planner
AOE_PLAN_PATH: docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md
AOE_NEXT: start_implementer_same_session
```

---

AOE_STATUS: plan_ready
AOE_ISSUE: ROB-61
AOE_ROLE: planner
AOE_PLAN_PATH: docs/plans/ROB-61-kr-news-prefect-push-readiness-plan.md
AOE_NEXT: start_implementer_same_session
