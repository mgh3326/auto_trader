# ROB-203 /invest coverage actionability

This page documents the read-only coverage dashboard/API for `/invest`.

Endpoint: `GET /invest/api/coverage`

Query parameters:
- `market`: `kr`, `us`, `crypto`, or `all` (default `kr`)
- `symbols`: optional comma-separated symbols for symbol-level diagnostics
- `asOf`: optional trading date override for deterministic inspection/tests

Contract:
- Source of truth is local `auto_trader` DB/read-model state.
- Toss is only a parity/reference benchmark; the endpoint does not read from Toss.
- Naver is only a candidate/reference signal where already represented by owned read models or explicitly marked as request-time/unwired readiness.
- The endpoint is read-only: it does not submit/cancel/modify orders, call broker clients, start collectors, run backfills, activate schedulers, or generate buy/sell recommendations.

Coverage states:
- `fresh`: current read-model rows are available.
- `stale`: only older read-model rows are available.
- `partial`: some current rows are present, but expected scope is incomplete or an ingestion partition is degraded.
- `missing`: no local rows are available for the surface.
- `unsupported`: the surface is intentionally unsupported for the selected market.
- `provider_unwired`: a Toss/Naver-parity data surface exists conceptually, but no durable local `/invest` read model is wired yet.
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

## Actionability metadata

Every surface and symbol diagnostic includes an `actionability` object. This object is advisory metadata for planning work; it is not an execution command.

Fields:
- `priority`: `none`, `low`, `medium`, `high`, or `blocked`.
- `action`: `none`, `monitor`, `investigate`, `repair_read_model`, `backfill_candidate`, `scheduler_candidate`, `provider_contract_needed`, or `unsupported_no_action`.
- `queue`: suggested work queue, for example `invest-screener-snapshots`, `news-ingestor`, `market-events-ingestion`, `research-report-ingestion`, `investor-flow-ingestion`, or `provider-contract`.
- `approvalGates`: required approvals before any future remediation is executed.
- `reason`: short human-readable explanation.
- `safeByDefault`: always indicates this dashboard path is safe-by-default/read-only.

The dashboard must not render buttons or copy implying “run backfill now”, “activate scheduler”, or “place order”. Use it to decide what work should be planned next.

## Coverage state to work queue mapping

| State | Meaning | Default action | Approval gate |
| --- | --- | --- | --- |
| `fresh` | Current local read-model rows exist | monitor/no action | none |
| `stale` | Only old local rows exist | investigate or repair read model | production DB write approval before any backfill |
| `partial` | Some expected rows exist | repair read model/backfill candidate | production DB write approval before any backfill |
| `missing` | No local rows exist | investigate, backfill candidate, or provider contract | production DB write approval and scheduler activation approval when remediation writes or schedules jobs |
| `unsupported` | Intentionally out of scope | unsupported/no action | none |
| `provider_unwired` | Concept exists but durable read model/provider contract is absent | provider contract/code work | code review; later DB/scheduler approval if added |
| `error` | Ingestion metadata failed/degraded | investigate | depends on remediation |

Representative queues:
- `symbol_universe` -> `invest-data-read-models`
- `screener_snapshots` -> `invest-screener-snapshots`
- `news_feed` -> `news-ingestor`
- `calendar_events` -> `market-events-ingestion`
- `research_reports` -> `research-report-ingestion`
- `investor_flow` -> `investor-flow-ingestion`
- `holdings` -> `account-panel-read-model`
- `pending_orders` -> `order-reconciliation-read-model`
- `quotes`, `ohlcv`, `valuation_fundamentals` -> `provider-contract`

## All-market symbol diagnostics

`GET /invest/api/coverage?market=all&symbols=005930,AAPL,MSFT` partitions requested symbols across KR and US diagnostics while preserving request order.

Resolution rules:
- Prefer `kr_symbol_universe.symbol` and `us_symbol_universe.symbol` matches.
- Fallback to six-digit numeric symbols as KR.
- Fallback to uppercase alphabetic tickers such as `AAPL`/`MSFT` as US.
- Crypto-style symbols such as `KRW-BTC` are returned as unsupported symbol rows until a durable crypto symbol diagnostic contract exists.

Expected semantics:
- `005930` returns a KR row and may include `naver_investor_flow` as a candidate/reference diagnostic.
- `AAPL` and `MSFT` return US rows.
- US rows mark `investor_flow` and `naver_investor_flow` as `unsupported`.
- Each row includes `actionability`; missing/stale diagnostics remain candidates only and do not execute remediation.

## Naver/Toss semantics

- `sourceOfTruth` must remain owned local DB/read-models such as KIS/Upbit/news-ingestor-derived tables.
- Toss remains a UI/parity/reference benchmark only.
- Naver remains candidate/reference/readiness metadata only.
- Naver discussion data is aggregate-signal-only and must not clone/display public community text.
- Neither Toss nor Naver should become `sourceOfTruth` in this contract.

## Frontend

- Route: `/invest/coverage`
- Nav label: `커버리지`
- The UI groups counts by state, lists surface-level gaps, shows Naver/Toss candidate readiness chips, renders actionability priority/action/queue/approval gates, and optionally shows symbol-level coverage for screener/news/investor-flow.

## Approval gates

Any production remediation discovered from coverage output requires a separate approval packet and task. This issue does not approve:
- production DB writes or backfills;
- scheduler/TaskIQ/Prefect activation or unpause;
- broker/KIS/Upbit/Alpaca calls;
- order submit/cancel/modify;
- watch-alert or order-intent mutations;
- live scraping in request paths.

Operational notes:
- Freshness thresholds are intentionally conservative and read-only diagnostics only: news 24h, calendar/screener/investor-flow 36h, research metadata 7d, pending orders 30m last-seen.
- This dashboard should be used to identify data gaps before implementing Toss/Naver-parity product features. It must not become a place for trading recommendation logic.
