# ROB-1488 — auto_trader feature and schema inventory toward a smaller core

Status: decision draft for the operator (hk task 2709). Documents only — no code, no migration, no runtime change.
Base: `origin/main` at `6097ac34e` (2026-11-02). Evidence read 2026-11-02/03.

## 0. What this is, and what it is not

The operator's question (2026-11-03): AT keeps growing (~3,000 non-test Python files, `app/services` 1,058
files, 233 registered MCP tools) while a few tools carry the load. What does an AT rebuilt from only the
needed features look like, and how do we get there from today by retiring and migrating?

This document answers with evidence, in four parts:

1. a **feature inventory** of every registered MCP tool, HTTP router, scheduled job, CLI script and service
   package, with 30-day call counts, the tables each touches, and a keep / merge / retire proposal ($3);
2. a **schema inventory** of every table in the production database with live row counts, last write, 30-day
   write cadence, duplicate and null findings, and a keep / restructure / retire proposal ($4);
3. a **health section** — the kept things that are used but silently not loading ($5);
4. the **target architecture** (Go execution core, Python research batch, thin MCP) and the
   **retire-first migration order** with the invariants that must be proven equal before each move ($6, $7).

Every number below is measured, not estimated, unless the cell says otherwise. The query for every health
claim is listed in $8 so the desk can re-run it.

Two limits are worth stating up front:

- **The 30-day call table is the operator's Sentry export.** It names 80 tool names. Everything else is `0`
  in the tool tables. Calls are not the same as value: `order_proposal_void` is called rarely and is the only
  way to cancel a bad card.
- **Per-tool table lists are static import reachability**, not traced reads (method in $2.3). They are good
  enough to decide *which domain* owns a table and *who must be migrated together*; they are not a data-flow
  proof. The schema table's readers/writers come from the same static pass.

## 1. Summary

| Measure | Value |
|---|---|
| Non-test Python in `app/` | 1,638 files, **456,648 LOC** |
| `app/services/` | **1,058 files, 320,943 LOC** (70% of app) |
| `tests/` | 2,246 files, 765,429 LOC |
| `research/` | 811 files, 226,913 LOC |
| `scripts/` | 255 files, 74,103 LOC |
| Registered MCP tool names (13 profiles) | **233** (217 on `default` with gates on, 151 with gates off) |
| Tools called at least once in 30d | **79 of 233 (34%)** |
| Tools with zero calls in 30d | **154 (66%)** |
| Tools with 30 or fewer calls in 30d | **37 of 79** (26 of them 8 or fewer) |
| Top 6 tools as a share of all 30d calls | **54%** (7,683 of ~14,200) (`get_quote`, `get_ohlcv`, `route_request`, `forecast_resolve`, `get_support_resistance`, `forecast_save`) |
| HTTP router modules / route declarations | **57 / 231** (57 mounted in `app.main`) |
| TaskIQ tasks / modules | **65 / 41** |
| Prefect flows | **7** (7 `@flow`, 7 `@task`) |
| Dagu DAGs under `deploy/` | **4** (all unregistered proposals) |
| NCP systemd timers (desk-owned) | **30** |
| CLI scripts in `scripts/` | **181** |
| Service packages under `app/services/` | **73** + 8 top-level modules |
| SQLAlchemy models with `__tablename__` | **199** (unique tables) |
| Tables in the live DB (public/review/research/paper) | **207**; **211** enumerated incl. unmodeled |
| Tables empty (0 rows) in prod | **95 of 207 (46%)** |
| Real DB size | **4.4 GB**; 61% of it is 8 market-data snapshot tables |

Proposals, at tool level: **69 keep, 37 keep-because-a-kept-flow-needs-it, 35 merge into another tool,
81 retire, 11 keep-and-decide**. At table level: **93 keep (incl. 8 "keep, verify" and 6 "keep, fix"),
11 restructure, 103 retire, 4 not in the prod DB**.

The load is small and I/O-bound: about **14,200 tool calls in 30 days** (counting each "1-8 calls" row as 8)
is roughly **one call every three minutes**, and the top-latency tools (`analyze_stock_batch` p95 7.7 s, `get_operating_briefing` p95 45 s,
`get_earnings_calendar` p95 10 s, `discover_buy_candidates_fanout` p95 17.5 s) are dominated by waiting on
brokers and HTTP. That is the same conclusion ROB-1436 reached for Go (`docs/plans/rob-1436-go-port-candidates.md`),
and it is why the target architecture below moves **execution**, not computation.

The real cost is not runtime, it is **surface area**: 154 registered tools nobody calls, 95 empty tables,
and — the finding that matters most — **a dozen kept features that are read every day but whose writer
stopped**. Those are in $5.

## 2. Method and counts

### 2.1 MCP tools

The tool list is taken from `tests/mcp_server/profile_tool_snapshot.json`, which CI already verifies against
the *actual* registrar output for all 13 profiles, in both gate states
(`tests/mcp_server/test_profile_tool_snapshot.py`). I did not start a server or call a tool.

- 233 distinct names across the union of profiles; 217 on `default` with feature gates on; 151 with gates off.
- Per-tool metadata (module, mutation class, prompt/runbook/code reference counts, the 2026-09-03 A/B/C/D/U
  classification) comes from `docs/mcp-tool-usage-audit-20260903.md`. 12 names added after that audit
  (`nh_mock_*`, `get_parking_exclusion`, `kis_mock_ledger_expire_day_orders`, `toss_proposal_accounts`, the two
  20-tool H3 closed worlds) have no audit row; their module was resolved from the registrar.
- 30-day calls are the operator's Sentry table (`usage-30d.md`), which names 80 tool names; 79 resolve to a
  registered tool and one label is ambiguous. All 79 are on `default`.

### 2.2 Routers, jobs, CLI, services

| Surface | How I counted it | Count |
|---|---|---|
| HTTP routers | AST: one `APIRouter(...)` + decorator routes per file in `app/routers/` | 57 modules, 231 route decorators |
| Mounted routers | `grep include_router app/main.py` | 57 (every module except `execution_intent_gateway.py`, which is a separate loopback process by contract) |
| TaskIQ tasks | AST: `broker.task` / `taskiq_broker.task` decorators in `app/tasks/` | 65 tasks in 41 modules |
| Prefect flows | AST: `@flow` in `app/flows/` | 7 flows (+7 `@task`) |
| Dagu DAGs | `deploy/dagu/**/*.yaml` | 4 |
| Desk timers | `ops/ncp/systemd/*.timer` | 30 |
| CLI | `scripts/*.py` (excluding `__init__`) | 181 |
| Service packages | `app/services/*/` directories | 73 |
| Models | AST: classes with `__tablename__` in `app/models/` | 199 |
| Live tables | `pg_tables` in `public`, `review`, `research`, `paper` | 207 |

### 2.3 How "tables a tool touches" was derived

For each tool I took its registering module (from the 2026-09-03 audit, which executed the registrars), parsed
every `import` in that file, and collected SQLAlchemy model classes imported at **depth 1** (falling back to
depth 2 when depth 1 yields nothing). A table is listed when one of its model classes appears in that set.
This over-reports for modules that import a shared hub and under-reports when the real reader is a service
two hops away. It is a *migration grouping* device: if two tools show the same table, they must move together.
199 of 233 tools resolve at least one table this way.

## 3. Feature inventory

Legend for the proposal column:

- **keep** — used in the last 30 days, or a live path that a kept feature cannot work without.
- **keep (unused; kept flow needs it)** — 0 calls but the kept flow (approval, cancel, reconcile, guard) is
  unreachable without it. These are the tools that *must not* be retired just because they are quiet.
- **merge into X** — same reader or same data as X; two tools for one answer.
- **retire** — 0 calls in 30 days *and* no kept flow needs it, and (for the table) no writes.
- **keep, decide** — 0 calls, no clear owner; needs an operator call, not a default.

Profiles column: `D` = `default`; `D+n` = also on *n* closed-world profiles.

### 3.1 market data (50 tools)

Reads of quotes, candles, orderbooks, fundamentals, news, calendars and index/crypto context.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `get_quote` | 1845 | D+9 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `get_ohlcv` | 1724 | D+8 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `get_support_resistance` | 909 | D+8 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_news` | 193 | D+7 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_disclosures` | 191 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `get_investment_opinions` | 148 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_market_index` | 123 | D+7 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_crypto_fear_greed` | 113 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `get_crypto_long_short_ratio` | 113 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_crypto_funding_rate` |
| `get_upbit_altseason` | 113 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_market_index` |
| `get_orderbook` | 75 | D+6 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `get_market_news` | 29 | D+6 | `news_handlers` | `news_analysis_results`, `news_article_related_symbols`, `news_articles` **+1** | merge into `get_news` |
| `get_analyst_consensus` | 22 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_company_profile` |
| `get_fx_rate` | 17 | D+7 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_holdings_news` | 11 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_news` |
| `get_company_profile` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `get_cost_basis_distribution` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_crypto_catalysts` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_crypto_funding_rate` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_crypto_market_regime` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_crypto_fear_greed` |
| `get_crypto_open_interest` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_crypto_order_flow` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_crypto_profile` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_crypto_social` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_crypto_top_movers` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | merge into `get_market_index` |
| `get_earnings_calendar` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_execution_strength` | 0 | D+6 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep (unused; decide) |
| `get_financials` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_company_profile` |
| `get_indicators` | 0 | D+8 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `get_insider_transactions` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_company_profile` |
| `get_intraday_investor_flow` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_investor_trends` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_intraday_investor_flow` |
| `get_kimchi_premium` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep (unused; decide) |
| `get_krx_session_health` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | retire |
| `get_latest_market_brief` | 0 | D+6 | `market_brief_tools` | `stock_analysis_results`, `stock_info` | merge into `get_operating_briefing` |
| `get_market_issues` | 0 | D+6 | `news_handlers` | `news_analysis_results`, `news_article_related_symbols`, `news_articles` **+1** | merge into `get_news` |
| `get_market_reports` | 0 | D+6 | `market_brief_tools` | `stock_analysis_results`, `stock_info` | merge into `get_latest_market_brief` |
| `get_momentum_candidates` | 0 | D+7 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `get_retail_sentiment` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_investment_opinions` |
| `get_sector_peers` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_company_profile` |
| `get_short_interest` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_company_profile` |
| `get_spike_attribution` | 0 | D+6 | `spike_attribution_registration` | — | merge into `get_news` |
| `get_theme_events` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep (unused; decide) |
| `get_toss_ai_signal` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_toss_buy_balance` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | retire |
| `get_upbit_index` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | merge into `get_market_index` |
| `get_valuation` | 0 | D+6 | `fundamentals_handlers` | `market_event_ingestion_partitions` | keep |
| `market_quote_snapshot_ensure` | 0 | D+2 | `market_quote_snapshot_tools` | `kr_symbol_universe`, `market_quote_snapshots`, `upbit_symbol_universe` **+1** | keep |
| `market_quote_snapshot_latest` | 0 | D+1 | `market_quote_snapshot_tools` | `kr_symbol_universe`, `market_quote_snapshots`, `upbit_symbol_universe` **+1** | merge into `market_quote_snapshot_ensure` |
| `search_symbol` | 0 | D+6 | `market_data_quotes` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | retire |

Notes on this group:

- `get_quote` / `get_ohlcv` / `get_support_resistance` / `get_orderbook` / `get_indicators` are the load-bearing
  read path (about 32% of all calls). They resolve per market through KIS, Toss, Yahoo and Upbit.
- `get_news` reads the DB pipeline; `get_market_news` / `get_market_issues` / `get_holdings_news` are three
  more news entry points over the same feed. Merge them into `get_news`.
- The fundamentals family is 31 registrations in one module (`fundamentals_handlers.py`). Eight are used, the
  rest are never called. `get_financials` (≤8), `get_analyst_consensus` (22) and `get_company_profile` (≤8) all
  hit yfinance and should be one `get_fundamentals`.
- Crypto context is spread over six tools (`get_crypto_fear_greed` 113, `get_crypto_long_short_ratio` 113,
  `get_crypto_funding_rate` 0, `get_crypto_open_interest` 0, `get_crypto_market_regime` 0,
  `get_crypto_order_flow` 0). Two are used; the other four should fold into them.
- `get_earnings_calendar` is used 40 times with a p95 of **10 s** — the Finnhub path is a retry-bounded async
  fetch (ROB-1384); the latency is a provider property, not a code defect.

### 3.2 research & selection (29 tools)

Screening, ranking, candidate discovery, analysis artifacts and the shadow experiments.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `analyze_stock_batch` | 700 | D+8 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `analysis_artifact_save` | 482 | D+8 | `multiple` | — | keep |
| `screen_stocks` | 176 | D+8 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `analysis_artifact_get` | 49 | D+6 | `analysis_artifact_tools` | `analysis_artifacts` | keep |
| `get_top_stocks` | 21 | D+7 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `discover_buy_candidates_fanout` | 12 | D+7 | `buy_candidate_fanout_registration` | `screener_pick_log` | keep |
| `analyze_stock` | 10 | D+8 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `analysis_artifact_list` | 0 | D+6 | `analysis_artifact_tools` | `analysis_artifacts` | keep |
| `analyze_portfolio` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep (unused; decide) |
| `canonical_session_context_read` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `deterministic_replay_compare` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `emitted_trigger_playbook_read` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `evaluate_buy_gate_ab_shadow` | 0 | D+6 | `buy_gate_ab_shadow_registration` | — | retire |
| `evaluate_buy_gate_ab_shadow_v2` | 0 | D+6 | `buy_gate_ab_shadow_v2_registration` | — | retire |
| `fill_watch_context_consume_artifact` | 0 | D+1 | `fill_watch_context_registration` | `fill_watch_context_outcomes` | retire |
| `fill_watch_context_outcome_get` | 0 | D+1 | `fill_watch_context_registration` | `fill_watch_context_outcomes` | retire |
| `get_correlation` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | merge into `analyze_stock_batch` |
| `get_dividends` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | merge into `get_company_profile` |
| `identical_input_snapshot_read` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `raw_difference_artifact_write` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `research_session_get` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | retire |
| `research_session_list_recent` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | retire |
| `research_summary_get` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | retire |
| `screen_stocks_enrich` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep (unused; decide) |
| `screen_stocks_snapshot` | 0 | D+8 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | keep |
| `shadow_report_write` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `source_event_observe` | 0 | D+1 | `shadow_replay_registration` | `paper_accounts`, `market_quote_snapshots` | retire |
| `stage_analysis_get` | 0 | D+6 | `analysis_registration` | `research_sessions`, `research_summaries`, `stage_analysis` | retire |
| `support_reserve_net_consume` | 0 | D+6 | `support_reserve_net_consumer_tool` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+10** | retire |

Notes:

- `analyze_stock_batch` (700 calls, p95 **7.7 s**) and `screen_stocks` (176) are the research workhorses.
  `screen_stocks_snapshot` / `screen_stocks_enrich` (1-8 and 0) are the ROB-1309 split; keep `snapshot` as the
  DB-only path and keep `enrich` only if the sector/consensus columns are still wanted.
