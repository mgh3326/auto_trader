# Research Reports Integration (ROB-140)

> Thin ingest/read-layer slice for `research-reports.v1` payloads from news-ingestor.
> No broker mutation. No full PDF/report bodies stored or returned.

## What this is

Auto_trader pulls **compact metadata** for broker research reports (Naver Research,
KIS Research, etc.) from news-ingestor's `research-reports.v1` payload and exposes
them as **citations** for Research Session evidence.

## Tables

* `research_reports` — one row per report, idempotent by `dedup_key`.
* `research_report_ingestion_runs` — one row per upstream run, idempotent by
  `run_uuid`. Audit only.

All writes go through `ResearchReportsRepository`. No direct SQL writes.

## Boundary policy

* Auto_trader runtime does **not** call news-ingestor internals.
* Auto_trader receives a **payload file** (or in-band ingest endpoint, future) and
  validates it against `ResearchReportIngestionRequest` (Pydantic v2).
* Payloads with `attribution.full_text_exported=true` or
  `attribution.pdf_body_exported=true` are **rejected** at the schema layer.
* `summary_text` is truncated to 1000 chars; `detail.excerpt` to 500 chars.

## Operator CLI

```bash
# Validate without writing
uv run python -m scripts.ingest_research_reports \
  --file path/to/payload.json --dry-run

# Ingest
uv run python -m scripts.ingest_research_reports --file path/to/payload.json
```

Output is a JSON summary with `inserted_count` and `skipped_count`.

## Read API

```
GET /trading/api/research-reports/recent
  ?symbol=AAPL&source=naver_research&since=2026-04-01T00:00:00Z&limit=20
```

Response is `ResearchReportCitationListResponse` — citations only, never full body.

### Sample citation payload

```json
{
  "count": 1,
  "citations": [
    {
      "source": "naver_research",
      "title": "Apple Q2 Outlook",
      "analyst": "김철수",
      "published_at_text": "2026-05-07 09:00",
      "published_at": "2026-05-07T00:00:00+00:00",
      "category": "기업분석",
      "detail_url": "https://finance.naver.com/research/company_read.naver?nid=abc123",
      "pdf_url": "https://example.com/report.pdf",
      "excerpt": "투자의견 매수, 목표가 220달러",
      "symbol_candidates": [
        {"symbol": "AAPL", "market": "us", "source": "ticker_match"}
      ],
      "attribution_publisher": "naver_research",
      "attribution_copyright_notice": "© Naver"
    }
  ]
}
```

## Tests

```bash
uv run pytest tests/test_research_reports_payload_schemas.py -v
uv run pytest tests/test_research_reports_repository.py -v -m integration
uv run pytest tests/test_research_reports_ingestion.py -v -m integration
uv run pytest tests/test_research_reports_query_service.py -v -m integration
uv run pytest tests/test_research_reports_router.py -v -m integration
uv run pytest tests/test_research_reports_copyright_guardrails.py -v
```

## Migration

```bash
uv run alembic upgrade head    # applies b1c2d3e4_add_research_reports_tables
uv run alembic downgrade -1    # to roll back
```

## Safety

* No broker / order / watch / scheduling side effects.
* No full PDF bytes or full extracted PDF text accepted, stored, or returned.
* Read layer never reads body-style columns (none exist on the model).
* Citation responses include `attribution_publisher` and `attribution_copyright_notice`
  so downstream consumers can render attribution.

## Future follow-ups (out of scope)

* HTTP ingest endpoint (currently file-based via CLI).
* Research Session integration: wire `ResearchReportsQueryService` into the
  Research Session evidence gather step.
* Symbol normalization with `symbol_universe` services.

## ROB-178 operations smoke

Manual, metadata-only smoke that exercises news-ingestor → auto_trader research_reports end-to-end. Read-only against production state. No scheduler, no broker, no PDF body, no full extracted text.

Prereqs:

* Two dedicated worktrees (one per repo) on branch `rob-178-research-reports-ingest`.
* A smoke-only PostgreSQL database (e.g. `auto_trader_rob178_smoke`).
* `scripts/rob178_smoke.py` defaults to no-write evidence mode; service write-path ingestion requires `--apply`.

Steps (from the auto_trader worktree):

`$NEWS_INGESTOR_WORKTREE` is the dedicated news-ingestor branch worktree, `$AUTO_TRADER_WORKTREE` is the dedicated auto_trader branch worktree, and `$OUT_DIR` is the auto_trader smoke scratch directory.

