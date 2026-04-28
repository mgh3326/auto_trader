# ROB-13 Production DB Smoke Report

Date: 2026-04-28
Worktree: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-13-tradingagents-production-db-smoke`
Branch: `feature/ROB-13-tradingagents-production-db-smoke`

## Smoke command shape

Executed the new `scripts/smoke_tradingagents_db_ingestion.py` harness against the deployed native runtime configuration using:

- `ENV_FILE=/Users/mgh3326/services/auto_trader/shared/.env.prod.native`
- TradingAgents repo path: `/Users/mgh3326/work/TradingAgents`
- TradingAgents Python wrapper: `/Users/mgh3326/work/TradingAgents/.venv/bin/python-tradingagents-wrapper`
- TradingAgents base URL: `http://127.0.0.1:8796/v1`
- model: `gpt-5.5`
- analysts: `market`
- symbol/date: `005930.KS` / `2025-01-15`
- instrument type: `equity_kr`

No `.env` contents or secret values were printed.

## Result

Status: PASS

```json
{
  "ok": true,
  "session": {
    "id": 2,
    "source_profile": "tradingagents",
    "market_scope": "kr",
    "advisory_only": true,
    "execution_allowed": false
  },
  "proposal": {
    "id": 10,
    "symbol": "005930.KS",
    "instrument_type": "equity_kr",
    "proposal_kind": "other",
    "side": "none",
    "original_payload_advisory_only": true,
    "original_payload_execution_allowed": false,
    "user_response": "pending"
  },
  "side_effect_counts": {
    "actions": 0,
    "counterfactuals": 0,
    "outcomes": 0
  }
}
```

## Acceptance checklist

- [x] `ingest_tradingagents_research()` succeeded in the deployed runtime.
- [x] `TradingDecisionSession.source_profile == "tradingagents"`.
- [x] `market_scope == "kr"`.
- [x] `market_brief.advisory_only is True`.
- [x] `market_brief.execution_allowed is False`.
- [x] Exactly one proposal was associated with the smoke session.
- [x] `proposal_kind == "other"`.
- [x] `side == "none"`.
- [x] Proposal payload `advisory_only is True`.
- [x] Proposal payload `execution_allowed is False`.
- [x] No `TradingDecisionAction`, `TradingDecisionCounterfactual`, or `TradingDecisionOutcome` rows were created for the smoke session.
- [x] No live order, `dry_run=False`, watch registration, order intent, or broker side-effect path was used by the harness.
