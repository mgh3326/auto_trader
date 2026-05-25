# `/invest/reports` News Dimension Evidence — Slice 3

- **Date**: 2026-05-24
- **Status**: Design approved, pending spec review
- **Branch**: `rob-310`
- **Linear**: [ROB-310](https://linear.app/mgh3326/issue/ROB-310) (parent: ROB-306; loop closed by ROB-308)

## Context

Slice 3 of the TradingAgents-style `/invest/reports` program. The dimension machinery is **generic**: `investment_dimension_reports` (ROB-306) already supports `dimension="news"` (table + `POST /hermes/dimension-reports` ingest + read surface), and the Hermes context export already carries `dimension_reports` + `symbol_intermediate_reports` (ROB-308). So News needs **no new table, endpoint, migration, or schema** — only a deterministic News evidence assembler + context wiring, mirroring `market_evidence` (ROB-306).

`research_reports` (ROB-140/207) is currently **empty**: ingestion is approval-gated (`RESEARCH_REPORTS_INGEST_COMMIT_ENABLED=False`, the `research_reports.ingest_bulk_smoke` TaskIQ task is scheduleless). This mirrors crypto Market evidence (deferred to ROB-282) — build the path + fixture-test it; ingestion enablement is a separate operator gate.

## Decisions locked (with the user)

- **Market-wide** News evidence — one News report per run (`symbol`=NULL). research_reports rows are multi-symbol *mentions* (`symbol_candidates` JSONB), not symbol-scoped; per-symbol News is a future extension.
- **Assembler-only + fixture-tested.** research_reports ingestion enablement is deferred (operator gate, out of this slice).
- Deterministic evidence; Hermes authors the News report prose via the existing `/hermes/dimension-reports` (dimension="news"). **No in-process LLM** (ROB-287). Read-only; no broker mutation.

## Architecture / scope

### N1. `app/services/investment_dimensions/news_evidence.py` (deterministic)
Mirror `market_evidence.build_market_evidence`:

```python
async def build_news_evidence(
    query_service: ResearchReportsQueryService,
    *, market: str, lookback_hours: int = 24, now: datetime | None = None,
) -> dict[str, Any]
```

Queries recent research reports (`ResearchReportsQueryService.find_relevant(since=now-lookback, limit=N)`; market-filter where the query surface supports it, else include `symbol_candidates` so Hermes can scope). Returns:

```python
{
  "market": "kr" | "us",
  "citations": [{"title", "source", "analyst", "published_at", "excerpt", "symbol_candidates"}],
  "count": int,
  "freshness": {"status": "fresh|stale|unavailable", "latest_published_at": str | None},
  "data_health": {"available_count": int},
}
```

Soft-fail: empty table → `citations: []`, `count: 0`, `freshness.status: "unavailable"` (never raises). No prose — raw material for Hermes.

### N2. Context wiring (`app/services/investment_stages/hermes_context.py`)
Add a `dimension_evidence["news"]` block immediately after the `dimension_evidence["market"]` block, `kr`/`us` only, wrapped in the same best-effort `try/except` (on error → `{"unavailable": str(exc)}`). Construct `ResearchReportsQueryService` from `self._session`.

### N3. Tests
- `news_evidence` assembler: fixture-seed `research_reports` rows → assert `citations`/`count`/`freshness`; empty-table → graceful (`citations: []`, freshness `unavailable`, no raise).
- Context export: a kr/us run yields `dimension_evidence["news"]` with the expected keys; soft-fail path on a stubbed failure.

## What's free (no implementation)

The News dimension **report** (prose, stance, confidence): Hermes writes it via the existing `POST /hermes/dimension-reports` with `dimension="news"`. Persistence (`investment_dimension_reports`), read surface (`GET …/dimension-reports?dimension=news`), and final-composition citation (`dimension_report_uuids`) all work via the ROB-306/308 generic contract — untouched here.

## Non-goals / boundaries

- No new table / endpoint / migration / schema. No in-process LLM. No broker/order/watch/order-intent mutation.
- No research_reports ingestion enablement (separate operator gate; data deferred, like ROB-282 for crypto).
- Market-wide only (per-symbol News deferred).

## Testing strategy

Fixture-based only (no live ingestion): seed `research_reports` via the repository/model in `db_session`, exercise the assembler + context export. Confirm ROB-287 no-internal-LLM import guard still passes (the new module imports no LLM provider).

## Assumptions to verify during implementation

- `ResearchReportsQueryService` method + signature for "recent reports" (`find_relevant` vs `find_feed_page`) and whether it supports a market filter; if not, post-filter on `symbol_candidates[].market` or include all and let Hermes scope.
- The freshness signal source — reuse the `research-reports/freshness` readiness logic (`research_report_ingestion_runs`) vs derive from the latest `published_at`. Pick one and make `freshness.status` deterministic.
- How the context exporter constructs session-backed services (mirror the `market_evidence` repo construction).

## Program order

Parent: ROB-306. Slice 3 (News). Next: Fundamentals (#4), Sentiment (#5), crypto Market evidence (after ROB-282). Loop-validation tooling = ROB-309 (done).