```bash
export NEWS_INGESTOR_WORKTREE=/path/to/news-ingestor/rob-178-research-reports-ingest
export AUTO_TRADER_WORKTREE=/path/to/auto_trader/rob-178-research-reports-ingest
export OUT_DIR="$AUTO_TRADER_WORKTREE/.smoke-out"
mkdir -p "$OUT_DIR"
cd "$AUTO_TRADER_WORKTREE"

# 1. Generate the live payload from the news-ingestor worktree.
( cd "$NEWS_INGESTOR_WORKTREE" && \
  uv run news-ingestor research-report kis-truefriend \
    --pages 1 --rows-per-page 10 --include-detail --export-payload \
    --output "$OUT_DIR/payload_live.json" )

# 2. Apply migrations.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run alembic upgrade head

# 3. Operator CLI dry-run.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run python -m scripts.ingest_research_reports \
    --file .smoke-out/payload_live.json --dry-run

# 4. Optional all-in-one dry-run evidence, with no service write-path ingestion.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run python scripts/rob178_smoke.py \
    --dry-run \
    --payload .smoke-out/payload_live.json \
    --evidence .smoke-out/evidence-dry-run.json

# 5. Live ingest + idempotent re-ingest + read-back + guardrails.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run python scripts/rob178_smoke.py \
    --apply \
    --payload .smoke-out/payload_live.json \
    --evidence .smoke-out/evidence.json
```

Pass criteria:

* `evidence.json -> first_ingest.inserted_count` equals payload `report_count` and `skipped_count == 0`.
* `evidence.json -> second_ingest_idempotent.inserted_count == 0` and `skipped_count == payload report_count` (idempotent).
* `evidence.json -> read_back_via_service.citations_sample[*]` excerpts are ≤ 500 chars and contain none of `pdf_body|pdf_text|extracted_text|full_text|article_content|article_body|raw_payload|raw_payload_json`.
* `evidence.json -> guardrails.full_text_exported_rejected.rejected == true` and `guardrails.forbidden_body_field_rejected.rejected == true`.

Out of scope: `--store` against news-ingestor, `--download-pdf`, `--extract-text`, Prefect deployment activation, broker/order/watch mutation, HTTP ingest endpoint.

---

## ROB-179: /invest/api/feed/research (user-facing feed)

### Endpoint

```
GET /invest/api/feed/research
```

**Auth:** Bearer token via `get_authenticated_user` (same as all `/invest/api/*` endpoints).

### Query parameters

| Param | Type | Default | Notes |
|---|---|---|---|
| `tab` | `top\|latest\|mine\|watchlist\|holdings\|kr\|us` | `top` | Scope/sort tab |
| `limit` | int (1–100) | 30 | Page size |
| `cursor` | string | null | Opaque base64-JSON cursor for pagination |
| `source` | string | null | Exact match on `research_reports.source` |
| `symbol` | string | null | JSONB `@>` filter on `symbol_candidates` |
| `analyst` | string | null | ILIKE `%analyst%` filter |
| `category` | string | null | Exact category match |
| `query` | string | null | ILIKE across title, summary_text, detail_excerpt |
| `fromDate` | ISO date | null | Inclusive lower bound on `published_at` |
| `toDate` | ISO date | null | Inclusive upper bound on `published_at` |

### Response shape

See `docs/superpowers/specs/2026-05-11-rob-179-invest-research-api-design.md` §3 for full schema.

### Sample curl

```bash
curl -H "Authorization: Bearer ***" \
  "https://your-host/invest/api/feed/research?tab=latest&limit=5"
```

### Tests

```bash
uv run pytest tests/test_invest_feed_research_schemas.py -v
uv run pytest tests/test_invest_feed_cursor.py -v
uv run pytest tests/test_research_reports_query_service_feed_page.py -v -m integration
uv run pytest tests/test_invest_feed_research_service.py -v -m integration
uv run pytest tests/test_invest_api_feed_research_router.py -v -m integration
uv run pytest tests/test_invest_api_feed_research_copyright_guardrails.py -v -m integration
uv run pytest tests/test_invest_api_feed_research_router_safety.py -v -m unit
```

### Safety

* Read-only. No broker / order / watch / scheduler side effects.
* Response field allowlist asserted in `test_invest_api_feed_research_router.py::test_response_field_allowlist`.
* Copyright guardrail recursive scan in `test_invest_api_feed_research_copyright_guardrails.py::test_response_excludes_body_fields`.
* Production smoke acceptance is gated on ROB-178's ingest smoke evidence.

## ROB-207: Operator Scheduler / Bridge Activation

### HTTP Bulk Ingest Endpoint

The bridge endpoint accepts `POST /trading/api/research-reports/ingest/bulk` authenticated with a shared token:

```
POST /trading/api/research-reports/ingest/bulk
X-Research-Reports-Ingest-Token: <token>
Content-Type: application/json

<ResearchReportIngestionRequest JSON body>
```

