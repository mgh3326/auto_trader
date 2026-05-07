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
