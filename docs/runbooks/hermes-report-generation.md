# Hermes report generation (ROB-287) — retired (hk 6930)

External report composition through Hermes is retired. Nothing in this runbook
can be run any more; the activation, cutover and smoke procedures it used to
hold are in git history.

## What was removed

| Removed | Task |
|---|---|
| HTTP routes `/trading/api/investment-reports/hermes/*`, the `AuthMiddleware` token branch, the ingest token settings and the round-trip smoke CLI | hk 6933 |
| MCP tools `investment_report_prepare_bundle`, `investment_report_get_hermes_context`, `investment_report_create_from_hermes_composition`, `investment_stage_artifacts_ingest_from_hermes`, `investment_report_prepare_intraday_context` | hk 6950 |
| Services `app/mcp_server/tooling/investment_hermes_handlers.py`, `app/services/investment_stages/{hermes_context,hermes_ingest,symbol_report_ingest}.py`, `app/services/investment_dimensions/dimension_report_ingest.py` | hk 6950 |
| Schemas `app/schemas/{hermes_composition,investment_symbol_reports,investment_dimension_reports}.py` | hk 6950 |
| `scripts/shadow_replay_probe.py`, `scripts/shadow_replay.py` and its MCP config | hk 6950 |

The lane allowlist rows for these tools were removed under the operator-approved
list in `tests/mcp_server/_operator_retired_tools.py`.

## What stays

- **History tables**: `review.investment_dimension_reports` and
  `review.investment_symbol_intermediate_reports` were written only by the
  removed ingest paths. They are frozen, read-only history. The shared
  `investment_stage_runs` / `investment_stage_artifacts` and
  `investment_report_news_citations` tables and their ORM models are unchanged.
  No migration drops or alters any of them.
- The read-only dimension-report route.
- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` (default off), which still gates
  `investment_report_generate_from_bundle` and the investment-reports router.
- The live watch notifier `app/services/hermes_client.py`, the
  `hermes-paper-kis` MCP profile and its Task 881 tool.
- News-citation and composition shapes, now in
  `app/schemas/investment_report_composition.py`.

Contract: `docs/agent-contracts/mcp-and-analysis-contracts.md`.
