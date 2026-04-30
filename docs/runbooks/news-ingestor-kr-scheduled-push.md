# Runbook: News Ingestor KR Scheduled Push

## Purpose

Keep `/trading/decisions/preopen` `news` readiness fresh by running KR News Ingestor crawl + bulk push on a schedule, with bounded blast radius and a clear rollback.

## Current Deployment Status (as of 2026-04-30)

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

Both deployments **must remain paused** until the unpause checklist (§Unpause Checklist) is satisfied.

## Readiness Contract auto_trader Exposes

`get_news_readiness(market='kr', max_age_minutes=180)` returns "ready" only when **all** of:

- The latest `NewsIngestionRun` for `market='kr'` has `status ∈ {success, partial}`.
- That run has `finished_at` set and `now - finished_at ≤ max_age_minutes` (default **180 min**).
- That run's `source_counts` is non-empty.

Warnings emitted otherwise: `news_unavailable`, `news_run_unfinished`, `news_sources_empty`, `news_stale`. `/trading/decisions/preopen` surfaces these in `source_warnings` and demotes only the `news` slot of `source_freshness`; it is fail-open on readiness lookup errors.

## Proposed Safe Schedule (DEFINITION ONLY — DO NOT ACTIVATE IN THIS PR)

Track in `robin-prefect-automations` (OOR-1, OOR-2):

| Deployment | Cadence | Params | Freshness budget |
|---|---|---|---|
| `news-ingestor-kr-core/hourly` | `interval=3600s` (existing) | (defaults) | Crawl populates pending pool; budget consumed by push step. |
| `news-ingestor-pending-push/scheduled` | `interval=1800s` (every 30 min) | `{ limit: 25, execute: false }` first, then **`execute: true`** only after dry-run window passes | 30 min cadence with 25-row cap keeps `now - finished_at ≪ 180 min` even on one missed tick. |

Rationale:
- 30 min push cadence × `limit=25` → with 180 min readiness window we tolerate **5 missed ticks** before stale.
- `limit=25` caps DB write fan-out per run; combined with URL-conflict upsert in `ingest_news_ingestor_bulk`, repeated runs are idempotent.
- Keeping `pending-push` on its own deployment preserves the `execute-explicit` tag contract — production execution requires an explicit param flip, not a code path.

## Dry-Run Preview (operator workflow, not in this repo's CI)

From an operator host with Prefect CLI access (NOT from this repo's CI):

```text
# 1) Inspect deployment without mutating
prefect deployment inspect 'news-ingestor-pending-push/manual'

# 2) Run pending-push in DRY-RUN (execute=false). This will NOT call /llm/news/ingestor/bulk.
prefect deployment run 'news-ingestor-pending-push/manual' \
  --param limit=25 --param execute=false

# 3) Confirm the flow logged: "dry-run: would push N articles" and exited 0.
```

`execute=false` MUST be the default until the unpause checklist is signed off. Any `execute=true` invocation is treated as a production push.

## Failure Alert

Owned in `robin-prefect-automations` (OOR-1). Required signals:

- Flow run `Failed` or `Crashed` on `news-ingestor-pending-push/scheduled` → Telegram alert via existing `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_IDS_STR` channel.
- Two consecutive `kr-core/hourly` failures → same channel, separate message tag (`[kr-core]`).
- Auto_trader `/trading/decisions/preopen` returning `news_stale` for ≥ 2 consecutive smoke runs → human escalation (handled by smoke cron in `robin-prefect-automations`, **not** by `scripts/smoke/preopen_news_readiness.sh` which is on-demand only).

The smoke script is operator-invoked; no scheduled alerter ships in auto_trader.

## Unpause Checklist

Must all be ✅ before flipping `paused=false` in robin-prefect-automations:

1. ✅ `tests/test_news_readiness_contract.py` and `tests/test_news_ingestor_bulk.py` green on `main`.
2. ✅ `scripts/smoke/preopen_news_readiness.sh` against `current` returns HTTP 200 with `news` readiness payload present (ready or warned, but not 5xx).
3. ✅ Three consecutive `pending-push` dry-runs (`execute=false`) in Prefect succeed and log non-empty pending counts.
4. ✅ Telegram failure alert hook verified by deliberately failing one dry-run.
5. ✅ `news` readiness `max_age_minutes=180` confirmed in `app/services/llm_news_service.py` (no drift).
6. ✅ Operator on call acknowledged the rollback steps below.

## Rollback / Disable

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

## Smoke Script

Run `scripts/smoke/preopen_news_readiness.sh` to check the news readiness slice of the preopen endpoint. This script is read-only and operator-invoked only — it does not mutate state or call any ingest endpoint.

Acceptance criteria:
- After an unrelated deploy: `READY` or `WARN: news_stale` (depending on push deployment state) — both acceptable, neither indicates regression.
- `ERROR` (non-200, JSON parse fail) is a release blocker.

## Out-of-Repo Follow-Ups

- **OOR-1 (robin-prefect-automations):** unpause `kr-core` hourly, register `pending-push` schedule per this runbook's proposed schedule, wire failure alert hook.
- **OOR-2 (robin-prefect-automations):** parameterize `pending-push` `limit` and freshness via deployment params, default `execute=false`.
