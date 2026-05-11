# ROB-192 /invest Toss-parity coverage

This page documents the read-only coverage dashboard/API added for `/invest`.

Endpoint: `GET /invest/api/coverage`

Query parameters:
- `market`: `kr`, `us`, `crypto`, or `all` (default `kr`)
- `symbols`: optional comma-separated symbols for symbol-level diagnostics
- `asOf`: optional trading date override for deterministic inspection/tests

Contract:
- Source of truth is local `auto_trader` DB/read-model state.
- Toss is only a parity/reference benchmark; the endpoint does not read from Toss.
- The endpoint is read-only: it does not submit/cancel/modify orders, call broker clients, start collectors, run backfills, or generate buy/sell recommendations.

Coverage states:
- `fresh`: current read-model rows are available.
- `stale`: only older read-model rows are available.
- `partial`: some current rows are present, but expected scope is incomplete or an ingestion partition is degraded.
- `missing`: no local rows are available for the surface.
- `unsupported`: the surface is intentionally unsupported for the selected market.
- `provider_unwired`: a Toss-parity data surface exists conceptually, but no durable local `/invest` read model is wired yet.
- `error`: local ingestion metadata indicates a failed/degraded state with no fresh rows.

Current surfaces:
- `symbol_universe`: `kr_symbol_universe`, `us_symbol_universe`.
- `screener_snapshots`: `invest_screener_snapshots`, KR/US only.
- `news_feed`: `news_articles` and `news_ingestion_runs`.
- `calendar_events`: `market_event_ingestion_partitions` and calendar read models.
- `research_reports`: compact `research_reports` metadata; full report bodies remain excluded by policy.
- `investor_flow`: `investor_flow_snapshots`, KR only.
- `pending_orders`: `pending_orders`; empty is considered OK because there may be no open orders.
- `holdings`: local holdings/account-panel foundation; empty is considered OK because a market may have no tracked holdings.
- `orderbook_nxt_capability`: KR NXT eligibility from `kr_symbol_universe.nxt_eligible`; US is unsupported and crypto orderbook remains provider-backed/unwired.
- `quotes`, `ohlcv`, `valuation_fundamentals`: explicitly `provider_unwired` until durable read models exist.

Frontend:
- Route: `/invest/coverage`
- Nav label: `커버리지`
- The UI groups counts by state, lists surface-level gaps, and optionally shows symbol-level coverage for screener/news/investor-flow.

Operational notes:
- Freshness thresholds are intentionally conservative and read-only diagnostics only: news 24h, calendar/screener/investor-flow 36h, research metadata 7d, pending orders 30m last-seen.
- This dashboard should be used to identify data gaps before implementing Toss-parity product features. It must not become a place for trading recommendation logic.
