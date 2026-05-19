# ROB-203 /invest coverage actionability

> **ROB-271 update:** `/invest/coverage` is now a Toss/Naver benchmark data-sourcing gap matrix first. The KR action-readiness card and the raw coverage surface table remain available as collapsed/secondary sections. The new product-facing endpoint is read-only and does not replace the existing `/invest/api/coverage` or `/invest/api/kr/action-readiness` contracts.

## ROB-271 — Benchmark gap matrix (data-sourcing-gap-first)

Endpoint: `GET /invest/api/coverage/benchmark-gap`

Query parameters:
- `market`: `kr`, `us`, `crypto`, `all` (default `kr`)
- `asOf`: optional trading date override

Purpose: answer "토스·네이버 대비 auto_trader 가 어떤 데이터를 다음에 수급해야 하는가?" without estimating, without scraping, and without proposing buy/sell logic.

Source authority (explicit, unchanged):
- **KIS live** = holdings / cash / orderable cash / open orders / sellable quantity broker authority.
- **auto_trader DB/read-models** = product authority for `/invest` surfaces (market, screener, news, calendar, valuation, flow, ledger, action-report snapshots).
- **Toss** = benchmark / reference only. Never `sourceOfTruth`.
- **Naver** = candidate / reference unless explicitly promoted to an owned read-model.
- **community / discussion** = aggregate-signal-only candidates. Raw text cloning is prohibited.

Product-facing status vocabulary (additive — legacy `CoverageState`/`ActionReadinessState` are preserved):

| Status | Meaning |
| --- | --- |
| `covered` | already available and mapped in auto_trader |
| `partial` | partially available; needs more fields/better mapping |
| `stale` | data exists but too old |
| `missing` | no owned read-model/source yet |
| `candidate_unwired` | source candidate exists but ingest/read-model/UI not wired |
| `benchmark_only` | visible in Toss/Naver, used only for comparison |
| `intentionally_excluded` | intentionally not collected (e.g., community text cloning) |
| `unsupported` | outside current scope |
| `blocked_by_auth_or_policy` | blocked by login/private API/robots/rate limit/licensing |

Legacy developer states (`blocked`, `missing`, `unknown`, `확인 불가`) remain in the action-readiness API and in the raw coverage surface table, both of which are now rendered under collapsed secondary sections.

UI information architecture (data-sourcing-gap-first):
1. Benchmark gap summary
2. 다음 수급 후보 list (priority-ordered)
3. Toss benchmark coverage
4. Naver benchmark coverage
5. auto_trader 내부 / KIS coverage
6. (collapsed) KR 액션 리포트 준비도 — secondary
7. (collapsed) 개발자 · 디버그 raw 커버리지 — original surfaces table + symbol diagnostics

Non-goals of this issue:
- No broker/order/watch/order-intent mutation.
- No buy/sell recommendation logic.
- No production DB writes, backfills, or scheduler activation.
- No live Toss/Naver scraping in request paths.
- No promotion of Toss/Naver to `sourceOfTruth`.
- No cloning of public community text.
- Implementing every downstream data collector is **out of scope**. This issue identifies and prioritizes gaps; collection work belongs to follow-up Linear issues.

New rows discovered during work that do not yet have a Linear issue should be marked with `newIssueCandidate=true` in the row payload. **Do not auto-create Linear issues from the dashboard.** Promotion to a real Linear issue is a separate human-approved handoff step.

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

## KR action-report readiness

ROB-256 adds a dedicated read-only readiness API for domestic-stock action-report prerequisites:

Endpoint: `GET /invest/api/kr/action-readiness`

Query parameters:
- `symbol`: optional six-digit KR equity code. Invalid/non-KR symbols return `overallState="blocked"` without provider or broker calls.

Contract:
- The endpoint maps existing `/invest/api/coverage` read-model surfaces plus the existing `InvestHomeService` account-panel read path into action-report readiness metadata.
- KIS live is authoritative for tradeable KR holdings, cash/orderable state, open-order visibility, and sellable quantity.
- `/invest` DB/read models are product authority for quotes/OHLCV/technical-readiness, screeners, Naver momentum/theme reference data, investor flow, news/issues/disclosures, calendar, valuation/research, and historical execution/fill readiness.
- Manual/paper holdings and Toss/Naver/external sources are reference/candidate/supporting only; they must not be rendered as action-readiness `sourceOfTruth`.
- Missing, stale, partial, failed, unsupported, or unwired data is shown as `missing`, `degraded`, `blocked`, `unsupported`, or `unknown`/`확인 불가`; the service must not estimate missing values.
- The endpoint does not submit/cancel/modify orders, mutate watch/order-intent ledgers, run production DB writes/backfills, activate schedulers, or scrape Toss/Naver/provider pages from request paths.

Response highlights:
- `overallState`: `ready`, `degraded`, `blocked`, `missing`, `unsupported`, or `unknown`.
- `canGenerateBuyReport` / `canGenerateSellReport`: conservative readiness booleans, not trade recommendations.
- `families`: grouped readiness cards with `authority`, `sourceOfTruth`, `references`, coverage state, blockers, warnings, notes, and advisory `actionability`.
- `sourcePolicy`: human-readable source-authority rules echoed to the frontend.

Readiness family mapping:

| Family | Authority | Blocks/degrades |
| --- | --- | --- |
| `kis_live_holdings`, `kis_live_cash_orderable`, `kis_live_open_orders`, `kis_live_sellable_quantity` | KIS live via existing account-panel read path | Cash blocks buy readiness; holdings/sellable blocks sell readiness; unavailable live broker/account state is `확인 불가`. |
| `trade_journals` | `auto_trader` live trade journals | Missing active thesis/target/stop context degrades reports and is mandatory context before sell recommendations. |
| `quotes`, `ohlcv`, `technical_indicators`, `support_resistance` | `/invest` read models | Missing quote for a requested symbol blocks action reports; unwired indicator/support-resistance surfaces degrade rather than fabricate values. |
| `orderbook_session`, `nxt_eligibility`, `pending_order_reconciliation` | `/invest` read models plus KIS live authority where already represented elsewhere | Missing reconciliation/open-order visibility blocks or degrades; no request-path orderbook fetch is introduced. |
| `screener_snapshots`, `naver_momentum_events`, `naver_momentum_candidates`, `naver_theme_events`, `investor_flow` | `/invest` read models; Naver is reference/candidate only | Stale/missing data degrades reports. |
| `news_feed`, `issue_clusters`, `disclosures`, `calendar_events` | `/invest` read models | Missing/stale context degrades reports and renders `확인 불가`. |
| `valuation_fundamentals`, `research_reports`, `research_consensus` | `/invest` read models | Missing/stale valuation or consensus degrades; full report bodies remain excluded when existing policy excludes them. |
| `execution_ledger`, `sell_history` | `/invest` historical ledger/read models; KIS live remains current open-order/sellable authority | Stale/missing historical fill/sell history degrades sell reports. |

Frontend placement:
- `/invest/coverage` now renders a top `KR 액션 리포트 준비도` card for KR mode before the raw coverage table.
- It shows overall state, buy/sell report readiness, blockers, degraded signals, source policy, and grouped family cards.
- It intentionally renders advisory actionability text only; no order/backfill/scheduler/run controls are present.

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