- `discover_buy_candidates_fanout` has 12 calls and a p95 of **17.5 s**; the fan-out is the only prospective
  candidate log (`review.screener_pick_log`, 658 rows, 12 write-days in 30d). Keep.
- The shadow experiments (`evaluate_buy_gate_ab_shadow*`, the 7 `shadow-replay` tools, `fill_watch_context_*`,
  `support_reserve_net_consume`, `watch_downside_register_sweep`) have **zero writes and zero calls**. Each is
  a hard-rule-protected observation surface. Retiring them is a policy decision, not a code cleanup — listed
  in $7 as an approval item.
- `analysis_artifact_save` (482) / `get` (49) / `list` (≤8) are the session's written record; `review.analysis_artifacts`
  holds 1,020 rows, 469 in the last 30 days. Keep.

### 3.3 orders & approval (38 tools)

Proposal lifecycle, the approval card path, and per-venue order send/cancel/modify.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `order_proposal_list` | 568 | D+7 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep |
| `toss_get_order_history` | 433 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep |
| `order_proposal_create` | 271 | D+7 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep |
| `order_proposal_get` | 263 | D+7 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep |
| `toss_reconcile_orders` | 62 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep |
| `buy_ladder_fill_preview` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `cancel_order` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `decision_table_apply` | 0 | D | `decision_table_apply_registration` | `analysis_artifacts`, `execution_ledger`, `execution_ledger_reconcile_runs` **+17** | retire |
| `decision_table_validate` | 0 | D+6 | `decision_table_registration` | — | keep (unused; kept flow needs it) |
| `get_order_history` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep |
| `kis_live_cancel_order` | 0 | D | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_live_get_order_history` | 0 | D | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_live_modify_order` | 0 | D | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_live_place_order` | 0 | D | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_live_reconcile_orders` | 0 | D | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_mock_cancel_order` | 0 | D+1 | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_mock_get_order_history` | 0 | D+1 | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep |
| `kis_mock_modify_order` | 0 | D+1 | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_mock_place_order` | 0 | D+1 | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `kis_mock_reconciliation_run` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `live_reconcile_orders` | 0 | D+1 | `orders_kis_variants` | `kis_live_order_ledger`, `live_order_ledger`, `order_proposal_rungs` | keep (unused; kept flow needs it) |
| `modify_order` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `order_proposal_expire_sweep` | 0 | D+6 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep (unused; kept flow needs it) |
| `order_proposal_list_expired_defensive` | 0 | D+6 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep (unused; kept flow needs it) |
| `order_proposal_redispatch` | 0 | D+6 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep (unused; kept flow needs it) |
| `order_proposal_void` | 0 | D+6 | `order_proposal_tools` | `execution_ledger`, `execution_ledger_reconcile_runs`, `kis_live_order_ledger` **+8** | keep (unused; kept flow needs it) |
| `place_order` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `proposal_revalidate` | 0 | D+7 | `proposal_revalidate_registration` | — | merge into `order_proposal_get` |
| `sell_ladder_fill_preview` | 0 | D+1 | `orders_registration` | `paper_accounts`, `trade_journals` | keep (unused; kept flow needs it) |
| `toss_cancel_order` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep (unused; kept flow needs it) |
| `toss_detect_manual_activity` | 0 | D | `toss_manual_activity_tools` | `order_proposal_rungs`, `toss_manual_activity_alerts` | keep |
| `toss_get_orderable_cash` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep |
| `toss_get_positions` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep (unused; kept flow needs it) |
| `toss_modify_order` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep (unused; kept flow needs it) |
| `toss_place_order` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep (unused; kept flow needs it) |
| `toss_preview_order` | 0 | D | `orders_toss_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `symbol_sectors` **+5** | keep |
| `toss_proposal_accounts` | 0 | D+1 | — | — | keep |
| `watch_downside_register_sweep` | 0 | D+6 | `downside_watch_registration` | `trade_journals` | retire |

Notes:

- `order_proposal_create` 271 calls at p95 **3.7 s**, `order_proposal_list` 568, `order_proposal_get` 263.
  `review.order_proposals` holds 1,446 rows and is written on 26 of the last 30 days. This is the busiest
  write path in AT and the one that must move to the Go core last.
- **43% of proposals never become an order**: 490 expired, 70 superseded, 57 rejected, 7 voided of 1,446.
  At rung level 1,039 of 1,464 (71%) end cancelled/expired/superseded/voided. That is the approval loop
  working, but it is also a large volume of state for a small number of fills (266 filled rungs).
- `place_order` / `cancel_order` / `modify_order` / `kis_*` / `toss_*` / `kiwoom_mock_*` order tools are all
  either 0 or ≤8 calls. They carry hard rules 4, 7, 11, 14 and 15 and each per-venue variant exists only
  because a broker's API differs. They are the reason the execution core is the *last* thing to move.
- `decision_table_validate` is 0-call but is the only consumer of the KR one-share exception in
  `config/trading_policy.yaml` (`docs/runbooks/decision-table-validate.md`). Keep.
- `toss_proposal_accounts` (0 calls) is the ROB-765 explicit-account gate for the parking account. Keep.

### 3.4 ledger & fills (9 tools)

Holdings, capital, protected quantities, account routing and the fill-event read.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `get_available_capital` | 207 | D+6 | `portfolio_holdings` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+3** | keep |
| `execution_ledger_fill_events_list_recent` | 113 | D+6 | `execution_ledger_events` | `execution_ledger`, `execution_ledger_quarantine_events`, `execution_ledger_reconcile_runs` | merge into `get_holdings` |
| `get_holdings` | 76 | D+8 | `portfolio_holdings` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+3** | keep |
| `get_cash_balance` | 0 | D+6 | `portfolio_holdings` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+3** | merge into `get_available_capital` |
| `get_portfolio_allocation` | 0 | D+6 | `portfolio_allocation` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+4** | merge into `get_holdings` |
| `get_position` | 0 | D+6 | `portfolio_holdings` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+3** | merge into `get_holdings` |
| `get_protected_positions` | 0 | D+6 | `protected_positions` | `execution_ledger`, `protected_position_revisions`, `protected_positions` | keep |
| `suggest_order_account` | 0 | D+7 | `account_routing_registration` | `kr_symbol_universe`, `krx_after_market_eligibility`, `user_settings` | keep (unused; kept flow needs it) |
| `update_manual_holdings` | 0 | D+6 | `portfolio_holdings` | `broker_accounts`, `manual_holdings`, `upbit_symbol_universe` **+3** | keep (unused; kept flow needs it) |

Notes:

