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

Steps (from the auto_trader worktree):

```bash
# 1. Generate the live payload from the news-ingestor worktree.
( cd /Users/mgh3326/worktrees/news-ingestor/rob-178-research-reports-ingest && \
  uv run news-ingestor research-report kis-truefriend \
    --pages 1 --rows-per-page 10 --include-detail --export-payload \
    --output /Users/mgh3326/worktrees/auto_trader/rob-178-research-reports-ingest/.smoke-out/payload_live.json )

# 2. Apply migrations.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run alembic upgrade head

# 3. Operator CLI dry-run.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run python -m scripts.ingest_research_reports \
    --file .smoke-out/payload_live.json --dry-run

# 4. Live ingest + idempotent re-ingest + read-back + guardrails.
DATABASE_URL=postgresql+asyncpg://localhost/auto_trader_rob178_smoke \
  uv run python scripts/rob178_smoke.py \
    --payload .smoke-out/payload_live.json \
    --evidence .smoke-out/evidence.json
```

Pass criteria:

* `evidence.json -> first_ingest.inserted_count` equals payload `report_count` and `skipped_count == 0`.
* `evidence.json -> second_ingest_idempotent.inserted_count == 0` and `skipped_count == payload report_count` (idempotent).
* `evidence.json -> read_back_via_service.citations_sample[*]` excerpts are ≤ 500 chars and contain none of `pdf_body|pdf_text|extracted_text|full_text|article_content|article_body|raw_payload|raw_payload_json`.
* `evidence.json -> guardrails.full_text_exported_rejected.rejected == true` and `guardrails.forbidden_body_field_rejected.rejected == true`.

Out of scope: `--store` against news-ingestor, `--download-pdf`, `--extract-text`, Prefect deployment activation, broker/order/watch mutation, HTTP ingest endpoint.
