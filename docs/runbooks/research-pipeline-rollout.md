# Research Pipeline Rollout Runbook

This runbook describes the rollout, monitoring, and fallback procedures for the new research pipeline (ROB-112).

## Scope

- Transitioning from legacy research logic to the new `research_pipeline` router.
- Monitoring throughput and reliability of the new pipeline.
- Immediate fallback procedures in case of degradation.

## How to Enable

Rollout is controlled via environment variables. Follow these steps in order:

1.  **Enable the Pipeline Router:**
    Set `RESEARCH_PIPELINE_ENABLED=True`. This activates the new pipeline infrastructure but does not yet route live traffic.
2.  **Enable Dual-Write (Optional):**
    Set `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED=True`. This ensures that data is populated into both the new pipeline tables and legacy tables, allowing for side-by-side comparison.
3.  **Enable Live Traffic:**
    Set `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED=True`. This dispatches the `analyze_stock` MCP tool calls to the new pipeline router.

## Monitoring

Use the following tools and checks to ensure the pipeline is healthy:

### 1. Throughput & Session History
Use the `research_session_list_recent` MCP tool to check throughput and session statuses:
```bash
# Example MCP tool call (via client)
research_session_list_recent(limit=10)
```

### 2. Error Detection (Verdicts)
Check the `stage_analysis` table for `unavailable` verdicts. A high frequency of these verdicts signals issues with upstream data providers or the analysis engine:
```sql
SELECT * FROM stage_analysis WHERE verdict = 'unavailable' ORDER BY created_at DESC;
```

### 3. Log Warnings
Monitor application logs for fallback warnings, which indicate that the new pipeline failed and automatically routed the request to the legacy path:
- Look for: `research_pipeline.analyze_stock fallback`

## Fallback / Kill-switch

If the new pipeline exhibits unexpected behavior, use these kill-switches:

1.  **Stop Traffic (Preferred):**
    Set `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED=False`.
    - **Effect:** Immediately returns all `analyze_stock` traffic to the legacy code path. The new pipeline infrastructure remains active but idle.
2.  **Full Disable:**
    Set `RESEARCH_PIPELINE_ENABLED=False`.
    - **Effect:** Disables the router and all new logic associated with the research pipeline.

## Querying the Pipeline Tables

### Latest Stage Row per `stage_type` (one session)

`stage_analysis` is **append-only** — multiple rows per `(session_id, stage_type)` are allowed. To pull the most recent row for each of the four stages in a session:

```sql
SELECT DISTINCT ON (stage_type) *
FROM stage_analysis
WHERE session_id = :sid
ORDER BY stage_type, executed_at DESC;
```

This is the same shape the `research_session_get` MCP tool and `GET /api/research-pipeline/sessions/{id}/stages` router use internally.

### Summary + Cited Stage Rows

```sql
SELECT s.id AS summary_id, s.decision, s.confidence,
       l.stage_analysis_id, l.weight, l.direction
FROM research_summaries s
JOIN summary_stage_links l ON l.summary_id = s.id
WHERE s.session_id = :sid
ORDER BY s.executed_at DESC, l.id;
```

Bull/bear arguments live in `s.bull_arguments` / `s.bear_arguments` (JSONB).

## Dual-write to `stock_analysis_results`

When `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED=True`, every finalized `research_summary` triggers an additional insert into the legacy `stock_analysis_results` table. Mapping:

| Source (`research_summary` / `price_analysis`) | Target (`stock_analysis_results`) |
|---|---|
| `decision`, `confidence` | `decision`, `confidence` |
| `price_analysis.appropriate_buy_min/max` | `appropriate_buy_min/max` |
| `price_analysis.appropriate_sell_min/max` | `appropriate_sell_min/max` |
| `price_analysis.buy_hope_min/max` | `buy_hope_min/max` |
| `price_analysis.sell_target_min/max` | `sell_target_min/max` |
| `reasons`, `detailed_text` | `reasons`, `detailed_text` |
| `model_name` (or `"research_pipeline"`) | `model_name` |
| `f"research_summary:{summary_id}/prompt_version:{prompt_version}"` | `prompt` |

The `prompt` field encodes the upstream summary id so consumers can trace back to the cited stage rows. Existing legacy consumers (`stock_info_service.get_latest_analysis`, MCP `analyze_stock` legacy fallback, market-brief tools) keep working unchanged because the row shape is identical.

To verify dual-write is healthy after enabling:

```sql
-- recent dual-write rows
SELECT id, model_name, prompt, created_at
FROM stock_analysis_results
WHERE prompt LIKE 'research_summary:%'
ORDER BY created_at DESC
LIMIT 20;
```

## Known Gaps / Out of Scope (Phase 1)

- **Social stage** is a placeholder only (`verdict='unavailable'`, `confidence=0`, `signals.reason='not_implemented'`). Treat as missing evidence in summary; `>=2` stale stages still force `decision=hold`.
- **No `superseded_by`** column on `stage_analysis`. "Latest" is a query, not a mutable flag — keeps the table strictly append-only.
- **`order_outcome` table** is intentionally deferred. Existing `TradingDecisionOutcome` covers retro analysis; if join coverage proves insufficient, file a follow-up Linear issue rather than altering this schema.
- **No live broker / watch / order-intent / scheduler side effects** — verified by `tests/analysis/test_pipeline_safety.py` (forbidden-imports assertion).
- **No bulk update / backfill of historical `stock_analysis_results`** — dual-write is forward-only.
- **React Research Session page** (5-tab read-only viewer) is **not** in this PR. Backend + read-only MCP tools + GET endpoints are complete; UI is tracked as a follow-up issue.

## Hermes / Server Handoff Checklist

Before promoting beyond local:

- [ ] Confirm `.env` (and production secrets) keep all three flags at `False` until staging cutover plan is signed off.
- [ ] `uv run alembic upgrade head` on the target environment; capture `alembic current` output. Migration is **additive only** — no destructive ops, no `stock_analysis_results` rewrite.
- [ ] Smoke-test enable-only step 1: set `RESEARCH_PIPELINE_ENABLED=True`, dual-write OFF, analyze_stock OFF. Call `research_session_list_recent` MCP tool — expect empty list, not an error.
- [ ] Run one `analyze_stock` call against a known symbol with `ANALYZE_STOCK_ENABLED=True` and `DUAL_WRITE_ENABLED=False`. Verify the response keys match the legacy contract (no schema drift). Roll back the flag immediately after.
- [ ] Enable dual-write: run another `analyze_stock` call, confirm one new row in `stock_analysis_results` with `prompt LIKE 'research_summary:%'`.
- [ ] Watch logs for `research_pipeline.analyze_stock fallback` warnings during the first hour. Repeated fallbacks → roll back via the kill-switch above.
- [ ] No broker / KIS / Upbit / Alpaca order traffic should appear that did not exist before this rollout. If you see any, **immediately** set `RESEARCH_PIPELINE_ENABLED=False` and page Hermes.

## Tables Created by This Rollout

| Table | Append-only? | Notes |
|---|---|---|
| `research_sessions` | No (status transitions: `open` → `finalized`/`failed`/`cancelled`) | One per (symbol, run) |
| `stage_analysis` | **Yes** | Multiple rows per `(session_id, stage_type)` allowed |
| `research_summaries` | **Yes** (no UNIQUE on `session_id`) | Re-summaries supported |
| `summary_stage_links` | Yes | `weight ∈ [0,1]`, `direction ∈ {support, contradict, context}` |
| `user_research_notes` | No (updates allowed) | Schema only in Phase 1; no service writes yet |