- `get_holdings` (76 calls, p95 **4.1 s**) and `get_available_capital` (207 calls, p95 **2.1 s**) are the two
  slowest non-analysis reads. Both join broker positions with the ledger (`ledger_lots`, #963). They are the
  natural first *read* endpoints for a core API, because their latency is broker HTTP, not SQL.
- `execution_ledger_fill_events_list_recent` (113 calls) is the only MCP read of `review.execution_ledger`;
  merge it into `get_holdings` (which already exposes `ledger_lots`) or into the core's fill endpoint.
- `get_position` and `get_portfolio_allocation` are 0-call duplicates of `get_holdings`; `get_cash_balance`
  is ≤8 calls and duplicates `get_available_capital`.

### 3.5 mock & paper venues (57 tools)

Kiwoom mock, KIS mock, Alpaca paper, the DB paper simulator, NHPLUG and the H3 closed worlds.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `paper_place_limit_order` | 55 | D+1 | `paper_limit_order_handler` | `paper_pending_orders`, `paper_trades` | keep |
| `paper_list_pending_orders` | 49 | D+1 | `paper_limit_order_handler` | `paper_pending_orders`, `paper_trades` | keep |
| `paper_reconcile_orders` | 49 | D+1 | `paper_limit_order_handler` | `paper_pending_orders`, `paper_trades` | keep |
| `alpaca_paper_list_orders` | 37 | D+3 | `alpaca_paper` | — | keep |
| `alpaca_paper_list_positions` | 35 | D+3 | `alpaca_paper` | — | keep |
| `kiwoom_mock_get_positions` | 31 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `kiwoom_mock_get_order_detail` | 30 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `paper_cancel_pending_order` | 18 | D+1 | `paper_limit_order_handler` | `paper_pending_orders`, `paper_trades` | keep |
| `alpaca_paper_automated_preview_order` | 0 | D+1 | `alpaca_paper_automated_orders` | `market_quote_snapshots`, `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_automated_submit_order` | 0 | D+1 | `alpaca_paper_automated_orders` | `market_quote_snapshots`, `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_cancel_order` | 0 | D+2 | `alpaca_paper_orders` | `market_quote_snapshots`, `alpaca_paper_order_ledger` | keep (unused; kept flow needs it) |
| `alpaca_paper_execution_preflight_check` | 0 | D+2 | `alpaca_paper_ledger_read` | `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_get_account` | 0 | D+2 | `alpaca_paper` | — | keep |
| `alpaca_paper_get_cash` | 0 | D+2 | `alpaca_paper` | — | merge into `alpaca_paper_get_account` |
| `alpaca_paper_get_order` | 0 | D+3 | `alpaca_paper` | — | merge into `alpaca_paper_list_orders` |
| `alpaca_paper_ledger_get` | 0 | D+2 | `alpaca_paper_ledger_read` | `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_ledger_get_by_correlation` | 0 | D+2 | `alpaca_paper_ledger_read` | `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_ledger_list_recent` | 0 | D+2 | `alpaca_paper_ledger_read` | `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_list_assets` | 0 | D+2 | `alpaca_paper` | — | retire |
| `alpaca_paper_list_fills` | 0 | D+2 | `alpaca_paper` | — | merge into `alpaca_paper_list_orders` |
| `alpaca_paper_preview_order` | 0 | D+2 | `alpaca_paper_preview` | — | retire |
| `alpaca_paper_reconcile_orders` | 0 | D+1 | `alpaca_paper_orders` | `market_quote_snapshots`, `alpaca_paper_order_ledger` | keep |
| `alpaca_paper_roundtrip_report` | 0 | D+2 | `alpaca_paper_ledger_read` | `alpaca_paper_order_ledger` | retire |
| `alpaca_paper_submit_order` | 0 | D+2 | `alpaca_paper_orders` | `market_quote_snapshots`, `alpaca_paper_order_ledger` | keep |
| `binance_demo_ledger_status` | 0 | D | `binance_demo_ledger_status_read` | `binance_demo_order_ledger`, `crypto_instruments` | retire |
| `create_paper_account` | 0 | D+1 | `paper_account_registration` | `paper_accounts`, `paper_daily_snapshots`, `paper_positions` **+1** | retire |
| `delete_paper_account` | 0 | D+1 | `paper_account_registration` | `paper_accounts`, `paper_daily_snapshots`, `paper_positions` **+1** | retire |
| `get_mock_loop_retrospective` | 0 | D+6 | `mock_loop_retro_registration` | `investment_watch_alerts`, `investment_watch_events`, `trade_journal_counterfactuals` **+2** | merge into `get_trade_retrospectives` |
| `kis_mock_ledger_expire_day_orders` | 0 | D+1 | — | — | keep (unused; kept flow needs it) |
| `kis_mock_mirror_execute_report` | 0 | D+5 | `mirror_counterfactual_tools` | `investment_report_items`, `investment_reports`, `kis_mock_order_ledger` | merge into `get_trade_retrospectives` |
| `kiwoom_mock_cancel_order` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep (unused; kept flow needs it) |
| `kiwoom_mock_get_order_history` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `kiwoom_mock_get_orderable_cash` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | retire |
| `kiwoom_mock_modify_order` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep (unused; kept flow needs it) |
| `kiwoom_mock_place_order` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | keep |
| `kiwoom_mock_preview_order` | 0 | D+2 | `orders_kiwoom_variants` | `kr_symbol_universe`, `krx_after_market_eligibility`, `upbit_symbol_universe` **+1** | retire |
| `kiwoom_mock_us_cancel_order` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_get_order_history` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_get_orderable_cash` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_get_positions` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_modify_order` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_place_order` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `kiwoom_mock_us_preview_order` | 0 | D+1 | `orders_kiwoom_us_variants` | `us_symbol_universe` | retire |
| `list_paper_accounts` | 0 | D+1 | `paper_account_registration` | `paper_accounts`, `paper_daily_snapshots`, `paper_positions` **+1** | retire |
| `nh_mock_cancel_order` | 0 | D | — | — | retire |
| `nh_mock_get_order_detail` | 0 | D | — | — | retire |
| `nh_mock_get_order_history` | 0 | D | — | — | retire |
| `nh_mock_get_orderable_cash` | 0 | D | — | — | retire |
| `nh_mock_get_positions` | 0 | D | — | — | retire |
| `nh_mock_modify_order` | 0 | D | — | — | retire |
| `nh_mock_place_order` | 0 | D | — | — | retire |
| `nh_mock_preview_order` | 0 | D | — | — | retire |
| `nh_mock_reconcile_orders` | 0 | D | — | — | retire |
| `reset_paper_account` | 0 | D+1 | `paper_account_registration` | `paper_accounts`, `paper_daily_snapshots`, `paper_positions` **+1** | retire |
| `us_dual_paper_account_states` | 0 | D+1 | `us_dual_paper` | — | retire |
| `us_dual_paper_capability_matrix` | 0 | D+1 | `us_dual_paper` | — | retire |
| `us_dual_paper_preview` | 0 | D+1 | `us_dual_paper` | — | retire |

Notes:

- 57 registrations (24% of the surface) serve venues that together produced **~200 calls in 30 days**:
  Kiwoom mock 61, Alpaca paper 72, DB paper 171, KIS mock ≤16, NHPLUG 0, US dual paper 0, Binance demo 0.
- `review.kis_mock_order_ledger` last wrote 2026-08-12 (78 rows); `review.kis_mock_signal_ledger` 2 rows;
  `review.kis_mock_runner_control` is `ACTIVE` since 2026-08-08. The KIS mock venue is armed but idle.
- The H3 closed worlds (`h3-crypto-paper`, `h3-us-paper`) are 20 tools each and are the *entire* MCP surface
  that two deployed units serve. They are in the 30d table (paper 171, alpaca 72) so they are real.
- NHPLUG (9 tools, 0 calls, 0 rows everywhere, `NH_MOCK_MCP_ENABLED` unset) is the clearest full retire:
  code, MCP surface and 7 empty tables.
- Alpaca paper keeps `list_orders` / `list_positions` / `get_account` / `submit_order` / `reconcile_orders` /
  `cancel_order` (used or needed); the 11 automated/ledger/preview/roundtrip tools go.

### 3.6 reports, watches & forecasts (47 tools)

The session's written output: forecasts, retrospectives, investment reports and watches, journal, briefing,
policy and routing. This group merges the brief's "retrospectives/forecasts" with the Hermes report pipeline,
because they share `review.investment_report*`, `review.trade_forecasts`, `review.trade_retrospectives`.

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `route_request` | 1460 | D+9 | `route_request_registration` | `market_quote_snapshots` | keep |
| `forecast_resolve` | 1006 | D+6 | `forecast_tools` | `investment_report_items`, `investment_reports`, `sample_eligibility_decisions` **+1** | keep |
| `forecast_save` | 739 | D+6 | `multiple` | — | keep |
| `session_context_append` | 448 | D+8 | `multiple` | — | keep |
| `investment_watch_events_list_recent` | 245 | D+7 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep |
| `trade_retrospective_pending` | 168 | D+6 | `trade_retrospective_tools` | `paper_trades`, `alpaca_paper_order_ledger`, `kis_live_order_ledger` **+7** | merge into `get_trade_retrospectives` |
| `save_trade_retrospective` | 117 | D+6 | `multiple` | — | keep |
| `get_forecasts` | 107 | D+6 | `forecast_tools` | `investment_report_items`, `investment_reports`, `sample_eligibility_decisions` **+1** | keep |
| `get_trading_policy` | 104 | D+9 | `trading_policy_tools` | — | keep |
| `get_operating_briefing` | 101 | D+8 | `operating_briefing` | `analysis_artifacts`, `investment_report_item_decisions`, `investment_report_items` **+12** | keep |
| `investment_watch_create` | 42 | D+6 | `multiple` | — | keep |
| `session_context_get_recent` | 21 | D+8 | `session_context_tools` | `operator_session_context` | keep |
| `investment_watch_void` | 13 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep |
| `session_bootstrap_pack` | 11 | D+8 | `session_bootstrap_registration` | — | keep |
| `get_forecast_calibration` | 0 | D+6 | `forecast_tools` | `investment_report_items`, `investment_reports`, `sample_eligibility_decisions` **+1** | keep |
| `get_retrospective_aggregate` | 0 | D+6 | `trade_retrospective_tools` | `paper_trades`, `alpaca_paper_order_ledger`, `kis_live_order_ledger` **+7** | keep |
| `get_trade_journal` | 0 | D+6 | `trade_journal_tools` | `trade_journals` | keep |
| `get_trade_retrospectives` | 0 | D+6 | `trade_retrospective_tools` | `paper_trades`, `alpaca_paper_order_ledger`, `kis_live_order_ledger` **+7** | keep |
| `get_trading_scoreboard` | 0 | D+6 | `trading_scoreboard_tools` | `investment_report_items`, `kis_live_order_ledger`, `kis_mock_order_ledger` **+3** | retire |
| `investment_report_activate_watch` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `investment_report_add_items` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_context_get` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_create` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `investment_report_create_from_hermes_composition` | 0 | D+6 | `investment_hermes_handlers` | `investment_reports`, `investment_snapshot_bundles`, `investment_snapshots` **+2** | retire |
| `investment_report_decide_item` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_delta_get` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_generate_from_bundle` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_get` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `investment_report_get_hermes_context` | 0 | D+6 | `investment_hermes_handlers` | `investment_reports`, `investment_snapshot_bundles`, `investment_snapshots` **+2** | retire |
| `investment_report_list` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `investment_report_prepare_bundle` | 0 | D+6 | `investment_hermes_handlers` | `investment_reports`, `investment_snapshot_bundles`, `investment_snapshots` **+2** | retire |
| `investment_report_prepare_intraday_context` | 0 | D+6 | `investment_hermes_handlers` | `investment_reports`, `investment_snapshot_bundles`, `investment_snapshots` **+2** | retire |
| `investment_report_set_status` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_report_update` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | retire |
| `investment_snapshot_bundle_get` | 0 | D+6 | `investment_snapshots_tools` | `investment_snapshot_bundle_items`, `investment_snapshot_bundles`, `investment_snapshot_runs` **+1** | retire |
| `investment_snapshot_bundle_list` | 0 | D+6 | `investment_snapshots_tools` | `investment_snapshot_bundle_items`, `investment_snapshot_bundles`, `investment_snapshot_runs` **+1** | retire |
| `investment_snapshot_list` | 0 | D+6 | `investment_snapshots_tools` | `investment_snapshot_bundle_items`, `investment_snapshot_bundles`, `investment_snapshot_runs` **+1** | retire |
| `investment_stage_artifacts_ingest_from_hermes` | 0 | D+6 | `investment_hermes_handlers` | `investment_reports`, `investment_snapshot_bundles`, `investment_snapshots` **+2** | retire |
| `investment_watch_expire` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `investment_watch_recommend` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `list_active_journals` | 0 | D+6 | `trade_journal_registration` | `trade_journals`, `trades` | merge into `session_context_get_recent` |
| `list_active_watches` | 0 | D+7 | `operating_briefing` | `analysis_artifacts`, `investment_report_item_decisions`, `investment_report_items` **+12** | merge into `investment_watch_events_list_recent` |
| `modify_journal_entry` | 0 | D+6 | `trade_journal_registration` | `trade_journals`, `trades` | retire |
| `save_position_intake_retrospective` | 0 | D | `trade_retrospective_tools` | `paper_trades`, `alpaca_paper_order_ledger`, `kis_live_order_ledger` **+7** | merge into `save_trade_retrospective` |
| `save_trade_journal` | 0 | D+6 | `trade_journal_tools` | `trade_journals` | keep (unused; decide) |
| `sweep_expired_watches` | 0 | D+6 | `investment_reports_handlers` | `investment_report_item_decisions`, `investment_report_items`, `investment_report_news_citations` **+4** | keep (unused; kept flow needs it) |
| `update_trade_journal` | 0 | D+6 | `trade_journal_tools` | `trade_journals` | retire |

Notes:

- `forecast_save` 739 / `forecast_resolve` 1,006 / `get_forecasts` 107 is the second-largest write path.
  `review.trade_forecasts`: 2,445 rows, 1,076 written in the last 30 days on all 31 days, 0 duplicate
  `forecast_id`, 459 rows (19%) with a NULL `decision_bucket` — that NULL rate is the known "unclassified"
  blind spot from ROB-1283, not a new defect.
- `route_request` (1,460 calls) is the lane capability echo. It is 6% of all calls and returns no data — it is
  a *guard*, not a feature. In the target it becomes a core-API call, not a tool.
- `get_operating_briefing` 101 calls at p95 **45 s** is the single slowest tool in the fleet. It assembles the
  briefing, the negative-class guard and the policy stamp. It is a batch job wearing a tool's clothes; the
  target moves it to a precomputed snapshot.
- **`investment_report_*` has not been written since 2026-07-28** (68 reports, 335 items) while
  `investment_watch_events_list_recent` is called 245 times and `review.investment_watch_events` wrote 29 rows
  in 30 days. The watch side is alive; the report compose/ingest side is dead. See $5.
- `trade_journal_*` (200 rows, 49 correlation ids, last write 2026-10-31) and `list_active_journals` are 0-call.
  The journal has no reader; the session writes to `operator_session_context` instead (2,923 rows, 30/30 days).

### 3.7 ops (3 tools)

| tool | 30d | profiles | module | primary tables (static) | proposal |
|---|---:|---|---|---|---|
| `get_parking_exclusion` | 42 | D | `user_settings_tools` | `user_settings` | keep (typed read, #883) |
| `get_user_setting` | 0 | D+5 | `user_settings_tools` | `user_settings` | retire (generic reader) |
| `set_user_setting` | 0 | D+5 | `user_settings_tools` | `user_settings` | retire (generic writer) |

`get_parking_exclusion` is a typed read of one `user_settings` key; the generic pair is a read/write API over
every key, 0 calls, and the only other consumer of `user_settings` is `manual_cash`. Retire the generic pair and
keep the typed read — the operator write path for parking exclusion is then a single documented tool, not a
generic settings surface.

### 3.8 HTTP routers (57 modules, 231 routes)

Callers are in-repo: the `/invest` SPA, the `/trader` page, n8n webhooks, the fillwire/kis ingest daemons, and
operator curl. There is no HTTP access log in the repo, so "usage" here is inferred from the mounted page, the
router's guards and the tables it reads. Gate env values were read from the prod API container (boolean only).

| router | prefix | routes | guard / caller | proposal |
|---|---|---:|---|---|
| `agent_callback.py` | — | 3 | agent callback (token) | keep |
| `alpaca_paper_ledger.py` | /trading | 8 | paper ledger read | keep |
| `candidate_discovery.py` | /api/candidates | 1 | `/invest` / n8n | keep |
| `dependencies.py` | — | 0 | — | keep |
| `deprecated_pages.py` | — | 1 | deprecated redirect | keep |
| `execution_intent_gateway.py` | /execution-intents | 5 | **NOT MOUNTED** — separate loopback process (ROB-1460) | keep (not mounted; separate process) |
| `execution_ledger_ingest.py` | /trading | 2 | **fillwire token ingest** (hard rule 16) | keep (hard rule 16) |
| `health.py` | — | 1 | liveness | keep |
| `invest_api.py` | /invest/api | 34 | `/invest` SPA (session auth) | keep (largest SPA surface) |
| `invest_app_spa.py` | /invest/app | 3 | `/invest/app` shell | keep |
| `invest_artifacts.py` | /trading/api/invest/artifacts | 2 | `/invest` artifacts | keep |
| `invest_buy_plan.py` | /trading/api/invest/buy-plan | 1 | `/invest` buy plan | keep |
| `invest_fills.py` | /trading/api/invest/fills | 5 | `/invest` fills | keep |
| `invest_forecasts.py` | /trading/api/invest/forecasts | 3 | `/invest` forecasts | keep |
| `invest_funding.py` | /invest/api/funding | 8 | `/invest` funding | keep |
| `invest_loss_cut_approvals.py` | /invest/api | 10 | `/trader` loss-cut 2-click (INVEST_LOSS_CUT_APPROVAL_ENABLED) | keep |
| `invest_manual_cash.py` | /invest/api/settings | 2 | `/invest/settings` (admin) | keep |
| `invest_open_orders.py` | /trading/api/invest/open-orders | 1 | `/invest` open orders | keep |
| `invest_protected_positions.py` | /invest/api/settings | 3 | `/invest/settings` (admin) | keep |
| `invest_retrospectives.py` | /trading/api/invest/retrospectives | 4 | `/invest` retrospectives | keep |
| `invest_scalping.py` | /invest/api/scalping | 7 | `/invest` scalping lab | retire (tables unwritten since 2026-07) |
| `invest_session_context.py` | /trading/api/invest/session-context | 1 | `/invest` session context | keep |
| `invest_watches.py` | /trading/api/invest/watches | 1 | `/invest` watches | keep |
| `invest_web_spa.py` | /invest | 3 | `/invest` shell | keep |
| `investment_dimension_reports.py` | /trading/api/investment-reports | 1 | `/invest` dimension reports | retire with the report pipeline |
| `investment_hermes_http.py` | /trading/api/investment-reports/hermes | 6 | Hermes token ingest (gate off) | retire with the report pipeline (gate already off) |
| `investment_reports.py` | — | 13 | `/invest` reports | keep |
| `investment_snapshots.py` | /trading | 3 | `/invest` snapshots | retire with the report pipeline |
| `investment_stage_runs.py` | — | 5 | `/invest` stage runs | retire with the report pipeline |
| `kospi200.py` | /kospi200 | 6 | `/invest` index | retire (table not in prod) |
| `market_calendar.py` | /trading | 1 | `/invest` calendar | keep |
| `market_events.py` | /trading | 5 | `/invest` calendar | keep |
| `mock_auto_read_model.py` | /trading | 0 | no routes (read model only) | retire (0 routes) |
| `news_analysis.py` | /api/v1/news | 7 | n8n | keep |
| `news_issues.py` | /trading | 1 | `/invest` news | keep |
| `news_radar.py` | /trading | 1 | `/invest` news radar | keep |
| `news_relevance.py` | /trading/api/news-relevance | 2 | relevance ingest (token, default off) | keep (token ingest, ROB-491) |
| `ops_task_kick.py` | /trading/api/ops/tasks | 2 | **ops task kick** (token) | keep (operator lever) |
| `order_estimation.py` | /api/symbol-settings | 11 | `/invest` / n8n | keep |
| `order_previews.py` | /trading/api/order-previews | 4 | `/invest` order preview | keep |
| `pagination.py` | — | 0 | — | keep |
| `portfolio_actions.py` | /api/portfolio-actions | 1 | `/invest` | keep |
| `preopen.py` | /trading | 1 | `/invest` preopen | keep |
| `research_pipeline.py` | /api/research-pipeline | 6 | n8n research sessions | retire with the legacy research tables |
| `research_reports.py` | /trading | 3 | news ingestor → AT (token) + `/invest` | retire (0 writes since 2026-05) |
| `research_retrospective.py` | /api/research-retrospective | 3 | n8n | retire with the legacy research tables |
| `research_run_decision_sessions.py` | /trading | 1 | operator research run | keep, verify (single operator route) |
| `screener.py` | — | 10 | n8n + SPA | keep |
| `strategy_events.py` | /trading | 3 | `/invest` | keep |
| `symbol_settings.py` | /api/symbol-settings | 5 | `/invest` settings | keep |
| `telegram_callback.py` | /trading/api/telegram | 1 | Telegram webhook (W5, gates off) | keep (W5, gates off) |
| `test.py` | /api/test | 6 | test-only (gated) | retire (test-only routes) |
| `trade_journals.py` | /api/trade-journals | 4 | — | keep |
| `trader_page.py` | /trading/api/trader | 5 | `/trader` approval inbox (session) | keep |
| `trader_spa.py` | /trader | 2 | `/trader` shell | keep |
| `user_defaults.py` | /api/symbol-settings | 2 | `/invest` settings | keep |
| `websocket.py` | — | 1 | websocket endpoint | keep |

Retire list from this table: 8 router modules (`investment_hermes_http`, `investment_stage_runs`,
`investment_dimension_reports`, `investment_snapshots`, `research_pipeline`, `research_retrospective`,
`research_reports`, `invest_scalping`, `kospi200`, `test`, `mock_auto_read_model`) = 11 modules, ~45 routes.
`invest_api.py` alone is 34 routes and carries the whole `/invest` product; it should be split per domain before
any of it moves.

### 3.9 Scheduled jobs

Three schedulers run in prod: one TaskIQ worker (`at-worker`, `--workers 1`) with an in-process scheduler, one
`at-scheduler` container, and 30 desk-owned systemd timers on NCP. Prefect runs 7 flows in `at-prefect-server`
and Dagu has 4 DAGs that are **not registered**.

| job | kind | schedule | writes | 30d evidence | proposal |
| `analyst_consensus_snapshot_tasks.py` | TaskIQ | manual only (no schedule label) | `analyst_consensus_snapshots` | **table EMPTY** | retire (see $5) |
| `binance_demo_root_reservation_reconcile_tasks.py` | TaskIQ | manual only | `binance_demo_order_ledger` | 43 write-days | keep, decide (venue idle) |
| `crypto_pending_order_alert_tasks.py` | TaskIQ | cron 08:30 / 22:00 KST | notifications | — | keep |
| `daily_candles_tasks.py` | TaskIQ | cron 16:30 KR / 07:00 US / 09:00 crypto | `*_candles_1d` | 19-30 write-days | keep |
| `daily_scan_tasks.py` | TaskIQ | manual only | scanner output | — | keep |
| `execution_ledger.py` | TaskIQ | cron (gate on) | `review.execution_ledger_reconcile_runs` | 2,860 runs / 31 days | keep (hard rule 16) |
| `intraday_order_review_tasks.py` | TaskIQ | cron 10:00/14:00 KR, 21:00/00:30 US, 14:00/00:00 crypto | notifications | — | keep |
| `invest_momentum_event_tasks.py` | TaskIQ | cron (gate on) | `public.invest_momentum_event_snapshots` | 21 write-days, 348k rows | keep, restructure (1.3 GB) |
| `invest_screener_snapshot_tasks.py` | TaskIQ | 6 crons 07:40/16:20/20:20 KST, 17:20 ET | `public.invest_screener_snapshots` | 25 write-days, 207k rows | keep |
| `investor_flow_snapshot_tasks.py` | TaskIQ | cron (gate on) | `public.investor_flow_snapshots` | 19 write-days | keep |
| `journal_counterfactual_tasks.py` | TaskIQ | manual only | `review.trade_journal_counterfactuals` | **table EMPTY, 0 writes** | retire |
| `journal_verdict_tasks.py` | TaskIQ | manual only | `review.trade_journal*` | journal 0-call readers | retire with the journal |
| `kis_live_reconcile_tasks.py` | TaskIQ | cron | `review.kis_live_order_ledger` | 44 rows / 14 days | keep |
| `kis_mock_reconciliation_tasks.py` | TaskIQ | cron | `review.kis_mock_order_ledger` | **0 writes in 30d** (last 2026-08-12) | keep, verify (venue armed but idle) |
| `kr_candles_tasks.py` | TaskIQ | cron */10 (gate on) | `public.kr_candles_1m` | 414k rows / 19 days | keep |
| `kr_symbol_universe_tasks.py` | TaskIQ | cron 07:10 KST | `public.kr_symbol_universe` | 3,967 rows / 11 days | keep |
| `live_reconcile_tasks.py` | TaskIQ | cron | `review.live_order_ledger` | 33 rows / 15 days | keep |
| `market_quote_snapshot_tasks.py` | TaskIQ | manual only | `public.market_quote_snapshots` | **7 rows total** | retire (see $5) |
| `market_valuation_snapshot_tasks.py` | TaskIQ | cron (gate on) | `public.market_valuation_snapshots` | 419k rows / 25 days | keep, restructure (duplicates) |
| `mechanical_loss_cut_tasks.py` | TaskIQ | manual only | `review.lot_exit_plans`, `..._decisions` | 75 + 22 rows, 1 day | keep (shadow, ROB-1460) |
| `mock_roundtrip_journal_tasks.py` | TaskIQ | manual only | journal bridge | — | retire with the journal |
| `news_relevance_judgment_tasks.py` | TaskIQ | manual only | `public.symbol_news_relevance` | 1,599 rows / 26 days (written by the external job) | keep |
| `order_proposal_expiry_tasks.py` | TaskIQ | night sweep 16:30/07:00 KST (gate on) | `review.order_proposals` | 490 expired | keep |
| `paper_cohort_tasks.py` | TaskIQ | cron when `PAPER_COHORT_ENABLED` (**unset**) | `research.paper_cohort_*` | **all EMPTY** | retire |
| `pending_orders.py` | TaskIQ | manual only | `public.pending_orders` | **EMPTY** | retire |
| `protected_position_tasks.py` | TaskIQ | manual only (desk timer) | `review.protected_positions` | 2 rows / 0 writes | keep |
| `quotes_consumer_tasks.py` | TaskIQ | manual only | `review.quotes_trigger_firings` | **EMPTY** | retire (with #1120 shadow) |
| `research_reports_ingest_tasks.py` | TaskIQ | manual only | `public.research_reports` | 0 writes in 30d | retire |
| `research_run_refresh_tasks.py` | TaskIQ | 8 crons 08:10-19:55 KST | `public.research_runs` | **0 writes in 30d** (table 1 row) | retire (see $5) |
| `telegram_callback_inbox_tasks.py` | TaskIQ | recovery cron when gate on (off) | `review.telegram_callback_inbox` | **EMPTY** | keep (dormant, W5) |
| `toss_live_reconcile_tasks.py` | TaskIQ | poll cron when `TOSS_FILL_POLL_ENABLED` (unset) | `review.toss_live_order_ledger` | 192 rows / 21 days | keep |
| `toss_manual_activity_tasks.py` | TaskIQ | manual only | `review.toss_manual_activity_alerts` | **EMPTY** | keep, verify |
| `toss_warnings_sync_tasks.py` | TaskIQ | manual only (desk timer) | `public.kr_stock_warnings` | 0 rows (no warnings) | keep |
| `upbit_symbol_universe_tasks.py` | TaskIQ | cron 06:15 | `public.upbit_symbol_universe` | 32 rows / 11 days | keep |
| `us_candles_tasks.py` | TaskIQ | cron */10 | `public.us_candles_1m` | 134k rows / 26 days | keep |
| `us_symbol_universe_tasks.py` | TaskIQ | cron 21:30 | `public.us_symbol_universe` | 10,842 rows / 21 days | keep |
| `watch_expiry_tasks.py` | TaskIQ | manual only | `review.investment_watch_events` | 29 rows / 15 days | keep |
| `watch_follow_up_tasks.py` | TaskIQ | manual only | watch follow-up | — | retire with the journal |
| `watch_scan_tasks.py` | TaskIQ | cron */5 | `review.investment_watch_alerts` | 67 rows / 21 days | keep |
| `watch_validity_review_tasks.py` | TaskIQ | manual only | watches | — | keep |
| `forexfactory_calendar_flow.py` | Prefect | unregistered | `public.market_events` (economic) | 5,065 rows | keep |
| `invest_crypto_insight_snapshots_flow.py` | Prefect | unregistered | `public.crypto_insight_snapshots` | **18 rows, last 2026-09-02** | retire |
| `invest_crypto_screener_snapshots_flow.py` | Prefect | unregistered | `public.invest_crypto_screener_snapshots` | 6,000 rows / 30 days | keep |
| `invest_kr_fundamentals_snapshots_flow.py` | Prefect | unregistered | `public.invest_kr_fundamentals_snapshots` | 94,832 rows / 22 days | keep |
| `invest_screener_snapshots_us_flow.py` | Prefect | unregistered | `public.invest_screener_snapshots` | US 27 write-days | keep |
| `investment_snapshots_refresh_flow.py` | Prefect | unregistered | `review.investment_snapshot*` | **all EMPTY** | retire with the report pipeline |
| `market_close_digest_flow.py` | Prefect | unregistered | digest notification | — | keep, decide |

**Dagu** (`deploy/dagu/`): `data-plane-kr.yaml`, `data-plane-us.yaml`, `data-plane-crypto.yaml`,
`paper-forward.yaml` — all four are proposals, explicitly unregistered (see
`docs/research/data-plane-contract-v1.md`, `docs/runbooks/paper-forward.md`). They point at the research CLIs in
`research/data_plane` and `research/paper_forward`, which have **no DB and no scheduler connection**. Keep as
research surface; do not register without approval (hard rule 6).

**Desk timers** (`ops/ncp/systemd/*.timer`, 30): 14 `job-kickoff-*` lane nudges, `at-pg-backup`,
`at-mcp-watchdog`, `fill-event-handoff`, 4 snapshot jobs, `b0x-lane-event-consumer` and 8 others. They are
outside this repo's scheduler and are listed only so a retirement PR does not orphan a timer that still fires.
The `job-kickoff-*` family drives the operator lanes (b0x, nxt, crypto, us) and is the live "operator
orchestration" surface, not part of the MCP product.

### 3.10 CLI scripts (181)

Grouped by purpose; every script in `scripts/*.py` appears exactly once. Scripts are manual levers or CI tools;
none is on a schedule by itself.

**broker smoke & execution levers** (38): `alpaca_paper_capability_probe`, `backfill_alpaca_paper_execution_asset_class`, `binance_demo_strategy_loop`, `binance_futures_demo_smoke`, `binance_h5_demo`, `binance_h5_heartbeat_watch`, `binance_h5_truth_gate`, `binance_h5_weekly_score`, `binance_public_smoke`, `binance_spot_demo_d2_remediation`, `binance_spot_demo_d2_root_reconcile`, `binance_spot_demo_smoke`, `expire_kis_mock_rows_by_inference`, `kis_live_auto_reconcile`, `kis_live_witness_reconcile`, `kis_mock_execution_consumer`, `kis_mock_fill_evidence_smoke`, `kis_mock_holdings_delta_smoke`, `kis_mock_open_order_probe`, `kis_mock_overseas_holdings_delta_smoke`, `kis_mock_runner`, `kis_mock_scalping_daemon`, `kis_mock_scalping_ws_smoke`, `kis_mock_us_cash_probe`, `kis_websocket_mock_smoke`, `kiwoom_collect_kr_daily`, `kiwoom_live_readonly_compare`, `kiwoom_mock_smoke`, `kiwoom_mock_us_smoke`, `mock_session_mcp`, `mock_strategy`, `mock_strategy_crypto`, `nhplug_collect_period`, `nhplug_mock_smoke`, `nhplug_t14_host_witness`, `run_b0x_kr_kiwoom_cycle`, `toss_fill_reconcile_poller`, `toss_live_smoke`

**data collection & diagnostics** (49): `alpaca_collect_us_daily`, `backfill_daily_candles`, `backfill_dart_disclosures`, `backfill_kr_candles_1m_toss`, `backfill_news_related_symbols`, `build_analyst_consensus_snapshots`, `build_crypto_insight_snapshots`, `build_financial_fundamentals_snapshots`, `build_invest_crypto_screener_snapshots`, `build_invest_kr_fundamentals_snapshots`, `build_invest_screener_snapshots`, `build_investor_flow_snapshots`, `build_market_quote_snapshots`, `build_market_valuation_snapshots`, `build_policy_table`, `build_support_proximity_snapshot`, `build_us_fundamentals_snapshots`, `diagnose_calendar_coverage`, `diagnose_invest_screener_snapshots`, `diagnose_invest_screener_toss_parity`, `diagnose_kr_dividend_conditions`, `diagnose_negative_class_recording`, `diagnose_research_reports`, `ingest_freqtrade_report`, `ingest_market_events`, `ingest_research_reports`, `ingest_validated_run_card`, `list_rob900_toss_projection_backfill_candidates`, `live_reconcile_backfill_report`, `news_feed_readonly_smoke`, `news_issue_lab`, `news_issue_lab_quality_eval`, `news_quality_baseline`, `news_relevance_judge_smoke`, `r4_p0_seed_backfill`, `run_research_run_refresh`, `shadow_new_candidates_report`, `sync_journal_counterfactuals`, `sync_journal_verdicts`, `sync_kr_candles`, `sync_kr_symbol_universe`, `sync_mock_roundtrip_journals`, `sync_toss_symbol_master`, `sync_toss_warnings`, `sync_upbit_symbol_universe`, `sync_us_candles`, `sync_us_common_stock_flags`, `sync_us_symbol_universe`, `sync_watch_follow_up_items`

**ledger, guards & operator levers** (39): `b0x_lane_event_consumer`, `b0x_lane_event_dispatcher`, `b0x_lane_event_poller`, `export_dots_history`, `fill_event_handoff`, `fill_handoff_bundle`, `fill_watch_context_consumer`, `fill_watch_context_shadow_replay`, `forecast_legacy_rule_migration`, `import_krx_after_market_eligibility`, `krb1_c_stress_independent_verify`, `krb1_c_stress_reducer`, `krb1_p0_journal`, `krb1_p0_liquidity_selector`, `krb1_p0_smoke`, `lane_event_kickoff`, `list_recent_fill_events`, `mechanical_loss_cut`, `promote_kr_candles_to_research`, `protected_positions`, `quarantine_execution_ledger_rows`, `reconcile_execution_ledger`, `record_strategy_learning_event`, `retrospective_action_cutover`, `rob1284_resting_rung_sweep`, `rob596_orderable_diagnostic`, `rob837_reconcile_upbit_proposal`, `run_b0x_cancel`, `run_b0x_cycle`, `run_b0x_kr_cycle`, `run_b0x_us_cycle`, `run_execution_intent_gateway`, `run_posture_shadow`, `seed_execution_ledger_opening_lots`, `shadow_replay`, `shadow_replay_probe`, `strategy_sleeves`, `three_market_shadow_once`, `us_regular_session_raw_capture`

**ops & CI** (15): `call_durations`, `call_durations_test`, `cio_quality_gate`, `deploy_ci_gate`, `devkit_healthcheck`, `invest_perf_report`, `mcp_profile_import_rss`, `mcp_tool_usage_audit`, `merge_test_durations`, `merge_test_durations_test`, `ncp_job_timers_check`, `ncp_job_timers_golden`, `ncp_mcp_watchdog`, `perf_compare_sentry`, `sentry_profiling_canary`

**other** (27): `attribute_daily_spikes`, `cleanup_toss_manual_holdings`, `debug_economic_calendar`, `fix_overseas_exchange_codes`, `free_social_sources_probe`, `hermes_roundtrip_smoke`, `invest_reports_us_schedule`, `kis_lean_once`, `kis_overseas_premarket_probe`, `kis_overseas_price_smoke`, `list_recent_watch_events`, `make_dev_seed`, `measure_calendar_endpoint`, `post_watch_triage_reply`, `precompute_spike_attribution`, `probe_us_earnings_coverage`, `quote_parity_shadow_probe`, `remote_debug_audit_smoke`, `review_active_watches`, `run_crypto_pending_order_alert`, `run_kr_stageb`, `run_market_close_digest`, `run_us_upside_instrumentation`, `snapshot_bundle_smoke`, `test_discord_webhook_e2e`, `toss_auto_acceptance_fixture`, `websocket_healthcheck`

**research replays** (13): `r4_p0_collector`, `r4_p0_readiness`, `r4_p0_watchdog`, `replay_cr_s1_etr`, `replay_cr_s1_tpr_ceb`, `resynthesize_cr_s1_b2`, `resynthesize_cr_s1_b2_correction`, `rob178_smoke`, `rob278_kr_dryrun`, `run_d3_calibration`, `run_d3_primary`, `run_rob1040_crs24_refreeze`, `run_rob974_r2_campaign`

Proposal for this surface: the 181 scripts are **not** the product, but ~40 of them are load-bearing operator
levers (the smokes, `quarantine_execution_ledger_rows`, `export_dots_history`, `strategy_sleeves`,
`mechanical_loss_cut`, `binance_h5_truth_gate`, `deploy_ci_gate`). The other ~140 are one-off diagnostics and
research replays. A rebuilt AT should keep the levers and move the diagnostics to `research/` or delete them;
the schema-retire PRs in $7 should delete the script that exists only to service a retired table
(e.g. `sync_toss_warnings.py` keeps, `cleanup_toss_manual_holdings.py` and `fix_overseas_exchange_codes.py` go
with their tables).

### 3.11 Service packages (73 packages, 1,058 files, 320,943 LOC)

`app/services` is 70% of the app. The distribution is the argument for the target architecture:

| package | files | role | proposal |
|---|---:|---|---|
| `brokers` | 192 | 10 broker clients (binance 80, kis 46, kiwoom 18, toss 11, nhplug 10, alpaca 10, paper 5, upbit 4, yahoo 2) | keep KIS/Toss/Upbit/Yahoo/Alpaca/paper; drop binance(80 files, idle)+nhplug(10) → **-90 files** |
| `order_proposals` | 49 | proposal lifecycle, approval window, callback inbox, auto-approve | keep (core write path) |
| `invest_view_model` | 48 | `/invest` read models for every SPA panel | keep; split by panel before moving |
| `action_report` | 48 | snapshot-backed report generation | retire with the report pipeline |
| `investment_reports` | 27 | Hermes report bundle/ingest/query | retire with the report pipeline |
| `market_events` | 25 | event ingest/normalize/query | keep |
| `investment_stages` | 19 | stage artifacts for Hermes | retire |
| `watch_trigger_repricing` | 18 | watch repricing claims (0 rows) | retire |
| `trade_journal` | 17 | journal CRUD (0 readers) | retire |
| `kis_mock_runner` | 13 | KIS mock execution + evidence | keep, decide (venue armed, idle) |
| `buy_gate_ab_shadow` | 13 | A/B shadow (0 rows) | retire or keep as observation |
| `execution_ledger` | 12 | fill ingest, supersede, quarantine, lots | **keep (core)** |
| `spike_attribution` | 11 | spike cause attribution (0 calls) | retire |
| `paper_cohort` | 11 | cohort runner (all tables empty) | retire |
| `naver_finance` | 11 | Naver scrape/parse | keep |
| `upbit_public_read_model` | 10 | Upbit read model | keep |
| `invest_screener_snapshots` | 10 | snapshot builder/reader | keep |
| `funding_advisory` | 10 | funding advisory (dormant since 08-19) | retire or keep, decide |
| `daily_candles` | 10 | daily candle fetch | keep |
| `strategy_sleeves` | 9 | ROB-1470 sleeves | keep |
| `nhplug_mock` | 9 | NHPLUG mock stage 2 | retire |
| `mock_strategy` | 9 | ROB-1474 paper strategies | keep (H3 venues) |
| `market_close_digest` | 9 | digest notification | keep |
| `investment_snapshots` | 9 | snapshot bundles (empty) | retire |
| `mechanical_loss_cut` | 8 | ROB-1460 shadow | keep |
| `invalid_sample_eligibility` | 8 | sample eligibility (empty) | retire |
| `fill_watch_context` | 8 | #137 context artifacts (empty) | retire |
| `fill_event_handoff` | 8 | fill → session context | keep |
| `us_dual_paper` | 7 | US dual paper (0 calls) | retire |
| `social_sentiment_probe` | 7 | social probe | keep |
| `quotes_consumer` | 7 | #1120 shadow consumer | keep as shadow |
| `market_valuation_snapshots` | 7 | valuation snapshot | keep, restructure |
| `kis_websocket_internal` | 7 | KIS ws decode (fillwire parity) | keep (or delete with at-kis-ws) |
| `investment_dimensions` | 7 | dimension reports (0 writes) | retire |
| `invest_crypto_screener_snapshots` | 7 | crypto screener | keep |
| `symbol_analysis` | 6 | symbol analysis helpers | keep |
| `invest_momentum_events` | 6 | momentum events | keep |
| `financial_fundamentals_snapshots` | 6 | fundamentals snapshot (empty) | retire |
| `external` | 6 | external data adapters | keep |
| *other 33 packages* | *354* | smaller services (disclosures, dots_history_export, execution_outcome intent gateway, analyst_consensus_snapshots, crypto_insight_snapshots, market_data_coverage, minute_candles, mock_integration, naver_stock, ops_task_kick, paper_evaluation, paper_validation, research_candles, research_reports, scalping_reviews, screener_evidence, shadow_replay, strategy_sleeves, toss_consumer, trader_page, …) | per-table proposal in $4 |

Deleting the retired packages listed above removes roughly **295 of 1,058 service files** without touching
`brokers` (except binance/nhplug), `order_proposals` or `execution_ledger`.

## 4. Schema inventory

Source: 199 SQLAlchemy models with `__tablename__` (AST over `app/models/`) plus 12 tables that exist in the
production database with no model (candle stores and lab tables created by raw migration SQL). The prod DB has
**207** tables in `public` (84), `review` (92), `research` (26) and `paper` (5); the extra 16 in `pg_tables` are
TimescaleDB internals and are excluded.

Columns:

- **rows** — exact `count(*)` (bounded, `statement_timeout` 8 s; every query completed).
- **last write** — `max()` of the table's strongest timestamp column.
- **30d d / 30d rows** — distinct KST dates with a write in the last 30 days, and rows written.
- **dupes** — measured, aggregate only.
- **readers** — number of non-model, non-test `app/` + `scripts/` files that mention the model class or the
  table name (static; a `__init__` re-export is excluded).
- **proposal** — keep / keep (evidence only) / keep, fix / restructure / retire.

`—` in a numeric cell means the table was empty and the value is not meaningful.

### 4.1 Retire candidates (103 tables)


103 tables. 73 of them are empty; the rest hold 16,450 rows that would need an export before a drop.

| table | rows | last write | 30d d | readers | note |
|---|---:|---|---:|---:|---|
| `public.news_ingestion_runs` | 7,129 | 2026-09-30 | 0 | 3 | 0 writes in 30d; superseded by market_events partitions |
| `public.stock_analysis_results` | 2,413 | — | 0 | 6 | legacy, 0 writes |
| `public.stage_analysis` | 2,270 | 2026-06-01 | 0 | 5 | 0 writes since 2026-06-01 |
| `public.summary_stage_links` | 2,088 | 2026-06-01 | 0 | 4 | 0 writes since 2026-06-01 |
| `public.research_sessions` | 735 | 2026-06-01 | 0 | 8 | 0 writes since 2026-06-01 |
| `public.research_summaries` | 673 | 2026-06-01 | 0 | 7 | 0 writes since 2026-06-01 |
| `review.investment_report_items` | 335 | 2026-07-28 | 0 | 20 | no writes since 2026-07-28 |
| `public.scalp_trade_analytics` | 206 | 2026-07-20 | 0 | 4 | 0 writes since 2026-07-20 |
| `public.stock_info` | 176 | — | 0 | 18 | legacy analysis master, 0 writes |
| `review.investment_stage_artifacts` | 86 | 2026-06-09 | 0 | 3 | no writes since 2026-06-09 |
| `review.investment_reports` | 68 | 2026-07-28 | 0 | 51 | no writes since 2026-07-28 |
| `review.investment_report_item_decisions` | 62 | 2026-07-28 | 0 | 6 | no writes since 2026-07-28 |
| `public.prompt_results` | 36 | 2026-01-05 | 0 | 0 | 0 writes since 2026-01-05 |
| `review.investment_symbol_intermediate_reports` | 36 | 2026-06-09 | 0 | 2 | no writes since 2026-06-09 |
| `public.crypto_insight_snapshots` | 18 | 2026-09-02 | 0 | 6 | 18 rows, last 2026-09-02 |
| `public.symbol_trade_settings` | 17 | 2026-02-04 | 0 | 6 | 0 writes since 2026-02-04 |
| `review.investment_stage_runs` | 17 | 2026-06-09 | 0 | 6 | no writes since 2026-06-09 |
| `public.research_run_candidates` | 16 | 2026-04-28 | 0 | 4 | 0 writes in 30d |
| `review.investment_dimension_reports` | 16 | 2026-06-09 | 0 | 3 | no writes since 2026-06-09 |
| `public.profile_change_log` | 14 | 2026-03-13 | 0 | 0 | 0 writes since 2026-03-13 |
| `public.asset_profiles` | 12 | 2026-03-13 | 0 | 0 | empty, trade_profile dead |
| `public.research_reports` | 10 | 2026-05-25 | 0 | 20 | 0 writes in 30d; ingest is manual |
| `public.market_quote_snapshots` | 7 | 2026-10-05 | 1 | 15 | 7 rows; no kept reader needs it |
| `public.portfolio_decision_runs` | 3 | — | 0 | 0 | 3 rows, 0 writes in 30d |
| `public.scalping_daily_reviews` | 2 | 2026-05-26 | 0 | 2 | 2 rows, 2026-05 |
| `public.market_reports` | 1 | 2026-04-21 | 0 | 2 | 1 row |
| `public.research_report_ingestion_runs` | 1 | 2026-05-25 | 0 | 5 |  |
| `public.research_runs` | 1 | 2026-04-28 | 0 | 11 | 1 row |
| `public.sell_conditions` | 1 | 2026-04-16 | 0 | 3 | 1 row |
| `review.nhplug_mock_key_version` | 1 | 2026-09-29 | 0 | 1 | nhplug venue unused |
| `paper.paper_daily_snapshots` | 0 | — | — | 2 | empty |
| `public.analyst_consensus_snapshots` | 0 | — | — | 7 | EMPTY; builder has no schedule, no reader |
| `public.crypto_instrument_health` | 0 | — | — | 5 | empty |
| `public.exchanges` | 0 | — | — | 15 | empty |
| `public.financial_fundamentals_snapshots` | 0 | — | — | 7 | EMPTY; MCP reads live yfinance/DART |
| `public.instruments` | 0 | — | — | 67 | empty |
| `public.market_filters` | 0 | — | — | 1 | empty |
| `public.news_analysis_results` | 0 | — | — | 4 | empty |
| `public.order_execution_request` | 0 | — | — | 2 | empty |
| `public.order_preview_leg` | 0 | — | — | 2 | empty |
| `public.order_preview_session` | 0 | — | — | 3 | empty |
| `public.pending_orders` | 0 | — | — | 32 | empty; live per-venue ledgers replaced it |
| `public.research_run_pending_reconciliations` | 0 | — | — | 3 | 0 rows |
| `public.scalping_review_actions` | 0 | — | — | 2 | empty |
| `public.stock_aliases` | 0 | — | — | 3 | empty |
| `public.tier_rule_params` | 0 | — | — | 0 | empty, trade_profile dead |
| `public.user_channels` | 0 | — | — | 0 | empty |
| `public.user_research_notes` | 0 | — | — | 0 | empty |
| `public.user_trade_defaults` | 0 | — | — | 3 | empty |
| `public.user_watch_items` | 0 | — | — | 3 | empty |
| `research.backtest_pairs` | 0 | — | — | 1 | empty |
| `research.backtest_runs` | 0 | — | — | 10 | empty |
| `research.canonical_market_snapshots` | 0 | — | — | 5 | empty |
| `research.evaluation_configs` | 0 | — | — | 9 | empty |
| `research.evaluation_epochs` | 0 | — | — | 1 | empty |
| `research.evaluation_scorecards` | 0 | — | — | 1 | empty |
| `research.evaluation_verdicts` | 0 | — | — | 1 | empty |
| `research.kr_candle_promotion_conflicts` | 0 | — | — | None | empty |
| `research.kr_candle_promotion_watermark` | 0 | — | — | None | empty |
| `research.kr_candles_1m` | 0 | — | — | None | empty |
| `research.paper_cohort_decisions` | 0 | — | — | 6 | empty |
| `research.paper_cohort_run_claims` | 0 | — | — | 3 | empty |
| `research.paper_cohort_target_reservations` | 0 | — | — | 1 | empty |
| `research.paper_cohort_terminal_fences` | 0 | — | — | 4 | empty |
| `research.paper_cohort_venue_intents` | 0 | — | — | 4 | empty |
| `research.paper_run_order_links` | 0 | — | — | 4 | empty |
| `research.paper_validation_cohort_assignments` | 0 | — | — | 6 | empty |
| `research.paper_validation_cohorts` | 0 | — | — | 6 | empty |
| `research.paper_validation_postmortem_reviews` | 0 | — | — | 1 | empty |
| `research.paper_validation_state_transitions` | 0 | — | — | 6 | empty |
| `research.promotion_candidates` | 0 | — | — | 2 | empty |
| `research.strategy_experiments` | 0 | — | — | 9 | empty |
| `research.strategy_hypothesis_drafts` | 0 | — | — | 1 | empty |
| `research.strategy_learning_events` | 0 | — | — | 1 | empty |
| `research.sync_jobs` | 0 | — | — | 1 | empty |
| `review.fill_watch_context_outcomes` | 0 | — | — | 5 | empty |
| `review.funding_advisory_deliveries` | 0 | — | — | 1 | empty |
| `review.funding_advisory_proposal_links` | 0 | — | — | 1 | empty |
| `review.funding_advisory_revisions` | 0 | — | — | 1 | empty |
| `review.investment_report_news_citations` | 0 | — | — | 3 | empty |
| `review.investment_report_news_fetch_runs` | 0 | — | — | 1 | empty |
| `review.investment_snapshot_bundle_items` | 0 | — | — | 1 | empty |
| `review.investment_snapshot_bundles` | 0 | — | — | 2 | empty |
| `review.investment_snapshot_runs` | 0 | — | — | 1 | empty |
| `review.investment_snapshots` | 0 | — | — | 21 | empty |
| `review.ladder_touch_events` | 0 | — | — | 1 | empty |
| `review.nhplug_mock_account_binding` | 0 | — | — | 1 | empty |
| `review.nhplug_mock_account_ref` | 0 | — | — | 1 | empty |
| `review.nhplug_mock_operator_authorization` | 0 | — | — | 1 | empty |
| `review.nhplug_mock_order_ledger` | 0 | — | — | 1 | empty |
| `review.nhplug_no_order_proof_code` | 0 | — | — | 1 | empty |
| `review.nhplug_success_proof_code` | 0 | — | — | 1 | empty |
| `review.order_proposal_loss_cut_scopes` | 0 | — | — | 2 | empty |
| `review.pending_snapshots` | 0 | — | — | 2 | empty |
| `review.quotes_trigger_firings` | 0 | — | — | 1 | empty |
| `review.toss_fill_poll_state` | 0 | — | — | 1 | empty; TOSS_FILL_POLL_ENABLED unset |
| `review.toss_manual_activity_alerts` | 0 | — | — | 1 | empty |
| `review.trade_journal_counterfactuals` | 0 | — | — | 3 | 0 writes in 30d |
| `review.trade_journal_reviews` | 0 | — | — | 3 | empty |
| `review.trade_reviews` | 0 | — | — | 3 | empty |
| `review.trade_snapshots` | 0 | — | — | 0 | empty |
| `review.watch_event_repricing_claims` | 0 | — | — | 1 | empty; watch_repricing profile has 18 tools, 0 calls |
| `review.watch_order_intent_ledger` | 0 | — | — | 1 | empty |

The three retire families worth calling out:

1. **The legacy analysis/research pipeline** — `stock_info` (176), `stock_analysis_results` (2,413),
   `stage_analysis` (2,270), `summary_stage_links` (2,088), `research_sessions` (735), `research_summaries`
   (673), `research_runs`, `prompt_results`, `sell_conditions`, `scalp_trade_analytics`,
   `scalping_daily_reviews`. Every one of them stopped writing on or before **2026-06-01**, and the MCP tools
   that read them (`research_session_*`, `stage_analysis_get`, `research_summary_get`) have 0 calls. This is the
   single biggest and cleanest drop: ~11 tables, ~11k rows.
2. **Unused venues** — the 6 `review.nhplug_*` tables (all empty, `NH_MOCK_MCP_ENABLED` unset), the 6
   `research.paper_cohort_*` + 5 `research.paper_validation_*` tables (all empty, `PAPER_COHORT_ENABLED`
   unset), `review.trade_journal_*` (0 rows/0 writes), `review.watch_*` and `review.trade_*review*` (0 rows).
3. **The Hermes report write side** — `review.investment_reports` (68), `investment_report_items` (335),
   `investment_report_item_decisions` (62), the 4 `investment_stage_*`/`investment_snapshot_*` tables. No write
   since **2026-07-28**, 0 tool calls. **This one needs an operator decision**, because the report schema is
   the contract Hermes writes into; see $5.3.

Dropping all 103 would remove **about 60 tables that are already empty** and 43 that hold data. Ordering and
export requirements are in $7.

### 4.2 Restructure candidates (11 tables)

These are kept, loaded daily, and wrong in a way that costs space or correctness. All of them are the DB's
largest tables: together they are **2.7 GB of the 4.4 GB database**.


| table | rows | size | 30d d | why restructure |
|---|---:|---:|---:|---|
| `public.invest_momentum_event_snapshots` | 745,246 | 1,295 MB | 21 | hypertable on trading_date + retention (1.3 GB, 48 rows per symbol-day) |
| `public.invest_screener_snapshots` | 436,622 | 351 MB | 25 | hypertable/partition on snapshot_date + retention (351 MB) |
| `public.market_valuation_snapshots` | 830,974 | 341 MB | 25 | dedupe (161,897 duplicate rows over 669,077 keys) + UNIQUE (market,snapshot_date,symbol,source) + hypertable/retention |
| `public.market_events` | 252,327 | 199 MB | 31 | add UNIQUE (source,source_event_id); 15,812 duplicate earnings rows in the last window |
| `public.invest_theme_event_snapshots` | 188,200 | 167 MB | 21 | hypertable on trading_date + retention (167 MB) |
| `public.news_articles` | 123,777 | 167 MB | 26 | hypertable/partition on article_published_at + retention (167 MB) |
| `public.investor_flow_snapshots` | 236,087 | 127 MB | 19 | hypertable on snapshot_date + retention (127 MB) |
| `public.invest_kr_fundamentals_snapshots` | 185,175 | 113 MB | 22 | hypertable on snapshot_date + retention (113 MB) |
| `public.news_article_related_symbols` | 20,929 | 9.4 MB | 0 | merge with symbol_news_relevance (two relevance tables) |
| `review.execution_ledger` | 5,537 | 8.4 MB | 23 | hypertable/index on filled_at once >10M rows; today 5,537 rows needs nothing |
| `public.market_event_values` | 26,722 | 5.6 MB | 16 | hypertable on the event date + retention |

Two structural notes:

- **TimescaleDB is already in the repo** — 8 hypertables exist (`public`/`research` candle stores). The eight
  large snapshot tables above are *not* hypertables, so they grow forever with no retention. Making them
  hypertables on their date column plus a retention policy is a mechanical change, and it is the single
  highest-value schema change for size.
- **`market_valuation_snapshots` has no unique key.** 830,974 rows hold 669,077 distinct
  `(market, snapshot_date, symbol)`; each source run re-inserts. A unique key + upsert would cut 24% of the
  table and make the reader deterministic. `market_events` has the same disease on earnings (35% dupes).

### 4.3 Keep (93 tables)

Kept tables with health columns. "30d d" is the number of distinct days in the last 30 with at least one write;
compare it with **expected cadence** (shown per group).


| table | rows | last write | 30d d | 30d rows | dupes | readers | verdict |
|---|---:|---|---:|---:|---|---:|---|
| **Market data — daily/session snapshots (expect ≥20 trading days in 30)** | | | | | | | |
| `public.kr_symbol_universe` | 4,049 | 2026-11-01 | 11 | 3,967 | — | 33 | keep |
| `public.us_symbol_universe` | 14,188 | 2026-11-02 | 21 | 10,842 | — | 29 | keep |
| `public.upbit_symbol_universe` | 928 | 2026-10-27 | 11 | 32 | — | 14 | keep |
| `public.crypto_instruments` | 320 | 2026-10-28 | 11 | 13 | — | 15 | keep |
| `public.kr_stock_warnings` | 0 | — | — | — | — | 1 | keep (empty = no active warnings; sync task is scheduleless) |
| `public.krx_after_market_eligibility` | 2,766 | 2026-09-29 | 0 | 0 | — | 2 | keep, fix refresh (last list_asof 2026-09-29 -> all KRX-after checks false) |
| `public.symbol_sectors` | 137 | 2026-07-27 | 0 | 0 | — | 12 | keep, fix lazy-fill (last write 2026-07-27) |
| `public.manual_holdings` | 22 | 2026-09-28 | 0 | 0 | — | 20 | keep, verify (0 writes in 30d) |
| `public.broker_accounts` | 5 | 2026-06-25 | 0 | 0 | — | 9 | keep |
| `public.naver_research_detail_cache` | 8,076 | 2026-10-30 | 18 | 810 | — | 1 | keep |
| `public.binance_demo_order_ledger` | 484 | 2026-11-01 | 13 | 43 | — | 12 | keep (evidence/audit only) |
| **Market data — candles (expect daily)** | | | | | | | |
| `public.kr_candles_1d` | 585,026 | 2026-11-02 | 19 | 205,382 | — | None | keep |
| `public.kr_candles_1m` | 1,161,123 | 2026-11-02 | 19 | 414,797 | — | None | keep |
| `public.us_candles_1d` | 1,043,609 | 2026-11-02 | 26 | 366,918 | — | None | keep |
| `public.us_candles_1m` | 303,738 | 2026-11-02 | 26 | 134,474 | — | None | keep |
| `public.crypto_candles_1d` | 60,415 | 2026-11-02 | 30 | 13,772 | — | 2 | keep (hypertable; 60,415 rows) |
| `public.crypto_candles_1m` | 0 | — | — | — | — | 0 | keep, fix loader (EMPTY but read; get_ohlcv crypto falls back to Upbit HTTP) |
| `research.kr_candles_1m_toss` | 1,171,860 | 2026-09-29 | 0 | 0 | — | None | keep, fix (collector stopped 2026-09-29) |
| **News (expect continuous)** | | | | | | | |
| `public.symbol_news_relevance` | 7,296 | 2026-11-02 | 26 | 1,599 | 0 dupes on (article_id,symbol) | 2 | keep |
| **Orders, approval and fills (expect every trading day)** | | | | | | | |
| `review.order_proposals` | 1,446 | 2026-11-02 | 26 | 313 | — | 50 | keep |
| `review.order_proposal_rungs` | 1,464 | 2026-11-02 | 26 | 313 | 0 orphan rungs; 0 resting rungs without a broker order id | 20 | keep |
| `review.order_proposal_approval_batches` | 204 | 2026-11-02 | 17 | 33 | — | 2 | keep |
| `review.order_proposal_approval_batch_members` | 354 | 2026-09-08 | 0 | 0 | — | 2 | keep, fix (no rows since 2026-09-08 while batches keep updating) |
| `review.order_proposal_approval_dispatch_attempts` | 1,263 | 2026-11-02 | 25 | 301 | — | 2 | keep |
| `review.order_proposal_approval_events` | 0 | — | — | — | — | 2 | keep (empty) |
| `review.order_send_intents` | 387 | 2026-11-02 | 14 | 46 | — | 10 | keep |
| `review.live_order_ledger` | 437 | 2026-10-31 | 15 | 33 | — | 31 | keep |
| `review.kis_live_order_ledger` | 252 | 2026-11-02 | 14 | 44 | — | 22 | keep |
| `review.toss_live_order_ledger` | 1,133 | 2026-11-02 | 21 | 192 | — | 23 | keep |
| `review.execution_ledger_reconcile_runs` | 15,975 | 2026-11-02 | 31 | 2,860 | — | 2 | keep |
| `review.trades` | 462 | 2026-11-02 | 12 | 20 | — | 136 | keep |
| `review.protected_positions` | 2 | 2026-09-29 | 0 | 0 | — | 6 | keep (2 rows) |
| `review.protected_position_revisions` | 2 | 2026-09-29 | 0 | 0 | — | 2 | keep (2 rows) |
| `public.trading_decision_proposals` | 4,120 | 2026-11-02 | 19 | 608 | — | 3 | keep |
| `public.trading_decision_sessions` | 264 | 2026-11-02 | 19 | 38 | — | 8 | keep |
| `public.trading_decision_actions` | 0 | — | — | — | — | 1 | keep, verify (0 rows; only proposals is written) |
| `public.trading_decision_counterfactuals` | 0 | — | — | — | — | 1 | keep, verify (0 rows; only proposals is written) |
| `public.trading_decision_outcomes` | 0 | — | — | — | — | 2 | keep, verify (0 rows; only proposals is written) |
| `public.trading_decision_strategy_events` | 5 | 2026-04-29 | 0 | 0 | — | 1 | keep, verify (no writes in 30d) |
| **Decision records (expect daily/weekly)** | | | | | | | |
| `review.trade_forecasts` | 2,445 | 2026-11-02 | 31 | 1,076 | 0 dupes on forecast_id | 7 | keep |
| `review.trade_retrospectives` | 659 | 2026-11-02 | 23 | 115 | 1 duplicate correlation_id | 10 | keep |
| `review.trade_retrospective_actions` | 800 | 2026-11-02 | 23 | 121 | — | 4 | keep (evidence/audit only) |
| `review.trade_retrospective_action_control` | 1 | 2026-07-15 | 0 | 0 | — | 1 | keep (evidence/audit only) |
| `review.screener_pick_log` | 658 | 2026-10-30 | 12 | 118 | — | 1 | keep (evidence/audit only) |
| `review.analysis_artifacts` | 1,020 | 2026-11-02 | 30 | 469 | — | 7 | keep |
| `review.operator_session_context` | 2,923 | 2026-11-02 | 30 | 634 | — | 1 | keep |
| `review.investment_watch_events` | 499 | 2026-10-30 | 15 | 29 | — | 11 | keep (active) |
| `review.investment_watch_alerts` | 721 | 2026-11-02 | 21 | 67 | — | 18 | keep (active) |
| **Mock / paper venues (evidence)** | | | | | | | |
| `review.kis_mock_order_ledger` | 78 | 2026-08-12 | 0 | 0 | — | 19 | keep (78 rows, last 2026-08-12) |
| `review.kis_mock_signal_ledger` | 2 | 2026-08-12 | 0 | 0 | — | 3 | keep (attribution gate, 2 rows) |
| `review.kis_mock_runner_control` | 1 | 2026-08-08 | 0 | 0 | — | None | keep, verify (no writes in 30d) |
| `paper.paper_accounts` | 2 | 2026-10-31 | 1 | 1 | — | 6 | keep |
| `paper.paper_trades` | 35 | 2026-10-31 | 12 | 35 | — | 6 | keep |
| `paper.paper_positions` | 2 | 2026-10-31 | 2 | 2 | — | 1 | keep |
| `paper.paper_pending_orders` | 58 | 2026-10-31 | 14 | 58 | — | 4 | keep |
| **Guards and feature state** | | | | | | | |
| `review.lot_exit_plans` | 75 | 2026-11-02 | 1 | 75 | — | 1 | keep |
| `review.mechanical_loss_cut_decisions` | 22 | 2026-11-02 | 1 | 22 | — | 1 | keep |
| `review.strategy_sleeves` | 0 | — | — | — | — | 1 | keep (ROB-1470, empty until first sleeve) |
| `review.strategy_sleeve_versions` | 0 | — | — | — | — | 1 | keep (ROB-1470, empty until first sleeve) |
| `public.user_settings` | 1 | 2026-11-02 | 1 | 1 | — | 2 | keep |
| `public.users` | 2 | 2026-04-20 | 0 | 0 | — | 84 | keep (auth) |
| `public.refresh_tokens` | 23 | 2026-09-03 | 0 | 0 | — | 3 | keep (auth) |
| **Other kept tables (31)** | | | | | | | |
| `public.alembic_version` | 1 | — | — | — | — | — | keep (system) |
| `public.invest_crypto_screener_snapshots` | 11,400 | 2026-11-02 | 30 | 6,000 | — | 13 | keep |
| `public.invest_theme_event_snapshot_stocks` | 0 | — | — | — | — | 1 | keep, fix (0 rows while the parent has 188,200) |
| `public.market_event_ingestion_partitions` | 1,773 | 2026-11-02 | 31 | 68 | — | 7 | keep |
| `public.news_issue_lab_results` | 24 | 2026-05-07 | 0 | 0 | — | — | keep, verify (no writes in 30d) |
| `public.news_issue_lab_runs` | 2 | 2026-05-07 | 0 | 0 | — | — | keep, verify (no writes in 30d) |
| `review.alpaca_paper_order_ledger` | 45 | 2026-10-05 | 1 | 7 | — | 15 | keep |
| `review.binance_h5_intents` | 40 | 2026-11-01 | 16 | 40 | — | 1 | keep (evidence/audit only) |
| `review.binance_h5_lane_state` | 1 | 2026-11-02 | 1 | 1 | — | 1 | keep (evidence/audit only) |
| `review.binance_h5_nav_samples` | 22,831 | 2026-11-02 | 17 | 22,831 | — | 2 | keep (evidence/audit only) |
| `review.binance_h5_opportunities` | 291 | 2026-11-02 | 17 | 291 | — | 2 | keep (evidence/audit only) |
| `review.binance_h5_signals` | 102 | 2026-11-02 | 16 | 102 | — | 2 | keep (evidence/audit only) |
| `review.buy_gate_ab_collection_epoch` | 1 | 2026-08-30 | 0 | 0 | — | 1 | keep (evidence/audit only) |
| `review.buy_gate_ab_collection_epoch_v2` | 0 | — | — | — | — | 0 | keep (evidence/audit only) |
| `review.buy_gate_ab_experiment_registration` | 1 | 2026-09-07 | 0 | 0 | — | 0 | keep (evidence/audit only) |
| `review.buy_gate_ab_experiment_termination` | 1 | 2026-09-07 | 0 | 0 | — | 0 | keep (evidence/audit only) |
| `review.execution_child_requests` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.execution_intents` | 0 | — | — | — | — | 2 | keep (evidence/audit only) |
| `review.execution_ledger_quarantine_events` | 4 | 2026-10-02 | 0 | 0 | — | 1 | keep (evidence/audit only) |
| `review.external_cash_declarations` | 1 | 2026-08-19 | 0 | 0 | — | 7 | keep (1 row) or retire |
| `review.funding_advisories` | 0 | — | — | — | — | 6 | keep (dormant since 2026-08-19) or retire |
| `review.invalid_sample_cleanup_bindings` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.invalid_sample_cleanup_lifecycle_events` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.kis_mock_inference_expiry_events` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.kiwoom_authority_attempts` | 0 | — | — | — | — | 2 | keep (evidence/audit only) |
| `review.kiwoom_authority_cessation_receipts` | 0 | — | — | — | — | 2 | keep (evidence/audit only) |
| `review.kiwoom_coordination_lifecycle` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.sample_eligibility_decisions` | 0 | — | — | — | — | 2 | keep (evidence/audit only) |
| `review.telegram_callback_inbox` | 0 | — | — | — | — | 2 | keep (evidence/audit only) |
| `review.telegram_callback_recovery_cursor` | 0 | — | — | — | — | 1 | keep (evidence/audit only) |
| `review.trade_journals` | 200 | 2026-10-31 | 9 | 22 | — | 21 | keep |

### 4.4 Modeled but absent from the production database (4)

| table | why |
|---|---|
| `public.kospi200_constituents` | migration not applied; `kospi200` router reads it |
| `review.kis_mock_dispatch_evidence` | ROB-1263 migration not applied |
| `review.kis_mock_dispatch_lineage` | ROB-1263 migration not applied |
| `review.mock_strategy_orders` | ROB-1474 migration not applied |

These are the proof that **migrations are applied by the operator, separately from the deploy** (CLAUDE.md
`#789` contract). Any retire-first PR must state which of these it expects to exist.

### 4.5 Tables that are only written (compliance / E3 evidence)

These must not be retired even when their readers are gone. They are marked `keep (evidence/audit only)` in
the table above and listed here so a drop review cannot mistake "0 reads" for "dead":

`review.execution_intents`, `review.execution_child_requests`, `review.execution_ledger_quarantine_events`,
`review.kis_mock_dispatch_evidence`, `review.kis_mock_dispatch_lineage`,
`review.kis_mock_inference_expiry_events`, `review.kiwoom_authority_attempts`,
`review.kiwoom_authority_cessation_receipts`, `review.kiwoom_coordination_lifecycle`,
`review.trade_retrospective_actions`, `review.trade_retrospective_action_control`, `review.screener_pick_log`,
`review.buy_gate_ab_collection_epoch` (+`_v2`, registration, termination),
`review.binance_h5_signals`, `review.binance_h5_intents`, `review.binance_h5_nav_samples`,
`review.binance_h5_opportunities`, `review.binance_h5_lane_state`, `public.binance_demo_order_ledger`,
`review.mock_strategy_orders`, `review.telegram_callback_inbox`, `review.telegram_callback_recovery_cursor`,
`review.invalid_sample_cleanup_bindings`, `review.invalid_sample_cleanup_lifecycle_events`,
`review.sample_eligibility_decisions`.

Rule applied: a table whose **only** purpose is to prove what a guarded system did (dispatch, expiry
inference, authority cessation, quarantine, observation cohorts) stays, whatever its read count.

## 5. Health: kept things that are used but silently not loading

This is the most important section. Every line is a kept feature whose **reader runs** and whose **writer
stopped**. None of them raises an error today; they return empty or stale results that look like a normal
answer.

| # | Feature (tool / job) | Reader works? | Writer evidence | Consequence today |
|---|---|---|---|---|
| 5.1 | `market_quote_snapshot_ensure` / `..._latest` | yes (≤8 calls) | `public.market_quote_snapshots`: **7 rows total**, last 2026-10-05 | "ensure" returns nothing durable; one row per call |
| 5.2 | `get_analyst_consensus`, `get_financials`, `get_company_profile` | yes (live yfinance) | `analyst_consensus_snapshots`: **EMPTY**; `financial_fundamentals_snapshots`: **EMPTY** | live fetch only; the `/invest` snapshot panels read empty tables |
| 5.3 | `investment_watch_events_list_recent` (245 calls) | yes | `investment_reports`/`_items`: no write since **2026-07-28**; `investment_watch_events`: 29 rows/30d | watches fire, but the report that should explain them is never written |
| 5.4 | KRX after-market gate (`route_request`, Toss preflight) | yes | `krx_after_market_eligibility`: **last `list_asof` 2026-09-29** | by contract the list must be ≤7 days old, so **every KRX-after trade is refused** — silently, as `krx_after_capability_stale` |
| 5.5 | `get_news` (193 calls) + relevance | yes | `news_article_related_symbols` and `news_ingestion_runs`: **0 writes in 30d** (last 2026-09-30); `symbol_news_relevance` still written (1,599/26d) | two relevance tables, one alive, one dead; news ingest run log stops a month ago |
| 5.6 | `screen_stocks_enrich` sector column | yes | `symbol_sectors`: 137 rows, last write **2026-07-27** | sector labels are whatever was cached in July |
| 5.7 | `research/selection` run refresh (`research_run.kr_preopen_refresh`, 8 crons) | schedules fire | `public.research_runs`: **1 row**, last 2026-04-28; `research_run_candidates`: 16 rows | 8 cron jobs per KR day produce nothing |
| 5.8 | `get_forecasts` / retrospective | yes | `trade_retrospectives`: 659 rows but **378 NULL `realized_pnl` (57%)**, 355 NULL `pnl_pct` | P&L columns are mostly empty; only 437 of 659 are `filled` |
| 5.9 | `mechanical_loss_cut` shadow (ROB-1460) | yes | `lot_exit_plans` 75 rows, `mechanical_loss_cut_decisions` 22 rows, **both written on 1 day** | the shadow evaluator has run once |
| 5.10 | `protected_position_auto_follow` (#943) | yes | `protected_positions`: **2 rows**, `protected_position_revisions`: 2 rows, 0 writes in 30d | the rule-executed P follow has nothing to follow |
| 5.11 | approval digest (ROB-1052) | yes | `order_proposal_approval_batch_members`: **0 rows since 2026-09-08** while `..._batches` keep updating (109 `idle`, 95 `sent`, last `sent` 2026-09-08) | digest batching stopped producing members a month ago |
| 5.12 | `market_events` calendar | yes | `category=economic`: 5,065 rows, last write **2026-10-30**; `earnings` US: 303 rows/30d | economic calendar ingestion is manual (`ingest_market_events.py`) |
| 5.13 | `paper_daily_snapshots` | no reader | **0 rows ever** | ROB-1450's export builds `account_balance_daily` from this table → it would emit nothing |
| 5.14 | `trading_decision_actions` / `_counterfactuals` / `_outcomes` | not read by any tool | all **EMPTY** while `trading_decision_proposals` has 4,120 rows written nightly | the decision record is half-written: proposals yes, actions/outcomes no |
| 5.15 | `invest_theme_event_snapshot_stocks` | yes | **EMPTY** while the parent `invest_theme_event_snapshots` has 188,200 rows | per-theme stock lists are missing |
| 5.16 | `crypto_candles_1m` (hypertable) | yes | **EMPTY**; `get_ohlcv` crypto silently falls back to Upbit HTTP | the DB read path is dead, the API path hides it |
| 5.17 | `research.kr_candles_1m_toss` (#1086) | no reader yet | 1,171,860 rows, **last write 2026-09-29** | the collector is scheduleless by contract; data stops a month ago |

Four of these are **safety relevant** and should be fixed before any retirement work:

- **5.4 KRX after-market** — a live approval gate is silently refusing every after-market KR order because the
  eligibility list is stale. Fail-closed is correct; the fact that nobody noticed for 34 days is not.
- **5.11 approval digest** — the auto-approve digest stopped producing members while batches keep being
  created. A card that never gets a member is a card the operator never sees.
- **5.3 investment report** — 245 reads of a watch-event feed whose companion report has not been written in
  3 months.
- **5.8 retrospective P&L** — 57% of retros have no realized P&L, which is the number the whole retrospective
  loop is for.

The other thirteen are correctness debt: the reader returns a plausible empty answer.

## 6. Target architecture

The operator's model — **execution core in Go next to fillwire/go-kis, research in Python batch jobs writing
snapshots, a thin MCP that only reads** — is right for this codebase, and the measured data says where to draw
each line. Three facts drive the split:

1. The request path is small: ~14,200 tool calls in 30 days (one every three minutes), mostly reads.
2. The **dangerous** code is concentrated: every hard rule (4, 5, 7, 11, 14, 15, 16, 18) lives in the order
   send path, the ledger, or a guard. That is the `brokers` package (192 files), `order_proposals` (49),
   `execution_ledger` (12), `kis_mock_runner` (13) and the guard modules — roughly **300 files that carry every
   rule**, and they are what must be proven identical rather than rewritten casually.
3. The **large** code is the opposite: `app/services` is 70% of the app, and its biggest packages are snapshot
   builders and read models (`invest_view_model` 48, `market_events` 25, `invest_screener_snapshots` 10) — pure
   batch plus pure read.

### 6.1 The three parts

**A. Execution core — Go, next to `fillwire`/`go-kis`.**
Owns: broker sessions, order send/cancel/modify, intent + approval gates, the fill ledger, the guards.
It is an extension of what `fillwire` already does (holds the KIS socket, decodes, writes its own stream, enters
AT through one token-authed HTTP route, `docs/runbooks/execution-ledger-ingest.md`). The core adds the *write*
direction and keeps the same shape: **no direct DB writes from Go, one token-authed route into the service
layer** until the ledger itself moves.

What must be true before anything moves here: see 6.3.

**B. Research and selection — Python batch jobs writing snapshots.**
Screening, ranking, candidate discovery, fundamentals/consensus/valuation, momentum/theme/flow, watch scanning
and the retrospective/forecast scoring. These already are batch jobs (TaskIQ/Prefect) and already write
`public.invest_*`; the change is to make them **the only writers** and to make the MCP read *their* snapshots
instead of calling brokers live. This is where ~60% of the remaining Python belongs.

**C. Thin MCP — reads the core API and the snapshots.**
A tool becomes: validate args → call one core endpoint or one snapshot query → return. No broker client import,
no per-call fan-out, no `analyze_stock_batch` doing 250-bar math inside a tool. The closed-world profiles
(`h3-crypto-paper`, `h3-us-paper`, `kiwoom`) already prove this shape works; they exist to constrain exactly
this.

### 6.2 Where each kept feature lands

| kept feature (MCP tools / jobs) | part | why |
|---|---|---|
| `get_quote`, `get_ohlcv`, `get_orderbook`, `get_support_resistance`, `get_indicators`, `get_market_index` | **B→C** | snapshot/batch writes, tool reads DB; today `get_quote` calls KIS/Upbit/Toss per call |
| `get_news`, `get_disclosures`, `get_earnings_calendar`, `get_market_issues` | **B→C** | already DB-backed; merge to one `get_news` |
| `screen_stocks`, `screen_stocks_snapshot`, `discover_buy_candidates_fanout`, `analyze_stock_batch` | **B** | pure batch over `invest_*` + `daily_candles`; 0 HTTP in quick mode already |
| `get_holdings`, `get_available_capital`, `suggest_order_account`, `get_protected_positions` | **A** (read endpoint) | need the ledger + broker positions; latency is broker HTTP |
| `route_request` | **A** | it is a guard over the served tool set, not a feature |
| `order_proposal_*`, `toss_proposal_accounts`, approval window, auto-approve | **A** | hard rules 4/11/14; the write path |
| `place_order` / `cancel_order` / `modify_order` / `kis_*` / `toss_*` / `kiwoom_mock_*` / `alpaca_paper_*` order tools | **A** | hard rules; each venue's transport |
| `execution_ledger_fill_events_list_recent`, reconcile jobs, `quarantine_execution_ledger_rows` | **A** | fill authority (rule 5/16) |
| `forecast_save` / `forecast_resolve` / `get_forecasts`, `save_trade_retrospective`, `analysis_artifact_*`, `session_context_*` | **B→C** | already DB-only; keep in Python, read from MCP |
| `get_operating_briefing` (45 s p95) | **B→C** | precompute into a snapshot; the tool returns the snapshot |
| `get_trading_policy`, `decision_table_validate` | **C** | pure config read |
| `investment_report_*` (dead) | *retire* | see $5.3 |
| `investment_watch_*` (alive) | **B→C** | watch scanner is already a 5-minute batch |
| `mechanical_loss_cut`, `protected_position_auto_follow` | **A** | they gate live sells |
| paper/mock venues (H3, DB paper, Alpaca) | **A** | they are order paths with their own ledgers |
| `screen_stocks_enrich`, sector/consensus lazy fill | **B** | fetch + persist; never on the request path |
| the 7 `shadow-replay` tools, buy-gate A/B, fill-watch context | *decide* | 0 calls, 0 rows; observation surfaces |
| `get_user_setting`/`set_user_setting`, `get_parking_exclusion` | **C** | one typed settings read |
| CLI levers (`quarantine_execution_ledger_rows`, `export_dots_history`, `strategy_sleeves`, H5 gates, `deploy_ci_gate`) | **ops** | stay Python; they are operator tools, not product |

### 6.3 Invariants that must be proven equal before each move

These are not new requirements; they are the existing contracts, restated as **acceptance gates** for a move.
A move is done when the named evidence exists, not when the code compiles.

| move | invariant | how to prove equal |
|---|---|---|
| proposals/approval → core | single-use nonce, commit lease, target lock, approval hash fail-closed (`approval-and-shadow-contracts.md`) | replay the reserved-case matrix against both implementations; same reject codes for the same input |
| proposals/approval → core | W5 retry algebra: only a proven pre-core failure is retryable | replay `test_retry_contract_surfaces.py` cases end-to-end; same terminal states |
| order send → core | double gate (`*_ENABLED` + per-call `confirm`), host allowlist, redirect/retry refusal | static test that the Go tree contains no order endpoint for venues it does not own; per-venue smoke with the gate off must produce 0 HTTP |
| order send → core | attribution pre-submit for `kis_mock` (rule 7) | same `attribution_required` / `signal_record_unavailable` behaviour |
| ledger/fills → core | idempotency key `(broker, account_mode, venue, broker_order_id, fill_seq)`; `source=websocket` forced; per-item savepoint; commit is authority | re-ingest the same payloads through both paths; same `inserted/updated/unchanged/rejected` per item |
| ledger/fills → core | ROB-1379 supersede and #1175 quarantine: a new reader must AND `execution_ledger_in_effect()` | run `test_cross_source_dedupe_db.py` and `test_quarantine_mutants.py` against the new reader |
| ledger/fills → core | evidence-first: never mark `filled` without broker evidence | diff ledger rows before/after on a dry-run reconcile |
| guards → core | kill switches default off; `loss_cut` auto-approve unlocked only by the bound decision row; caps not raised | config diff must show every switch still false; decision-table tests unchanged |
| research split | RSI-14 single source and Wilder formula; `current_price` in quick is a DB close, not a live quote | golden compare of `analyze_stock_batch(quick=True)` output before/after |
| research split | `halted_suspect` classification and fail-open on read error | replay the 3-session freeze fixtures |
| MCP layer thinning | lane allowlists preserved (`config/mcp_lane_allowlists/` `tool<TAB>basis`); profile snapshot unchanged except intended removals | `test_lane_allowlist_contract.py` + `test_profile_tool_snapshot.py` |
| anything | runtime LLM boundary (no in-process provider in `app/**`) | the existing static guard |

### 6.4 Migration order

The operator's order — **retire, then move execution, then split research** — is correct, with one insertion
before step 1: **fix the writers that silently stopped** ($5). Retiring while a live gate is quietly refusing
every after-market order would destroy the evidence needed to judge the retirement.

```
0. (2 weeks) fix the $5 writers + add a "writer heartbeat" check (see 7.0)
1. (6-8 PRs) retire dead tool surface / dead venues / dead routers / dead services     ← no schema change
2. (1 PR)    schema migration M1: drop 73 empty tables                                 ← operator approval
3. (2 PRs)   schema migration M2/M3: export+drop data-holding retires; dedupe + unique keys
4. (N PRs)   execution core: proposal/approval state → guards → order send → ledger/fills
             (ledger last: it is the authority for everything else)
5. (N PRs)   research split: snapshots become the only source; MCP tools read them
6. (1 PR)    schema migration M4: hypertable + retention for the 8 snapshot tables
```

Rationale for the order:

- Retirement is **cheap and reversible** (delete code + tests, no data). It also shrinks the surface that any
  later move has to prove equal.
- The execution core is the **only place where a mistake costs money**, so it moves after the surface is small
  enough to test exhaustively.
- The ledger moves **last inside step 4**, because every other core component's evidence is read from it.
- Research split is **independent** of the core (it reads DB only) and is the largest remaining block of code;
  it can run in parallel with step 4 once the snapshots are stable.

## 7. Retire-first plan

### 7.0 Before the first retirement PR (health fixes, 4 small PRs)

Not retirement, but they must land first so the retirement PRs are not deleting the only signal:

| PR | change | blast radius |
|---|---|---|
| H1 | refresh `public.krx_after_market_eligibility` on a schedule or make the router refuse loudly | 1 table, 1 import script, the KRX-after gate in `approval_window` + `nxt_preflight` |
| H2 | fix the approval digest member writer (0 rows since 2026-09-08) | `app/services/order_proposals/` digest path, ROB-1052 |
| H3 | add a `writer heartbeat` guard: a table whose declared cadence is missed for N days should call `get_operating_briefing`'s attention section | `get_operating_briefing` + a new read-only diagnostic; **no writes** |
| H4 | decide `investment_report_*`: restart the writer or retire it ($5.3) | 4 routers, 27 service files, 12 tables — operator decision |

### 7.1 The first five retirement PRs (no migrations)

Each is code + tests only. `git diff --stat` touches `app/`, `tests/` and `docs/` but **no `alembic/`**.

**R1 — Retire the 81 dead MCP tools and their registrars.**
Tools listed `retire` in $3 (81 names). Delete the registration, the handler, and the dedicated tests; keep
shared services. Blast radius: the profile snapshot (233 → ~152 names), the lane allowlist contract, the
`niche` group, and any prompt that names a retired tool. Mitigation: the snapshot test fails loudly with the
exact list; the operator prompts live in a separate repo and must be checked first
(`docs/runbooks/mcp-surface-cleanup-20260905.md` is the precedent: 21 D tools removed with the same gate).
Largest sub-groups: 26 fundamentals/market-data duplicates, 16 `investment_report_*`, 7 `shadow-replay`,
9 NHPLUG, 7 Kiwoom US, 11 Alpaca automated/ledger.

**R2 — Retire the unused venues end-to-end.** NHPLUG (9 tools + `app/services/nhplug_mock` 9 files +
`app/services/brokers/nhplug` 10 files + `scripts/nhplug_*` 4), Binance demo (80 `app/services/brokers/binance`
files, 12 scripts, 1 table), `us_dual_paper` (7 files, 3 tools), Kiwoom US (7 tools). Blast radius: the
`kiwoom` profile drops 7 names (152 -> 145); `h3-*` profile snapshots unchanged; the Binance demo
ledger stays as evidence until M2.
**Operator approval required** — these are execution surfaces, and hard rule 4 says a venue's code is its
permission to trade.

**R3 — Retire the legacy research pipeline.** Tools `research_session_get`, `research_session_list_recent`,
`research_summary_get`, `stage_analysis_get`; routers `research_pipeline.py`, `research_retrospective.py`,
`research_reports.py`; jobs `research_run_refresh_tasks.py` (8 crons producing nothing),
`journal_*_tasks.py`, `watch_follow_up_tasks.py`, `mock_roundtrip_journal_tasks.py`,
`research_reports_ingest_tasks.py`, `pending_orders.py`, `quotes_consumer_tasks.py`,
`paper_cohort_tasks.py`, `spike_attribution` service. Blast radius: 3 routers and their consumers, 12 tables
that stay until M2, `app/services/{trade_journal,spike_attribution,paper_cohort,paper_evaluation,paper_validation,shadow_replay}`
(~60 files).

**R4 — Retire the Hermes report write surface** (blocked on H4). The **15 write/compose tools** retire
(`investment_report_create_from_hermes_composition`, `..._generate_from_bundle`, `..._prepare_bundle`,
`..._prepare_intraday_context`, `..._get_hermes_context`, `..._add_items`, `..._decide_item`,
`..._set_status`, `..._update`, `..._delta_get`, `..._context_get`, the 3 `investment_snapshot_*` readers and
`investment_stage_artifacts_ingest_from_hermes`). `investment_report_create` / `_list` / `_get` **stay** until
H4 decides, because they are the only way to create or read an existing report row. Also retires 4 routers,
27 `investment_reports` files, 19 `investment_stages` and 48 `action_report` files. Blast radius: the Hermes
ingest token path, the `/trading/api/investment-reports/hermes/` routes, and the `investment_report_*` schema.
**Operator approval required**: `docs/agent-contracts/mcp-and-analysis-contracts.md` treats the report item
contract as live.

**R5 — Merge the 35 duplicate tools.** `get_financials`/`get_analyst_consensus`/`get_dividends`/
`get_insider_transactions`/`get_short_interest`/`get_sector_peers` → one `get_fundamentals`; four news entry
points → `get_news`; six crypto context tools → two; `get_position`/`get_portfolio_allocation`/`get_cash_balance`
→ `get_holdings`/`get_available_capital`; six retrospective/journal readers → `get_trade_retrospectives` +
`session_context_get_recent`. Blast radius: tool *names* change, which is a prompt-visible break. Do it after
R1 so only surviving tools are merged.

### 7.2 Schema migrations that follow (each needs operator approval)

| id | migration | what | why it is safe | approval |
|---|---|---|---|---|
| **M1** | drop 73 empty tables | one `op.drop_table` per table, grouped by schema (public 30, review 30, research 13) | 0 rows in prod; verified by exact `count(*)` | **yes** — a drop is irreversible |
| **M2** | export then drop 30 data-holding retire tables | `pg_dump --table` to an archive outside the repo, verify row counts, then drop | 16,450 rows total; the biggest is `news_ingestion_runs` 7,129 and `stock_analysis_results` 2,413 | **yes** |
| **M3** | `market_valuation_snapshots` dedupe + unique | keep the newest row per `(market,snapshot_date,symbol,source)`, add the unique key, switch the writer to upsert | removes 161,897 duplicate rows (24%); reads become deterministic | **yes** — deletes rows |
| **M4** | `market_events` dedupe + unique on `(source,source_event_id)` | same pattern; 15,812 duplicate earnings rows | the event reader already assumes one row per event | **yes** |
| **M5** | hypertable + retention for 8 snapshot tables | `create_hypertable` on the date column + `add_retention_policy` | TimescaleDB is already installed (8 hypertables exist); retention is the only way to bound 2.7 GB of growth | **yes** — retention deletes data |

Migrations are **operator-applied** (CLAUDE.md `#789`; 4 modeled tables are absent from prod today, which is
the proof). None of them may ride along with a code PR that touches execution.

### 7.3 What this does to the size

| after | files removed | tables removed |
|---|---|---|
| R1 (81 tools) | ~90 tooling/test files | 0 (M1/M2 later) |
| R2 (venues) | ~120 service + script files | 0 (M2 later) |
| R3 (legacy research) | ~60 service + router + job files | 0 (M2 later) |
| R4 (report pipeline, if approved) | ~94 service + router files | 0 (M2 later) |
| R5 (merges) | ~20 handler files | 0 |
| **total code** | **~380 of 1,638 app files (23%)** | — |
| M1+M2 | — | **103 of 211 tables (49%)** |
| M3+M5 | — | 8 tables bounded, ~170k duplicate rows removed |

## 8. Appendix — queries used

All queries ran read-only through the active API container on NCP
(`ssh ncp 'C=at-api-$(cat /root/at-run/api-active-color); docker exec -i $C sh -c "cd /app && /app/.venv/bin/python -"'`),
`from app.core.db import engine`, `SELECT`/`information_schema`/`pg_stat_*` only. Statement timeouts 8-12 s.
Results were aggregate only; no order rows, account numbers or secrets were read into this document.

**A1 — schema/table census**

```sql
select schemaname, count(*) from pg_tables
where schemaname not in ('pg_catalog','information_schema') group by 1 order by 2 desc;
select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema');
select * from timescaledb_information.hypertables;
select pg_size_pretty(pg_database_size(current_database()));
```

**A2 — activity / deadness**

```sql
select s.schemaname, s.relname, s.n_live_tup, s.n_tup_ins, s.n_tup_upd, s.n_tup_del,
       s.seq_scan, coalesce(i.idx_scan,0) idx_scan, s.last_vacuum, s.last_analyze
from pg_stat_user_tables s
left join (select schemaname, relname, sum(idx_scan) idx_scan
           from pg_stat_user_indexes group by 1,2) i using (schemaname, relname)
where s.schemaname not in ('pg_catalog','information_schema','_timescaledb_internal','_timescaledb_catalog');
```
`n_live_tup` is an estimate and was **0 for several populated tables** (e.g. `investment_report_items`,
`broker_accounts`), so every row count in $4 is an exact `count(*)`, not this column. `seq_scan` is inflated
by `pg_dump` (~71 per table since 2026-09-16); `idx_scan` is the app-read signal.

**A3 — per-table rows and last write** (run per table, `statement_timeout 8s`)

```sql
select (select count(*) from "<schema>"."<table>"),
       (select max("<ts_col>") from "<schema>"."<table>");
```
`<ts_col>` is chosen by preference order `updated_at, created_at, filled_at, observed_at, finished_at,
published_at, snapshot_date, trading_date, time, as_of, …` from `information_schema.columns`.

**A4 — 30-day cadence**

```sql
select count(*) rows_30d,
       count(distinct (("<ts_col>") at time zone 'Asia/Seoul')::date) days_30d,
       min("<ts_col>"), max("<ts_col>")
from "<schema>"."<table>" where "<ts_col>" >= now() - interval '30 days';
```

**A5 — duplicates / nulls**

```sql
-- execution ledger idempotency key
select count(*) from (select broker, account_mode, venue, broker_order_id, fill_seq, count(*) c
  from review.execution_ledger group by 1,2,3,4,5 having count(*)>1) x;
-- valuation snapshot keys
select count(*), count(distinct (market, snapshot_date, symbol)) from public.market_valuation_snapshots;
-- market events keys
select category, count(*), count(distinct (source, source_event_id)) from public.market_events group by 1;
-- forecasts
select coalesce(decision_bucket,'<null>'), count(*) from review.trade_forecasts group by 1 order by 2 desc;
select count(*) filter (where resolved_at is not null), count(*) from review.trade_forecasts;
-- retrospectives
select count(*), sum((realized_pnl is null)::int), sum((pnl_pct is null)::int)
  from review.trade_retrospectives;
-- proposals / rungs lifecycle
select lifecycle_state, count(*) from review.order_proposals group by 1 order by 2 desc;
select state, count(*) from review.order_proposal_rungs group by 1 order by 2 desc;
-- ledger source mix and null rates
select broker, account_mode, source, count(*), count(distinct broker_order_id),
       sum((filled_qty is null)::int), sum((filled_at is null)::int)
  from review.execution_ledger group by 1,2,3 order by 4 desc;
-- news relevance
select status, count(*) from public.symbol_news_relevance group by 1 order by 2 desc;
-- screener partitions
select market, count(distinct snapshot_date), min(snapshot_date), max(snapshot_date)
  from public.invest_screener_snapshots where snapshot_date >= current_date - 40 group by 1;
```

**A6 — production env gates (boolean only, whitelisted names)**

```bash
ssh ncp 'for n in INVEST_SCREENER_SCHEDULE_ENABLED ... WS_LEDGER_SINK; do   v=$(docker exec at-api-green printenv "$n"); echo "$n=${v:-<unset>}"; done'
```
Read on 2026-11-02: `INVEST_SCREENER_SCHEDULE_ENABLED`, `MARKET_VALUATION_SCHEDULE_ENABLED`,
`INVESTOR_FLOW_SCHEDULE_ENABLED`, `INVEST_MOMENTUM_EVENTS_SCHEDULER_ENABLED`,
`ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED`, `EXECUTION_LEDGER_RECONCILE_SCHEDULER_ENABLED`,
`ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED`, `INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED`, `ORDER_PROPOSALS_ENABLED`,
`INVEST_APPROVALS_ENABLED`, `KIS_MOCK_ENABLED`, `TOSS_API_ENABLED`, `TOSS_LIVE_ORDER_MUTATIONS_ENABLED`,
`SCREENER_PICK_LOG_ENABLED`, `MECHANICAL_LOSS_CUT_SHADOW_ENABLED`,
`PROTECTED_POSITION_AUTO_FOLLOW_ENABLED` = `true`; `WS_LEDGER_SINK=http`; `PAPER_COHORT_ENABLED`,
`TOSS_FILL_POLL_ENABLED`, `NH_MOCK_MCP_ENABLED` = unset (off).

**A7 — MCP tool surface**

```bash
python3 - <<'PY'   # reads the CI-verified snapshot; starts no server
import json; d=json.load(open('tests/mcp_server/profile_tool_snapshot.json'))
print({g:{p:len(v) for p,v in d[g].items()} for g in d})
PY
```

