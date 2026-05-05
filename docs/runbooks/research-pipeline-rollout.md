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