- Token must be configured in production via `RESEARCH_REPORTS_INGEST_TOKEN`. If unset → 403, mirroring `NEWS_INGESTOR_INGEST_TOKEN`.
- Idempotent on `run_uuid` (re-POSTs return existing counts with `skipped_count = N`).
- Copyright guardrails enforced at schema layer: `full_text_exported=true` / `pdf_body_exported=true` payloads rejected with 422.

### Freshness Contract

`GET /trading/api/research-reports/freshness?source=&max_age_hours=24` (session auth required).

Returns `ResearchReportsReadinessResponse`:
- `is_ready`: true only if latest run finished within the budget
- `is_stale`: true if no finished run within `max_age_hours`
- `latest_run_uuid`, `latest_started_at`, `latest_finished_at`, `latest_inserted_count`, `latest_skipped_count`, `latest_report_count`
- `warnings`: list of warning codes

Warning vocabulary:
- `research_reports_unavailable` — no ingestion runs found for the source
- `research_reports_run_unfinished` — latest run has no `finished_at`
- `research_reports_stale` — `finished_at` older than `max_age_hours`

Default freshness budget: 24 h (configurable via `RESEARCH_REPORTS_FRESHNESS_MAX_AGE_HOURS`).

### Proposed Safe Schedule (DEFINITION ONLY — DO NOT ACTIVATE IN THIS PR)

Out-of-repo deployment: `robin-prefect-automations` → `research-reports-bridge/hourly`, `paused=true`.

Cadence: hourly POST of latest payload to `/trading/api/research-reports/ingest/bulk`. With 24 h budget this tolerates many missed ticks.

### Unpause Checklist (all must be ✅ before flipping `paused=false`)

1. ✅ `tests/test_research_reports_*` and `tests/test_middleware_auth_research_reports_ingest.py` green on `main`.
2. ✅ `RESEARCH_REPORTS_INGEST_TOKEN` set in production env (verify via 403 vs 401 probe — never log the token).
3. ✅ Three consecutive dry-run POSTs from the operator host return `inserted_count >= 0`, no 5xx, and `/freshness` reports `is_ready=false` only on `research_reports_unavailable`/`unfinished`, not on transport errors.
4. ✅ Diagnose CLI on `current` returns `is_ready` for at least one staged-source after a manual POST.
5. ✅ Operator on-call acknowledged the rollback steps below.

### Approval Packet Template

Fill this out before any production activation:

- **Source / scope:** (e.g., `naver_research`)
- **Endpoint / curl command (token redacted):**
  ```bash
  curl -X POST "$AUTO_TRADER_BASE/trading/api/research-reports/ingest/bulk" \
    -H "X-Research-Reports-Ingest-Token: ***" \
    -H "Content-Type: application/json" \
    -d @/path/to/payload.json | jq
  ```
- **Payload sha256 + report count** (from staged file header)
- **Dry-run smoke evidence:** 3 successful POSTs returning the same `run_uuid` counts
- **Expected DB delta:** `inserted_count`, `skipped_count`
- **Rollback statement:** "no destructive writes; POST is idempotent on `run_uuid`; freshness service is read-only"
- **Scheduler scope:** `paused=true → false` separate from any backfill
- **Post-run verification:**
  ```bash
  GET /trading/api/research-reports/freshness?source=<src>
  uv run python -m scripts.diagnose_research_reports --source <src>
  ```

### Rollback / Disable (order matters)

1. **Pause Prefect bridge:** `prefect deployment pause 'research-reports-bridge/hourly'`
2. **Rotate token:** set `RESEARCH_REPORTS_INGEST_TOKEN=""` → endpoint returns 403, no further inbound writes. Restart auto_trader app.
3. **Confirm read path intact:** `GET /trading/api/research-reports/recent` still serves prior citations — table is append-only.
4. **No DB cleanup needed:** `research_reports` is upsert-on-`dedup_key`; `research_report_ingestion_runs` is upsert-on-`run_uuid`. Do not delete rows.
5. **Re-enable:** configure token, smoke 3 dry-runs, then unpause Prefect.

### Smoke Commands

```bash
# Read-only diagnose (does not call the bridge)
uv run python -m scripts.diagnose_research_reports --max-age-hours 24
uv run python -m scripts.diagnose_research_reports --source naver_research

# CLI ingest, dry-run (file-based, ROB-140 path — still valid)
uv run python -m scripts.ingest_research_reports --file /path/to/payload.json --dry-run

# Bridge smoke (operator-only; token redacted; commit only after approval)
curl -X POST "$AUTO_TRADER_BASE/trading/api/research-reports/ingest/bulk" \
  -H "X-Research-Reports-Ingest-Token: ***" \
  -H "Content-Type: application/json" \
  -d @/path/to/payload.json | jq

# Freshness check (authenticated)
GET /trading/api/research-reports/freshness?source=naver_research&max_age_hours=24
```
