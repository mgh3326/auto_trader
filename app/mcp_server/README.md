# auto_trader MCP server

MCP tools (market data, portfolio, order execution) exposed via `fastmcp`.

## Observability (Sentry MCP)
- MCP tracing uses `sentry_sdk.integrations.mcp.MCPIntegration` when enabled.
- Recommended trace filter:
  - `service:auto-trader-mcp op:mcp.server`
- yfinance outbound HTTP is custom-instrumented via `SentryTracingCurlSession` and injected with `session=` at yfinance entrypoints.
- yfinance child span format:
  - `op:http.client`
  - span name/description: `METHOD /path` (query string excluded)
- Example trace filters for yfinance spans:
  - `service:auto-trader-mcp op:http.client transaction:"tools/call screen_stocks"`
  - `service:auto-trader-api op:http.client span.description:"GET /v1/finance/screener"`
- `profile` flamegraph and `trace` spans are different datasets, so some frames may appear only in profiling.
- It is normal to see only high-level spans when a tool does not execute DB/HTTP operations.
- MCP tool call arguments are attached as structured Sentry context (`mcp_tool_call`) via `McpToolCallSentryMiddleware`:
  - Context fields: `tool_name` (string), `arguments` (dict, sanitized and truncated)
  - Tag: `mcp.tool.name` for issue-level filtering
  - Sensitive values (`token`, `secret`, `password`, `authorization`, `cookie`) are masked to `[Filtered]`
  - Large arguments are truncated (strings: 1024 chars, lists/dicts: 25 items) with a visible `[truncated]` marker
  - The middleware never calls `capture_exception` directly; exception capture is handled by Sentry's `MCPIntegration`

## Tools

### News Tools (Pre-Market Briefing Pipeline)

- `get_news(symbol, market=None, limit=10)`
  - Fetch symbol-level recent news for decision diagnostics (`kr`: Naver Finance, `us`/`crypto`: Finnhub)
  - KR/US/crypto: fetched articles are persisted (`news_articles` + `symbol_news_relevance`) and the response is served from DB state. Each item carries a `relevance` block (`status`: `pending`/`confirmed`, judged fields, non-authoritative `hints`). `excluded` articles (judged unrelated/low by the external judgment job) are omitted; `excluded_count` reports how many. No deterministic blacklist — auto_trader never excludes on its own.
  - When `NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED=true`, visible `pending` rows in the canonical DB response enqueue `news_relevance.judge_pending`, including rows created during an earlier worker/webhook outage. Duplicate enqueue is acceptable because the worker re-queries pending rows.
  - `degraded: true` + `fetch_error` appear when provider fetch failed and the response was served from DB cache only.
  - `pending` means "not yet judged" — treat as unverified recall, not confirmed evidence.
  - Returns: `symbol`, `market`, `source`, `count`, `excluded_count`, `news`

- `get_market_news(market=None, hours=24, feed_source=None, source=None, keyword=None, limit=20, briefing_filter=False)` [LEGACY — briefing only, not decision evidence]
  - Fetch recent market news for agent pre-market briefing
  - `market`: Optional market scope (`kr`, `us`, `crypto`) for market-separated briefing inputs
  - `feed_source`: Collection path key (e.g., `browser_naver_mainnews`, `browser_naver_research`, `rss_cointelegraph`, `rss_cnbc_earnings`, `rss_cnbc_finance`)
  - `source`: Publisher label (e.g., `연합뉴스`, `매일경제`, `CNBC`, `Cointelegraph`)
  - `briefing_filter`: Format market-specific briefing sections for `kr`/`us`, and rank crypto-relevant articles while separating broad-tech/AI noise into `excluded_news`; raw storage is not affected
  - US briefing sections include `macro_fed`, `finance_credit_rates`, `big_tech`, `earnings`, `market_sentiment`, and `watchlist_analyst`; `rss_cnbc_earnings` is source-hinted into `earnings`, `rss_cnbc_finance` into `finance_credit_rates`, while `http_finviz_news` and `rss_investing_stock_market_news` remain experimental and are only boosted when already market-relevant
  - Returns: `surface`, `advisory`, `count`, `total`, `news` (list), `sources` (unique publishers), `feed_sources` (unique collection paths), `briefing_filter`, `briefing_summary`, `briefing_sections`, `excluded_news`
  - Each article includes `stock_symbol` and `stock_name` for holdings impact analysis; formatted articles include `briefing_relevance`; crypto articles also include `crypto_relevance` metadata


### Market Data Tools

- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
  - KR equity `get_quote` uses KRX daily quote data for the regular-session baseline and includes `previous_close` when at least two daily rows are available.
  - During KR NXT pre-market (`session: "nxt_premarket"`) and trading-day NXT after-hours (`session: "nxt_after"`), KR `get_quote` overlays `price` from `get_orderbook(symbol, market="kr", venue="nxt")`.
  - NXT price selection order is `expected_price` (`price_source: "nxt_expected_price"`), then best bid/ask mid (`"nxt_mid"`), then a single available best ask or bid (`"nxt_best_ask"` / `"nxt_best_bid"`).
  - A successful NXT overlay returns `data_state: "fresh"`, `regular_session_data_state` with the KRX classifier value, and venue diagnostics (`venue`, `venue_label`, `kis_market_code`, `source_endpoint`, `source_tr_id`) when KIS supplies them.
  - If the NXT orderbook is empty or unavailable, `get_quote` keeps the ROB-464 stale-session behavior: KRX daily `price`, `data_state` from `kr_market_data_state()`, and no NXT diagnostic fields.
  - KR NXT overlay honors Toss market-calendar partial-session closures when the Toss API is enabled; otherwise it falls back to XKRX session days and the corrected NXT windows.
- `get_fx_rate(pair="USDKRW")`
  - Read-only spot FX quote for exchange-timing and US-market cash conversion decisions.
  - P1 supports USD/KRW only. Accepted spellings: `USDKRW`, `USD/KRW`, `USD_KRW`, `USD-KRW`.
  - Source is `app.services.exchange_rate_service.get_usd_krw_rate_details()`, which uses Toss when enabled and open.er-api as fallback.
  - Response fields: `pair`, `base_currency`, `quote_currency`, `rate`, `mid_rate`, `default_rate`, `source`, `valid_from`, `valid_until`, `basis_point`, `rate_change_type`.
  - `default_rate` mirrors the scalar exchange-rate behavior used by existing portfolio and cash consumers.
  - Unsupported pairs raise a tool argument error. FX pairs are not market indices; `get_market_index("USDKRW")` remains unsupported.
  - Trends, bank-specific quotes, preferential effective rates, exchange execution, and US-order total-cost routing are outside ROB-567 P1.
- `suggest_order_account(symbol, market=None, side="buy", quantity, price=None, usd_krw=None)`
  - Read-only advisory tool. It never submits, previews, routes, modifies, or cancels an order.
  - Supports KR/US buys only. Crypto, Upbit, manual cash, paper accounts, and sells are out of scope.
  - Compares KIS/Toss using orderable cash, commission bps, FX spread bps, optional Toss notional limit, and existing-position consolidation.
  - If a symbol is already held in one candidate account, that account wins unless the cheaper alternative saves at least `position_consolidation_threshold_bps` of order notional.
  - Default thresholds: KR 25 bps, US 40 bps. US is stricter because FX basis and overseas tax lots split by account.
  - Always returns `cost_comparison` for both candidate accounts and `position_consolidation` with either `foregone_savings_krw` or `distribution_warning`.
- `get_orderbook(symbol, market="kr")`
- US equity quote price resolution uses KIS overseas current price first when `settings.us_quote_kis_primary` is enabled, then falls back to Yahoo `fast_info`.
  - US quote response keeps `source: "kis_overseas"` or `source: "yahoo"` and includes `previous_close/open/high/low/volume` when the provider supplies them.
  - US quote response includes `session` (`premarket`, `regular`, `afterhours`, `closed`), `data_state` (`fresh` during the extended-hours envelope, `stale` when closed), `price_source` (`kis_overseas_last` or `yahoo_fast_info_close`), `delayed: true`, and optional `quote_asof` when KIS supplies parseable quote date/time fields.
  - KIS-backed US quote response includes `venue` with the DB-resolved KIS exchange code (`NASD`, `NYSE`, `AMEX`) used for the upstream request.
  - US quote failures are propagated as tool-level errors (exceptions), not returned as in-band error payload dicts.
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=None, account_mode=None)`
  - Crypto positions may include optional `strategy_signal` field when Phase 2 exit logic triggers (4.5% stop-loss or RSI > 46 mean-reversion on profitable positions)
- `get_position(symbol, market=None, account_mode=None)`
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None, include_indicators=False)`
  - period: `day`, `week`, `month`, `1m`, `5m`, `15m`, `30m`, `4h`, `1h`
  - `include_indicators=True` adds `indicators_included` at the payload top level and appends `rsi_14`, `ema_20`, `bb_upper`, `bb_mid`, `bb_lower`, `vwap` to each row
  - `vwap` is populated for intraday periods and `null` for `day/week/month`
  - `1m` / `5m` / `15m` / `30m`: KR/US equity + crypto
  - `4h`: crypto only
  - `1h`: KR/US equity + crypto
  - Crypto `1m` / `5m` / `15m` / `30m` rows expose `timestamp`, `date`, `time`, `open`, `high`, `low`, `close`, `volume`, `value`, `trade_amount` and do not expose raw `datetime`
- US OHLCV behavior:
  - US `day`/`week`/`month` uses Yahoo Finance (`app.services.brokers.yahoo.client.fetch_ohlcv`)
  - US daily uses Yahoo first and Toss as a `period="day"` fallback; US `week` and `month` remain Yahoo-only
  - US intraday (`1m`/`5m`/`15m`/`30m`/`1h`) uses KIS via DB-first reader (`read_us_intraday_candles`) with ET-naive timestamps
  - US intraday rows include `session` field (`PRE_MARKET`, `REGULAR`, `POST_MARKET`)
  - US intraday `end_date="YYYY-MM-DD"` is interpreted as ET `20:00:00` for that market date; timestamp inputs use the exact provided instant
- KR OHLCV behavior:
  - KR `day` keeps the existing Redis-backed `kis_ohlcv_cache` path when `end_date` is omitted
  - KR intraday `1m/5m/15m/30m` use Toss candles first when `TOSS_API_ENABLED` is configured, then fall back to the existing DB/KIS reader. `1h` uses the DB hourly aggregate directly (ROB-548: 60x Toss `1m` aggregation is heavy and shallow) — same on the `get_ohlcv` service and MCP surfaces
  - Toss only provides `1m`; `5m/15m/30m` are aggregated from Toss `1m` (paginated to the requested depth) using the same bucket rules as the KIS path; an empty Toss frame falls back to the DB/KIS reader
  - When Toss is unavailable or disabled, KR `1m` falls back to DB-first reads from raw `public.kr_candles_1m` with venue merge (`KRX` price priority, `volume/value` sum)
  - When Toss is unavailable or disabled, KR `5m/15m/30m/1h` fall back to DB-first reads from Timescale continuous aggregates (`public.kr_candles_5m`, `public.kr_candles_15m`, `public.kr_candles_30m`, `public.kr_candles_1h`)
  - On Toss fallback, KR intraday overlays the most recent 30 minutes from `public.kr_candles_1m` + KIS minute API to cover the unchanged 10-minute sync cadence
  - KR intraday includes the current partial bucket when minute data is available
  - KIS minute venues are merged with strict dedup to prevent double-counting (API overwrites DB per minute+venue)
  - KIS minute API call plan (KST):
    - `09:00 <= now < 15:35`: call KRX (`J`) + NTX (`NX`) in parallel when `nxt_eligible=true` (15:35 delay defense)
    - `08:00 <= now < 09:00`: call NTX (`NX`) only when `nxt_eligible=true`
    - `15:35 <= now < 20:00`: call NTX (`NX`) only when `nxt_eligible=true`
    - When `end_date` is in the past: DB-only (0 API calls)
  - KR intraday degrades to an empty result when symbol is missing/inactive in `kr_symbol_universe` (used for `nxt_eligible`)
  - KR intraday does not use Redis OHLCV cache (`kis_ohlcv_cache`)
  - KR intraday degrades to DB-backed partial data when recent KIS minute overlay calls fail
  - KR intraday response rows add `session` and `venues` fields
- `get_indicators(symbol, indicators, market=None)`
- `get_market_index(symbol=None, period="day", count=20)`
  - KR indices (`KOSPI`/`KOSDAQ`) are tagged with `data_state` from the KRX
    session clock. If the clock is live but the Naver payload is self-inconsistent
    (`change == 0`, `change_pct == 0`, and `open != current`), the original
    numeric fields are preserved and `data_state` is downgraded to `"stale"`
    with `data_state_reason: "kr_index_fresh_clock_payload_lagging"` and `as_of`.
- `get_investment_opinions(symbol, limit=10, market=None)`
- `get_analyst_consensus(symbol)`
  - Get analyst consensus (recommendation mean and price target mean) for a Korean stock from Naver mobile integration API. Distinct from `get_investment_opinions` (report-level). Korean stocks only.
- `get_short_interest(symbol, days=20)`
  - 6자리 KR 종목코드만 지원 (예: `005930`)
  - US ticker (`AAPL`, `SMCI`) 와 crypto symbol (`KRW-BTC`) 은 지원하지 않음
  - `days` 는 1~60 범위로 cap 됨
- `get_intraday_investor_flow(symbol)`
  - KR-only read-only tool for same-day provisional foreign/institution flow by symbol.
  - Source: KIS `investor-trend-estimate` (`/uapi/domestic-stock/v1/quotations/investor-trend-estimate`, TR `HHPTJ04160200`).
  - Returns quantity estimates only: `foreign_net_qty`, `institution_net_qty`, `combined_net_qty`.
  - The response always marks successful data as `provisional: true` and `data_state: "intraday_provisional"`.
  - `as_of` is inferred deterministically from the latest returned KIS slot (`bsop_hour_gb`: 09:30, 10:00, 11:20, 13:20, 14:30) on the KST request date because the KIS payload does not include a date field. Session attribution is a pure function of a single captured `now`, the market state, and the Naver-confirmed latest date, so identical inputs always yield an identical label and a stale prior-session payload is never labeled `observed`.
  - `confidence` is one of: `observed` (KRX session live before 14:30 and the rows are positively today's), `inferred` (today's confirmed daily row already exists), `carry_over` (future slot or non-session day — rows belong to a prior session), or `provisional_unconfirmed` (could be today OR a prior session and today could NOT be positively confirmed — e.g. live after 14:30, or after close before the confirmed daily is posted). `as_of` is a full ISO datetime only for `observed`/`inferred` and is `null` for `carry_over`/`provisional_unconfirmed` — it is never silently upgraded.
  - `today_available` (bool): true only when today's data is positively confirmed (`observed`/`inferred`). `is_prior_session` (bool) and `warning` ({code, message} when `carry_over`, else null) flag prior-session leftovers. `as_of_date` is null for `provisional_unconfirmed` and the prior XKRX session DATE for `carry_over` — never a fabricated time.
  - `last_confirmed_session_date`: most recent confirmed session (Naver-recent when available, else the previous XKRX session).
  - `confirmed`: embedded confirmed multi-day series (source `naver`) carrying `foreign_ownership_pct` (외인소진율), `foreign_ownership_trend` (up/down/flat), `foreign_ownership_rate_change` (pp), and `history` (last 5 confirmed days of foreign/institution/individual net-buy + close). Best-effort: on Naver failure the KIS block stays intact and `confirmed.error` is set.
  - This is not a confirmed daily close figure and should not be mixed with `get_investor_trends` day/week/month history.
- `get_toss_buy_balance(symbol)`
  - Toss orderbook balance rate (buyBalanceRate/sellBalanceRate) and foreigner holding ratio — NOT user buy ratio. Live per-call, operator-gated. Disabled by default (returns `status='disabled'` unless `TOSS_CONSUMER_SIGNALS_ENABLED=true` is set). Korean stocks only.
- `get_toss_ai_signal(symbol)`
  - Toss AI signal (direction + reasoning). Live per-call, operator-gated. Disabled by default (returns `status='disabled'` unless `TOSS_CONSUMER_SIGNALS_ENABLED=true` is set). Korean stocks only.
- `get_volume_profile(symbol, market=None, period=60, bins=20)`
- `get_order_history(symbol=None, status="all", order_id=None, limit=50, account_mode=None)`
  - `status="pending"` 만 symbol 없이 호출 가능
  - `status in {"all", "filled", "cancelled"}` 는 symbol 필요
  - filled/cancelled 조회는 시장별 historical endpoint 제약 때문에 symbol fan-out을 자동 수행하지 않음
- `save_trade_journal(symbol, thesis, ..., paperclip_issue_id=None)` - Save the thesis, strategy, account context, and optional Paperclip issue link for a trade.
- `get_trade_journal(symbol=None, status=None, ..., paperclip_issue_id=None)` - Query active journal entries by symbol/account or reverse-lookup a journal from a Paperclip issue ID.
- `update_trade_journal(journal_id=None, symbol=None, ...)` - Activate, close, stop, or adjust the latest matching journal entry.
- `get_latest_market_brief(symbols=None, market=None, limit=10)` - Return concise latest AI analysis context for recent or selected symbols.
- `get_market_reports(symbol, days=7, limit=10)` - Return detailed AI analysis report history and decision trend for one symbol.
- `place_order(symbol, side, order_type="limit", quantity=None, price=None, amount=None, dry_run=True, reason="", exit_reason=None, thesis=None, strategy=None, target_price=None, stop_loss=None, min_hold_days=None, notes=None, indicators_snapshot=None, defensive_trim=False, approval_issue_id=None, account_mode=None)`
  - `side="buy"` 이고 `dry_run=False` 인 경우 `thesis` 와 `strategy` 가 필수
  - 실매수 성공 시 trade journal draft를 자동 생성하고 fill 저장 후 active로 연결 시도
  - 실매도 성공 시 동일 symbol의 active journal을 FIFO 기준으로 auto-close 시도
  - 부분 매도는 quantity를 수정하지 않고, fully-consumed journal만 close한다
  - journal close 실패는 주문 성공을 되돌리지 않고 `journal_warning` 으로 응답한다
  - `defensive_trim=True` 는 ROB-164/ROB-166 승인 기반 제한 경로이며 `(a) side="sell"`, `(b) order_type="limit"`, `(c) `approval_issue_id` 가 Paperclip `done` 상태, `(d) middleware-extracted caller identity 가 Trader agent 와 일치할 때만 평균단가 1% 매도 floor 를 우회한다
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None, dry_run=True, account_mode=None)`
- `cancel_order(order_id, symbol=None, market=None, account_mode=None)`
  - US equities: resolves exchange from symbol DB, open orders, and recent history before cancel
  - When symbol is omitted, KR/US auto-lookup is best effort and may fail if the order cannot be reconstructed
  - Discord button flows: `cancel_order(order_id="...", market="...")` — symbol auto-lookup enabled
- `modify_order` Discord button flow example:
  - `modify_order(order_id="...", symbol="...", market="...", new_price=123.45, dry_run=false)`
- `screen_stocks(...)` - Screen stocks across different markets (KR/US/Crypto) with various filters. **Single candidate-discovery entrypoint.**
- `screen_stocks_snapshot(preset=None, presets=None, market="kr", filters=None, exclude_watched=false, exclude_held=false, exclude_symbols=None, min_analyst_count=None, min_analyst_buy_count=None, min_market_cap=None, min_market_cap_eok=None, max_market_cap_eok=None, sort=None, limit=40, offset=0)`
  - Snapshot-backed discovery workflow. Pass either `preset="consecutive_gainers"` or `presets=["consecutive_gainers", "double_buy"]`; `preset` also accepts a comma-separated list for compatibility.
  - Returns symbols that matched the preset(s) from the persisted daily snapshots.
  - Supports multi-preset sweeps with symbol deduplication and `matchedPresets` tagging.
  - `exclude_held` (bool): hide symbols already in the KIS-live portfolio; if KIS holdings degrade, the response keeps results and emits a warning.
  - `exclude_watched` (bool): accepted for compatibility, but currently unsupported in MCP because no user watchlist context is wired; requests emit an explicit warning.
  - `exclude_symbols`: explicit symbols to remove after dedupe.
  - `min_analyst_count` (int): quality filter — filters enriched results by consensus total coverage.
  - `min_analyst_buy_count` (int): compatibility filter — filters enriched results by consensus buy count.
  - `min_market_cap` (float): size filter using raw numeric `marketCapValue` (`KRW` for KR, `USD` for US/crypto).
  - `min/max_market_cap_eok` (float): KR compatibility size filter — unit is 1억원.
  - `sort="matched_presets_desc"`: ranks intersections (stocks in multiple presets) first.
  - `filters` list: tune preset thresholds (threaded for `consecutive_gainers` and `crypto`).
  - Returned rows include `analysisContext` (consensus, RSI) and `isHeld` status.
  - Results are capped (default 40) and paginated. Check `pagination` in payload.
  - Preset sweeps are capped at 5 presets. Analyst filters are capped at 200 merged rows before enrichment; narrow with preset, market cap, or explicit symbols first.
  - Minimum market-cap filters exclude rows with missing `marketCapValue` and report the excluded count in `warnings`.
  - Crypto snapshot examples:
    - `screen_stocks_snapshot(preset="crypto_high_volume", market="crypto", limit=40)`
    - `screen_stocks_snapshot(preset="crypto_momentum", market="crypto", filters=[{"field":"trade_amount_24h","operator":"gte","value":10000000000}], limit=40)`
  - Use `get_crypto_top_movers` for live Upbit top movers; use `screen_stocks_snapshot(..., market="crypto")` for persisted snapshot-backed filtering.
- `get_top_stocks(market="kr", ranking_type="volume", limit=20)` - Cross-market rankings. Crypto supports `volume`, `gainers`, `losers`, and `relative_strength`.
- `get_crypto_top_movers(ranking_type="relative_strength", limit=20)` - Crypto-only Upbit KRW discovery wrapper. Default ranking sorts non-BTC coins by 24h outperformance vs KRW-BTC.
- `get_upbit_altseason(include_constituents=false, constituents_limit=50)` - Upbit altseason ratio and 24h breadth. With constituents enabled, `breadth.constituents` lists KRW alts beating BTC with 24h change, vs-BTC relative strength, volume, and traded value.
- ~~`recommend_stocks(...)`~~ — **DEPRECATED / registry-hidden (ROB-359).** No longer registered on the MCP tool surface. Use `screen_stocks` for candidate discovery. The implementation is retained in `analysis_tool_handlers.recommend_stocks_impl` for a possible future narrow `build_buy_plan` tool; do not call it from active report/operator prompts.

- `analyze_stock_batch(symbols, market=None, include_peers=False, quick=True, decision_history_account_mode=None)`
  - Legacy/deep-dive batch analysis for up to 10 symbols.
  - Do not use it as the routine follow-up after `screen_stocks_snapshot`; snapshot
    rows now expose consensus and RSI context directly.
  - Keep using it when support/resistance or full `quick=False` analysis is needed
    for symbols outside the snapshot result path.
  - Default `quick=True` returns compact summary with: symbol, current_price,
    rsi_14, consensus, recommendation, supports (top 3), resistances (top 3).
  - When a non-stale `analysis_artifact` already covers a symbol, that symbol's
    compact summary also carries a `fresh_artifact_exists` hint
    (`{artifact_uuid, as_of, kind}`) so you can choose to reuse the persisted
    artifact via `analysis_artifact_get` instead of re-deriving. This is a soft
    hint only — the analysis still runs and is returned.
  - `decision_history_account_mode="kis_mock"` switches only the advisory
    `decision_history` block to the explicit mock/counterfactual branch. Leave it
    unset for default live/default lesson context.
  - Compact US rows carry the same quote provenance fields as `get_quote` when present: `session`, `data_state`, `data_state_reason`, `price_source`, `venue`, `quote_asof`, and `delayed`.

### Snapshot-backed report generation

Snapshot-backed generator/Hermes MCP tools are registered only when
`SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true`. With the default `false`
setting they are physically absent from the MCP surface instead of returning
disabled no-op payloads:

- `investment_report_generate_from_bundle`
- `investment_report_prepare_bundle`
- `investment_report_get_hermes_context`
- `investment_report_create_from_hermes_composition`
- `investment_stage_artifacts_ingest_from_hermes`
- `investment_report_prepare_intraday_context`

### Session Context Tools

`session_context_append(entries)` persists append-only operator context for
cross-session handoff. It is for "where did we leave off?" state: plans,
decisions, deferred items, rejected candidates, constraints, open questions,
next actions, and handoff notes. It is not an investment report, research
session, trade journal, watch alert, or order ledger.

Each entry accepts:

- `kst_date` optional `YYYY-MM-DD`; defaults to current KST date.
- `market` required: `kr`, `us`, or `crypto`.
- `account_scope` optional: `kis_live`, `kis_mock`, `alpaca_paper`, `upbit_live`.
- `entry_type` required: `plan`, `decision`, `deferred`,
  `rejected_candidate`, `constraint`, `open_question`, `next_action`,
  `handoff_note`.
- `title` required short title.
- `body` required markdown body.
- `refs` optional object: `report_uuid`, `item_uuid`, `alert_uuid`, `order_id`,
  `journal_id`, `symbols`.
- `created_by` optional: `claude`, `operator`, `system`; defaults to `claude`.
- `session_label` optional grouping label.

`session_context_get_recent(market?, account_scope?, kst_date_from?, entry_type?, limit)`
returns recent entries newest first. `limit` is clamped to 1..100 and defaults
to 20. New trading sessions should call this before comparing yesterday's plan
with today's candidate tournament.

### Analysis Artifact Tools

`analysis_artifact_save(market, kind, title, symbols?, payload?, as_of?, valid_until?, created_by?, session_label?, correlation_id?, account_scope?, readiness_label?)`
persists a structured analysis artifact for cross-session reuse. It is for the
durable outputs of analysis runs — screening rankings, profit-taking verdicts,
support/resistance maps, flow assessments, candidate pools, session summaries,
and briefings — so a later session can reuse them instead of recomputing. Save is
explicit only; `analyze_stock_batch` and other analysis runs do not auto-persist.
This is the cross-session artifact store; it is complementary to (not a duplicate
of) the ROB-638 fetch-layer Redis cache, which dedupes slowly-changing provider
fetches across calls within a run.

Each artifact accepts:

- `market` required: `kr`, `us`, or `crypto`.
- `kind` required: `screening_ranking`, `profit_taking_verdicts`,
  `support_resistance_map`, `flow_assessment`, `candidate_pool`,
  `session_summary`, `briefing`.
- `title` required short title.
- `symbols` optional string list; defaults to empty.
- `payload` optional JSON object; defaults to empty.
- `as_of` optional ISO datetime; defaults to now (UTC).
- `valid_until` optional ISO datetime; when in the past the artifact is stale
  and excluded from `analysis_artifact_list` unless `include_stale=true`. **When
  omitted, the server assigns a per-kind default TTL** (price/screen kinds expire
  at the end of the `as_of` KST day; `session_summary`/`briefing` at the end of
  the next KST day) so an artifact is never never-stale.
- `created_by` optional: `claude`, `operator`, `system`; defaults to `claude`.
- `session_label` optional grouping label.
- `correlation_id` optional idempotency key. Re-saving the same `correlation_id`
  updates the row in place (omit to append a new row).
- `account_scope` optional grouping/filter label.
- `readiness_label` optional advisory (caller-declared, not a gate):
  `screen_grade`, `not_decision_ready`, `ready_for_order_review`, `blocked`.

The response includes `action` and the saved artifact. `action` is `created`
(new row), `updated` (correlation_id re-save whose payload changed — `version` is
bumped in place), or `unchanged` (correlation_id re-save whose canonical payload
hashed identical — no write, `version` preserved). Each artifact carries a
server-computed `content_hash` (over the canonical payload JSON) and an integer
`version`.

`analysis_artifact_list(market?, kind?, symbol?, since?, include_stale?, limit, correlation_id?, account_scope?)`
returns matching artifacts newest `as_of` first. `symbol` does a containment
match on the `symbols` array. `limit` is clamped to 1..100 and defaults to 20.
Stale rows (`valid_until` in the past) are excluded unless `include_stale=true`.
`correlation_id` and `account_scope` are optional exact-match filters (the same
labels set on `analysis_artifact_save`).

`analysis_artifact_get(artifact_id)` returns a single artifact including the
full payload, by numeric `id` or `artifact_uuid` string. Missing ids return
`success=false` with `error="not_found"`.

### Investment Report Tools

- `investment_report_add_items(report_uuid, items, actor=None)` - Append new proposal items to an existing draft investment report. The item payload contract matches `investment_report_create`. Duplicate `client_item_key` rows are returned as existing items and are not rewritten. Non-draft reports return `error="not_draft"`. No broker, order, or watch mutation is performed.
- `investment_report_update(report_uuid, title=None, summary=None, risk_summary=None, thesis_text=None, no_action_note=None, market_snapshot=None, portfolio_snapshot=None, metadata=None, valid_until=None, actor=None, reason=None)` - Update draft report header fields without changing report identity, lifecycle status, predecessor chain, account scope, generator version, or items. Each successful update appends an audit entry to `report.metadata.draft_updates`. Non-draft reports return `error="not_draft"`.
- `kis_mock_mirror_execute_report(report_uuid, dry_run=True, min_rung_quantity=1.0, confirm=False)` - Execute ROB-734 mirror counterfactual orders through KIS mock only. `dry_run=True` returns per-item `plan` previews including symbol, side, quantity/amount, limit price, target/stop, correlation id, source bucket, and WATCH approximation notes. `dry_run=False` requires `confirm=True`; otherwise the tool fails closed with `error_code="mirror_confirm_required"`. The planner mirrors only KR report items with `target_kind="asset"` and a six-digit numeric symbol. Non-KR, US, crypto, index, and FX items are skipped with `reason="non_kr_equity_out_of_mirror_scope"` and counted in `plan_skipped_count`; they are never submitted to `place_order`. WATCH thresholds are used as prices only when `watch_condition.metric == "price"`; non-price metrics such as RSI are skipped with `reason="unsupported_watch_metric_for_limit_price"`. Breakout/above WATCH conditions are labeled as `watch_approximation=limit_at_threshold`. If a KIS mock mirror send fails before a ledger row is written, the scoped mock mirror pre-send intent is released so the same report item can be retried. If a duplicate mock mirror intent is still present, the tool fails closed with mock-specific duplicate wording and operators should inspect `kis_mock_order_ledger.report_item_uuid` / `mirror_cohort` before manual cleanup.

### Alpaca paper read-only smoke tools

ROB-69 exposes Alpaca paper broker inspection via explicit read-only MCP tool
names only. These tools are registered under `MCP_PROFILE=us-paper`; they are
not part of the default or `hermes-paper-kis` surfaces.

- `alpaca_paper_get_account()`
- `alpaca_paper_get_cash()`
- `alpaca_paper_list_positions()`
- `alpaca_paper_list_orders(status="open", limit=50)`
- `alpaca_paper_get_order(order_id)`
- `alpaca_paper_list_assets(status="active", asset_class="us_equity")`
- `alpaca_paper_list_fills(after=None, until=None, limit=50)`
- `alpaca_paper_ledger_list_recent(limit=50, lifecycle_state=None)`
- `alpaca_paper_ledger_get(client_order_id)`
- `alpaca_paper_execution_preflight_check(...)`

`alpaca_paper_execution_preflight_check` is a read-only runner gate for the
later automated paper cycle. It reads recent ledger rows and accepts optional
caller-supplied read-only `open_orders`, `positions`, and `approval_packet`
snapshots, then returns severity-classified anomalies plus `should_block`. Scoped
callers may pass `lifecycle_correlation_id`, `client_order_id`, `candidate_uuid`,
`briefing_artifact_run_uuid`, or an `approval_packet` containing those keys; the
tool then reads only matching ledger rows and returns `scoped_by` so decision
sessions do not get blocked by unrelated recent ETH/SOL/BTC rows. Calls without
scope keep the broad recent-ledger safety behavior for global runners. Passing
`legacy_cycle_blockers_as_warnings=True` is an explicit Alpaca Paper execution
flow test-mode: residual positions and stale preview/approval packets are
returned as warnings instead of blockers so operators can test buy/sell/order
adjust/close flows on a used paper account. It does not weaken open-order
conflicts, duplicate `client_order_id`, ledger/order/fill anomalies, missing
linked sells, unclosed sell snapshots, or symbol mismatches; those remain
blocking. ROB-93
checks include unexpected open orders, residual positions, duplicate
`client_order_id`, filled buys without linked sells, filled sells without a zero
final position snapshot, ledger/order/fill mismatches, stale previews/approval
packets, and signal/execution symbol mismatches. Stale same-scope preview
findings return the dry-run action hint
`recommended_action="mark_stale_preview_cleanup_required"` and
`lifecycle_state="stale_preview_cleanup_required"`; operators can use this to
surface an explicit cleanup-required state before any separately approved repair
write. The preflight itself performs no broker mutation, no repair writes, and
no direct DB backfill.

The broker inspection tools instantiate `AlpacaPaperBrokerService`, so they
inherit the
service-level endpoint guard: the trading base URL must be exactly
`https://paper-api.alpaca.markets`. The Alpaca dashboard may display
`https://paper-api.alpaca.markets/v2`, but runtime env should **not** include
`/v2`; service methods append `/v2/...` paths internally, and setting the env to
`.../v2` would produce duplicated `/v2/v2/...` requests.

Safety boundary: there are no Alpaca live MCP tools. ROB-73 adds explicit
paper-only, confirm-gated `alpaca_paper_submit_order` and
`alpaca_paper_cancel_order` tools for dev-owned smoke, with no runtime live
switch and no bulk/by-symbol cancel. ROB-74 extends those explicit paper-only
surfaces to a narrow crypto contract; ROB-86 permits guarded paper sell/close
smokes through the same explicit Alpaca Paper submit surface. Crypto remains
buy/sell limit-only, allowlisted to `BTC/USD`, `ETH/USD`, and `SOL/USD`,
`time_in_force` limited to `gtc`/`ioc`, and capped at $50 max notional or
estimated cost.
There is still no Alpaca paper `place_order`, `replace_order`, `modify_order`,
`cancel_all`, close-position/liquidate, or generic Alpaca order-routing surface.

Read-only operator runbook: [`docs/runbooks/alpaca-paper-readonly-smoke.md`](../../docs/runbooks/alpaca-paper-readonly-smoke.md)
Read-only smoke helper: `scripts/smoke/alpaca_paper_readonly_smoke.py` (argumentless, read-only, exits non-zero on failure)
Dev submit/cancel smoke runbook: [`docs/runbooks/alpaca-paper-dev-smoke.md`](../../docs/runbooks/alpaca-paper-dev-smoke.md)
Dev submit/cancel smoke helper: `scripts/smoke/alpaca_paper_dev_smoke.py` (preview-only by default, side effects require dual explicit gates)

### Alpaca paper order preview

ROB-70 adds `alpaca_paper_preview_order`: a side-effect-free validator + echo tool.
ROB-74 extends preview to a narrow Alpaca paper crypto shape without adding any
broker side effects: `asset_class="crypto"` supports only `BTC/USD`, `ETH/USD`,
and `SOL/USD`, is buy/sell and limit-only, defaults omitted `time_in_force` to
`gtc`, rejects crypto `day`/`fok`, and is capped at $50 notional or estimated
cost.

**Signature:**
```
alpaca_paper_preview_order(
    symbol,          # US equity ticker or allowlisted crypto pair (uppercased)
    side,            # "buy" | "sell" (crypto: buy/sell limit-only)
    type,            # "market" | "limit"  (crypto: limit only)
    qty=None,        # Decimal quantity (xor notional)
    notional=None,   # Decimal notional USD (xor qty; crypto limit allowed)
    time_in_force=None,    # omitted => day for us_equity, gtc for crypto; crypto allows only "gtc" | "ioc"
    limit_price=None,      # required for limit orders, forbidden for equity market
    stop_price=None,       # always rejected (deferred)
    client_order_id=None,  # optional, 1-48 chars
    asset_class="us_equity",  # "us_equity" or ROB-74 "crypto"
)
```

**Validation rules (enforced before any service call):**
- `symbol`: non-empty after strip; uppercased; 1–10 chars
- `side`: `"buy"` or `"sell"`; case-insensitive
- `type`: `"market"` or `"limit"`; stop/stop_limit deferred
- `qty` xor `notional`: exactly one required
- `limit_price`: required for limit orders, forbidden for US-equity market orders, must be > 0
- `stop_price`: always rejected with explicit error
- `asset_class`: `"us_equity"` or `"crypto"`; other values rejected
- `time_in_force`: omitted/blank defaults to `"day"` for US equities and `"gtc"` for crypto; US equities allow `"day"`, `"gtc"`, `"ioc"`, `"fok"`; crypto allows only `"gtc"` or `"ioc"`
- For `asset_class="us_equity"`, `notional + type="limit"` is rejected (Alpaca only supports equity notional for market orders in this surface)
- For `asset_class="crypto"`, only `BTC/USD`, `ETH/USD`, and `SOL/USD` are supported; orders are buy/sell, limit-only, require `limit_price`, permit `notional + limit_price`, and cap notional or `qty * limit_price` at $50

**Return shape:**
```json
{
  "success": true,
  "account_mode": "alpaca_paper",
  "source": "alpaca_paper",
  "preview": true,
  "submitted": false,
  "order_request": { "symbol": "AAPL", "side": "buy", "type": "market", ... },
  "estimated_cost": "360" | null,
  "account_context": { "cash": "...", "buying_power": "..." } | null,
  "would_exceed_buying_power": false | null,
  "warnings": []
}
```

**Safety boundary:** Preview is a pure validator + echo. It does NOT call
POST `/v2/orders`. The only Alpaca paper side-effect tools are the explicit
confirm-gated `alpaca_paper_submit_order` and `alpaca_paper_cancel_order`
handlers. There is still no generic/live-capable `place_order`, `cancel_order`,
`modify_order`, `replace_order`, bulk cancel, or endpoint-switching tool for
Alpaca paper.

Account context (cash/buying_power) is fetched via read-only `GET /v2/account`
and fails soft: if unavailable, `account_context` is `null` and
`"context_unavailable"` is added to `warnings`. The preview still returns
`success: true` with the normalized `order_request` echo.

The endpoint guard applies: if `ALPACA_PAPER_BASE_URL` is ever set to the live
endpoint, the service constructor rejects it and the tool raises
`AlpacaPaperEndpointError` (fail closed).

### Account Routing

MCP account-facing tools use `account_mode` to avoid mixing DB simulation,
official KIS mock, and KIS live account paths:

- `account_mode="db_simulated"`: DB-backed paper trading only. No KIS broker
  calls. Existing `account_type="paper"`, `account_mode="paper"`, and
  `account_mode="simulated"` remain aliases and return warnings.
- `account_mode="kis_mock"`: official KIS mock/sandbox account. Uses KIS mock
  credentials only, passes `is_mock=True` to KIS broker methods, and fails
  closed if `KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`,
  `KIS_MOCK_APP_SECRET`, or `KIS_MOCK_ACCOUNT_NO` are missing. HTTP requests
  use `KIS_MOCK_BASE_URL`, which defaults to the official KIS mock host
  `https://openapivts.koreainvestment.com:29443`.
  KIS mock is a KIS venue only. It does not simulate Upbit crypto orders:
  symbols that resolve to crypto, such as `KRW-BTC`, fail closed with
  `error: "crypto has no mock venue"` before Upbit balance reads or order
  mutation calls.
- `account_mode="kis_live"` or omitted: existing live KIS behavior. For
  `place_order`, `dry_run=True` remains the default. KR live buy paths query
  Toss stock warnings before order submission; active `LIQUIDATION_TRADING`
  blocks non-dry-run buys before KIS POST, while lookup failures are fail-open
  and surfaced in the response metadata.
- **Buy balance pre-check (ROB-625)**: For `side="buy"`, both `dry_run=True` and
  `dry_run=False` apply the *same* orderable-cash pre-check against the shared
  `get_cash_balance` source. Insufficient balance returns `success=false` with an
  `insufficient_balance: true` flag and an `insufficient_balance_detail` block
  (`balance`, `order_amount`, `currency`, `shortfall`, and — for US — a KIS field
  `breakdown` exposing `frcr_dncl_amt1`/`frcr_gnrl_ord_psbl_amt`). On `dry_run=True`
  the preview body (estimated value, fee) is still returned so the operator can
  size a deposit. This closes the prior "dry_run passes → live rejects" gap.
- `account_mode="toss_live"`: official Toss Securities live KR/US account. Uses Toss credentials, maps to `toss_live` routing, and fails closed when `TOSS_API_ENABLED=false` or credentials are missing. Actual Toss order mutation POSTs also require `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true`; keep this false until the accepted-order ledger and operator live-smoke hold are cleared.

Do not use `account_type="paper"` for official KIS mock. It is always DB
simulation. Responses from updated surfaces include `account_mode`; deprecated
aliases include `warnings`.
  - Set `quick=False` for full analysis payload (like `analyze_portfolio`)
  - Example: `analyze_stock_batch(symbols=["NVDA", "AMZN", "MSFT", "GOOGL"], market="us")`

#### KIS mock unsupported endpoints

> See [`docs/kis-mock-tr-routing-matrix.md`](../../docs/kis-mock-tr-routing-matrix.md)
> for the full live ↔ mock TR routing matrix.

`account_mode="kis_mock"` returns explicit "mock unsupported" errors instead
of silently degrading for the following KIS endpoints, which are live-only on
the official KIS mock account:

- `inquire_integrated_margin` (`TTTC0869R`) — returns `OPSQ0002 없는 서비스 코드 입니다`
  on mock. Mock cash routes via `inquire_domestic_cash_balance` (`VTTC8434R`)
  instead.
- `inquire_overseas_orders` (`TTTS3018R`) — KIS does not publish a mock TR.
  Pending US history under `account_mode="kis_mock"` returns
  `errors: [{market: "equity_us", error: "kis_mock: overseas pending-orders
  inquiry ..."}]` and an empty orders list.
- `inquire_korea_orders` (`TTTC8036R`) — confirmed live-only on the mock
  account (returns `EGW02006 모의투자 TR 이 아닙니다`). Pending KR history
  and KR cancel/modify lookup under `account_mode="kis_mock"` return
  `mock_unsupported=true` errors instead of attempting the call.
- KIS overseas margin (`TTTC2101R` / `VTTS2101R`) — treated as
  mock-unsupported; the USD account row is omitted under
  `account_mode="kis_mock"` and the failure is reported in `errors[]`.

#### Operator runtime config

`account_mode="kis_mock"` reads only `KIS_MOCK_*` settings. To enable the
mock account in production, the operator should source a separate env file
(for example `~/services/auto_trader/shared/.env.kis-mock`) into the launchd
plist environment for the MCP / API processes — **never** merge mock
secrets into the live `.env.prod.native` file. When any of
`KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, or
`KIS_MOCK_ACCOUNT_NO` are missing, every mock surface returns:

```
{
  "success": false,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, ...",
  "source": "kis",
  "account_mode": "kis_mock"
}
```

The error names variables only — never values.

### Toss Live Order MCP Tools

The `default` profile registers eight typed `toss_live` MCP tools:
- `toss_preview_order`
- `toss_place_order`
- `toss_modify_order`
- `toss_cancel_order`
- `toss_get_order_history`
- `toss_get_positions`
- `toss_get_orderable_cash`
- `toss_reconcile_orders`

Operator activation and the one-share live smoke are documented in
[`docs/runbooks/toss-live-smoke.md`](../../docs/runbooks/toss-live-smoke.md).

#### Toss Safety Rules and Gates

- **API Enablement**: Toss live tools are default-disabled. They fail closed unless `TOSS_API_ENABLED=true` and `validate_toss_api_config()` returns no missing keys.
- **Account Mode Routing**: All Toss tools require `account_mode="toss_live"` (or `account_type="toss_live"`) and reject any mismatched account parameters.
- **Mutation Safety (Dry-Run, Confirm, and Activation Gate)**: All mutation tools (`toss_place_order`, `toss_modify_order`, `toss_cancel_order`) default to `dry_run=True`. They perform actual HTTP requests (POSTs) to Toss Securities only when `dry_run=False`, `confirm=True`, and `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true` are explicitly set. Keep `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=false` until the accepted-order ledger and operator live-smoke hold are cleared.
- **Accepted-only ledger and reconcile**: Real `toss_place_order` writes only an accepted/rejected row to `review.toss_live_order_ledger`. It does not create fills, journals, or realized PnL at send time. `toss_reconcile_orders(dry_run=True)` previews broker evidence from `GET /orders/{orderId}`; `dry_run=False` books only confirmed execution deltas. GET order-detail `403 non-json-response` failures are retried once after token reissue; unresolved failures are persisted as `requires_manual_review=true`. Mutation POSTs are not implicitly retried on that error.
- **US FX PnL split**: Toss order detail does not provide fill-time FX. For US rows only, `toss_reconcile_orders(dry_run=False)` captures USD/KRW through `exchange_rate_service` at reconcile time. Buy rows persist `buy_fx_rate`; sell rows persist `sell_fx_rate`, FIFO-attributed `fx_pnl_krw`, `security_pnl_usd`, `security_pnl_krw`, and `total_pnl_krw`. Automatic values are labelled `fx_rate_source="reconcile_spot"` and `fx_pnl_accuracy="approximate"`. Legacy lots with no buy FX keep FX PnL fields null until an operator backfills exact values through `modify_journal_entry`.
- **Fill Notifications (ROB-576)**: `toss_reconcile_orders(dry_run=False)` sends a Discord/Telegram fill notification only when `TOSS_FILL_NOTIFY_ENABLED=true`, the reconcile pass books a new fill delta, and the shared `TradeNotifier` has a KR/US webhook or Telegram fallback configured. Notifications reuse the existing fill card format and route by `market='kr'|'us'`; Toss fill enrichment is intentionally disabled (`enrichment=None`) until Toss account PnL/position enrichment exists. The optional paused TaskIQ task `toss_live.reconcile_periodic` calls `toss_reconcile_orders_impl(dry_run=False)` only when both `TOSS_LIVE_AUTO_RECONCILE_ENABLED=true` and `TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED=true`. It has no in-repo schedule; operator automation must register/unpause the cadence externally.
- **High-Value Orders**: KR orders with a computable notional value >= 100,000,000 KRW fail locally unless `confirm_high_value_order=True` is supplied.
- **KR Stock Warnings**: KR order previews include active Toss warning rows. Confirmed non-dry-run KR orders call the same warnings guard before mutation and block active `LIQUIDATION_TRADING`; Toss warning lookup failures are fail-open and reported as `warnings_check_message`.
- **Preview Market And Cost Context**: `toss_preview_order` is read-only but enriches the payload preview with Toss quote and cost context. It returns `current_price`, `current_price_currency`, `fill_distance` for off-market limit prices, `order_warnings` for marketability/fill-risk strings, `estimated_value`, `fee`, `fee_currency`, `fx_cost_full_conversion`, `fx_cost_full_conversion_currency`, and `estimated_costs`. The existing `warnings` field remains reserved for Toss stock-warning rows; string order warnings are not mixed into it. US `fx_cost_full_conversion` assumes the full order notional is converted KRW->USD and is labelled `fx_assumption="full_notional_krw_conversion"`; use `suggest_order_account` for cash-aware routing cost comparison.
- **Sell Loss-Sell Guard**: For sell orders and sell reprices, holdings cost basis is validated. Sells block locally if the execution price (limit) or current market proxy price (market) is below `average_purchase_price * 1.01`. If the holding/cost basis cannot be resolved, the sell fails closed.
- **Opposite Pending Orders**: Before placing a non-dry-run order, the tool queries all paginated `OPEN` order pages for the symbol and blocks the order if an opposite-side pending order already exists. Pagination anomalies fail closed.
- **Modify Semantics**:
  - KR modify requires both `new_price` and `new_quantity`.
  - US modify requires `new_price` and rejects `new_quantity`.
  - Cancel/modify responses surface `replacement_order_id` and semantic notes indicating that Toss issues a new replacement `orderId` instead of modifying/canceling the original one in-place.

> [!IMPORTANT]
> **Implementation Hold Status**: Toss live order MCP tools implemented under ROB-531 are under `hold_for_final_review`. Do not merge, deploy, or execute live Toss orders until a stronger review clears the safety boundaries and confirmation gates.

### `get_orderbook` spec
Parameters:
- `symbol`: KR equity symbol/code or Upbit market code (required)
- `market`: defaults to `"kr"`; supports KR aliases (`"kr"`, `"kospi"`, `"kosdaq"`, `"korea"`, `"kis"`, `"equity_kr"`) plus crypto aliases (`"crypto"`, `"upbit"`)
- `venue`: optional, KR equity only; selects the KIS trading venue for the orderbook. Non-blank values are rejected for crypto. Defaults to `"krx"` (KRX regular session) for backward compatibility.

Venue mapping (KR equity only):
| `venue` input | Canonical venue | KIS code | Korean label |
|---|---|---|---|
| `null`, `""`, `"krx"`, `"regular"`, `"j"` | `krx` | `J` | `KRX` |
| `"nxt"`, `"ntx"`, `"nx"`, `"afterhours"`, `"extended"` | `nxt` | `NX` | `NXT` |
| `"unified"`, `"combined"`, `"integrated"`, `"all"`, `"un"`, `"통합"`, `"통합시장"` | `unified` | `UN` | `통합` |

Behavior:
- KR requests follow the existing KR quote normalization path, including zero-padding numeric codes such as `5930 -> 005930`
- Crypto orderbook requests require explicit `market="crypto"` (or `"upbit"`) and a raw `KRW-*` symbol such as `KRW-BTC`; plain coins (`BTC`) and non-KRW crypto pairs (`USDT-BTC`) raise an argument error
- Providing a non-blank `venue` with `market="crypto"` raises an argument error
- Valid KR requests use KIS endpoint `inquire-asking-price-exp-ccn` (TR_ID `FHKST01010200`) and return 10-level asks/bids, total residual quantities, expected match metadata, and integer-valued `price`, `quantity`, `total_ask_qty`, `total_bid_qty`, and `spread`
- Valid crypto requests use Upbit orderbook data and return the same shared snapshot fields, but `price`, `quantity`, `total_ask_qty`, `total_bid_qty`, and `spread` can be fractional numbers
- `expected_qty` keeps the public `int | null` contract; when KIS leaves `output2.antc_cnqn` blank or omits it, the response serializes `expected_qty` as `null` instead of inventing a fallback quantity
- During the NXT after session (`15:30`-`20:00` KST; Toss market-calendar when available, corrected hardcoded fallback otherwise), KIS may return `expected_price` while leaving `expected_qty` blank or absent; this is treated as a valid upstream state, not an MCP error
- Successful responses always include MCP-only derived fields: `pressure`, `pressure_desc`, `spread`, `spread_pct`, `bid_walls`, and `ask_walls`
- Successful KR responses include venue diagnostics: `venue`, `venue_label`, `kis_market_code`, `source_endpoint`, `source_tr_id`, `is_empty_book`, `requires_final_recheck`, and (when empty) `empty_reason`
- Successful KR responses use `source: "kis"`, `instrument_type: "equity_kr"`, and return `bid_walls: []`, `ask_walls: []`
- Successful crypto responses use `source: "upbit"`, `instrument_type: "crypto"`, and may return non-empty wall arrays
- Invalid input raises; upstream failures for otherwise valid requests return an in-band error payload via the shared MCP error contract. When the underlying exception is a `DomainServiceError`, the payload may also include `error_type`
- `venue` does not affect order routing or trading venue defaults; this is market-data only. WebSocket streaming for NXT/UN is out of scope for this REST read-only slice.

Response format (KR equity):
```json
{
  "symbol": "005930",
  "instrument_type": "equity_kr",
  "source": "kis",
  "asks": [{"price": 70100, "quantity": 123}],
  "bids": [{"price": 70000, "quantity": 321}],
  "total_ask_qty": 1000,
  "total_bid_qty": 1500,
  "bid_ask_ratio": 1.5,
  "pressure": "buy",
  "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
  "spread": 100,
  "spread_pct": 0.143,
  "expected_price": 70050,
  "expected_qty": null,
  "bid_walls": [],
  "ask_walls": [],
  "venue": "nxt",
  "venue_label": "NXT",
  "kis_market_code": "NX",
  "source_endpoint": "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
  "source_tr_id": "FHKST01010200",
  "is_empty_book": false,
  "requires_final_recheck": false
}
```

`expected_qty: null` means KIS did not provide `antc_cnqn`; it does not by itself indicate a tool failure.

KR-only diagnostic fields:
- `venue`: canonical venue name (`"krx"`, `"nxt"`, `"unified"`)
- `venue_label`: Korean-facing label (`"KRX"`, `"NXT"`, `"통합"`)
- `kis_market_code`: KIS `FID_COND_MRKT_DIV_CODE` sent to KIS (`"J"`, `"NX"`, `"UN"`)
- `source_endpoint`: REST endpoint path used
- `source_tr_id`: KIS TR_ID used
- `is_empty_book`: `true` when the orderbook returned no asks and no bids
- `requires_final_recheck`: `true` for empty KR books (caller should re-check before acting on empty depth)
- `empty_reason`: stable short string (e.g. `"empty_kis_orderbook"`) when `is_empty_book` is `true`; absent when book is non-empty

Derived fields:
- `pressure` is derived from `bid_ask_ratio` using fixed inclusive boundaries:
  - `ratio > 2.0` -> `strong_buy`
  - `ratio > 1.3` (i.e. 1.3 excluded) -> `buy`
  - `ratio >= 0.7` (i.e. 0.7 included, up to 1.3 inclusive) -> `neutral`
  - `ratio >= 0.5` (i.e. 0.5 included, below 0.7) -> `sell`
  - `ratio < 0.5` -> `strong_sell`
- `pressure_desc` is a Korean interpretation string. `strong_buy`/`buy` use `total_bid_qty / total_ask_qty`, `strong_sell`/`sell` use `total_ask_qty / total_bid_qty`, and `neutral` is always `"매수/매도 잔량이 균형권 - 중립"`
- If `bid_ask_ratio` is `null`, both `pressure` and `pressure_desc` are `null`
- `spread` is `asks[0].price - bids[0].price` when both best levels exist; otherwise it is `null` (integer-valued for KR, fractional-capable for crypto)
- `spread_pct` is `(spread / bids[0].price) * 100`, rounded to 3 decimal places, and becomes `null` when the best bid is missing or `<= 0`
- `bid_walls` / `ask_walls` are MCP-only convenience fields. They are calculated only for crypto orderbooks by taking each side's `value_krw = round(price * quantity)`, using the side median as the baseline, selecting levels where `value_krw >= baseline * 2`, sorting by `value_krw` descending, and returning up to 3 entries shaped as `{price, size, value_krw}`

### Crypto sell orderable validation
- Crypto `place_order(..., side="sell")` uses **orderable balance only** (`balance` from Upbit `/v1/accounts`), not total holdings (`balance + locked`).
- `locked` coins are already committed to pending orders and cannot be sold.
- `quantity=None` (full sell) defaults to the orderable balance.
- If `quantity > orderable balance`, the tool returns `success: false` with an error containing `requested`, `orderable`, and `locked` values instead of forwarding to Upbit.

### Crypto stop-loss cooldown (Phase 2 strategy)
- `place_order(..., side="buy", market="crypto")` may reject buys while a stop-loss cooldown is active; returns `success: false` with cooldown message
- `place_order(..., side="sell", market="crypto")` automatically records an 8-day stop-loss cooldown after a non-dry-run sell when `current_price <= avg_buy_price * (1 - 0.045)` (4.5% stop-loss)
- Dry-run sells do not record cooldown; profitable sells (above stop-loss threshold) do not record cooldown

### KR order routing
- Domestic order tools (`place_order`, `modify_order`, `cancel_order` with `market="kr"`) use the new KIS TR IDs (`TTTC0012U/TTTC0011U/TTTC0013U`, mock: `VTTC0012U/VTTC0011U/VTTC0013U`).
- Domestic order requests (`order-cash`, `order-rvsecncl`) route with `EXCG_ID_DVSN_CD="SOR"`.

### US symbol/exchange resolution
- US symbol search and order routing resolve from DB table `us_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for US symbol/exchange lookups.
- If symbol/name is missing, inactive, or ambiguous in `us_symbol_universe`, tools return explicit lookup errors with sync hint.
- US prerequisite: run `make sync-us-symbol-universe` (or `uv run python scripts/sync_us_symbol_universe.py`) right after migrations.

### KR symbol resolution
- KR symbol search resolves from DB table `kr_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for KR name/symbol lookups.
- If symbol/name is missing, inactive, or ambiguous in `kr_symbol_universe`, tools return explicit lookup errors with sync hint.
- KR prerequisite: run `make sync-kr-symbol-universe` (or `uv run python scripts/sync_kr_symbol_universe.py`) right after migrations.

### Upbit symbol resolution
- Upbit crypto symbol/market resolution uses DB table `upbit_symbol_universe` only.
- Runtime does not call Upbit `/v1/market/all`; that endpoint is sync-path only.
- If `upbit_symbol_universe` is empty/unavailable, tools fail fast with explicit sync hint.
- If a coin/market lookup is missing or inactive in `upbit_symbol_universe`, MCP tools generally propagate explicit lookup errors (no silent fallback/default ticker).
- `get_holdings` and `get_position` are exceptions: missing/inactive Upbit holdings coins are silently skipped at collection time, while universe-level fatal states (for example empty/unavailable) still fail fast.
- `search_symbol` (crypto) uses DB-backed `search_upbit_symbols` only; in-memory map-based search is removed.
- Upbit prerequisite: run `make sync-upbit-symbol-universe` (or `uv run python scripts/sync_upbit_symbol_universe.py`) right after migrations.
- Scheduled sync task `symbols.upbit.universe.sync` runs daily at `06:15` KST (`cron: 15 6 * * *`, `cron_offset: Asia/Seoul`).

### `get_indicators` spec
Parameters:
- `symbol`: Asset symbol/ticker
- `indicators`: Indicator list (e.g. `rsi`, `sma`, `obv`)
- `market`: Optional explicit market (`crypto`, `kr`, `us`)

Symbol/market contract:
- `market` is required when `symbol` is a plain alphabetic token (for example `AAPL`, `ETC`).
- If omitted for plain alphabetic symbols, `get_indicators` raises:
  - `"market is required for plain alphabetic symbols. Use market='us' for US equities, or provide KRW-/USDT- prefixed symbol for crypto."`
- Crypto symbols continue to support prefix-based routing (`KRW-` / `USDT-`) and can omit `market`.
- This requirement is specific to `get_indicators`; other tools keep their existing routing behavior.

Examples:
- Allowed: `symbol="AAPL", market="us"`
- Allowed: `symbol="KRW-ETC", market="crypto"`
- Allowed: `symbol="KRW-ETC"` (market omitted)
- Rejected: `symbol="ETC"` (market omitted)

### `analyze_stock` spec

Parameters:
- `symbol`: Asset symbol/ticker/code (required; string or int accepted)
- `market`: Market - `"kr"`, `"us"`, `"crypto"` (optional, inferred from symbol if omitted)
- `include_peers`: Whether to include sector peer analysis for KR/US equities (default: false; ignored for crypto)

Response notes:
- Equity responses (KR/US markets) include `recommendation.rsi14` when RSI(14) is available from the indicator payload
- This field provides a convenient summary; callers should continue to use `get_indicators` when they need the full indicator set rather than the summarized recommendation field

### `get_correlation` spec
Parameters:
- `symbols`: List of asset ticker/code inputs (required, 2-10 entries)
- `period`: Lookback window in days (default: 60, minimum effective value: 30, maximum: 365)

Symbol contract:
- `get_correlation` has no `market` parameter and therefore accepts ticker/code inputs only.
- Mixed-market ticker/code inputs continue to work, including KR codes such as `005930`, US tickers such as `AAPL`, and crypto symbols such as `KRW-BTC`.
- Company-name inputs such as `삼성전자` or `Apple Inc.` are rejected with:
  - `"get_correlation does not support company-name inputs because it has no market parameter. Use ticker/code inputs directly."`
- When at least 2 ticker/code inputs resolve and fetch successfully, the tool still returns a correlation matrix and includes failed symbols in `errors`.

### `get_earnings_calendar` spec

Parameters:
- `symbol`: Optional equity ticker/code. US examples: `AAPL`, `MSFT`; KR examples: `005930`, `A005930`.
- `from_date`: Optional ISO start date, inclusive. Defaults to server `today`.
- `to_date`: Optional ISO end date, inclusive. Defaults to `from_date + 30 days`.
- `market`: Optional explicit market (`us`, `kr`). If omitted, 6-digit or A-prefixed KR codes route to KR; other non-crypto symbols route to US.

Behavior:
- US requests keep the existing Finnhub path and response shape: `symbol`, `instrument_type`, `source`, `from_date`, `to_date`, `count`, `earnings`.
- KR requests read existing `market_events` rows where `category="earnings"` and `market="kr"`.
- KR rows are read-only and may come from `source="wisefn"` scheduled earnings or `source="dart"` filings classified as earnings.
- KR response top-level includes `source="market_events"`, `sources`, `market="kr"`, `warning`, and the existing `earnings` list.
- KR `earnings` items include `symbol`, `company_name`, `date`, `hour`, `time_hint`, `quarter`, `year`, `status`, `source`, `source_event_id`, `source_url`, and `title`.
- KR `eps_*` and `revenue_*` fields are present for shape compatibility but usually `null` until realized-value joins are implemented.

Limitations:
- KR shareholder meetings, ex-dividend dates, IR, and conferences are not collected by this tool yet.
- Empty KR results mean no matching `market_events` rows are currently stored for the requested window; they do not prove there is no real-world event.
- Production WiseFn ingestion enablement and scheduler activation are operational follow-ups, not part of this MCP read-path contract.

Errors:
- Crypto symbols return an explicit error because earnings calendars apply to equities only.
- `from_date > to_date` is rejected.
- Explicit `market="us"` with a Korean equity code is rejected with guidance to use `market="kr"`.

### `get_disclosures` spec
Parameters:
- `symbol`: Korean corporation lookup input (required)
- `days`: Lookback window in days (default: 30)
- `limit`: Maximum filings to return (default: 20)
- `report_type`: Optional Korean disclosure group (`정기`, `주요사항`, `발행`, `지분`, `기타`)

Symbol contract:
- Direct 6-digit KR stock codes such as `005930` are passed through to OpenDartReader as-is.
- Korean company names such as `삼성전자` are supported on a best-effort basis through OpenDartReader's exact-name corp lookup.
- Blank or whitespace-only `symbol` inputs are rejected with an explicit in-band error payload (`success: false`, `error: "symbol is required"`, `filings: []`, `symbol: ""`).
- Company-name inputs that OpenDartReader cannot resolve return an explicit in-band error payload with `success: false`; they do not silently degrade to an empty `filings` list.

Behavior:
- `report_type` maps internally to DART disclosure kinds: `정기 -> A`, `주요사항 -> B`, `발행 -> C`, `지분 -> D`, `기타 -> E`.
- Unsupported `report_type` inputs return `success: false` instead of silently broadening the query.
- Successful responses return the existing `filings` list shape with `date`, `report_nm`, `rcp_no`, and `corp_name`.
- An empty DataFrame from OpenDartReader is treated as a successful lookup with `filings: []`.
- The first process-local client initialization still downloads the OpenDART corp-code cache, so cold-start latency can be higher than warm calls.

Error payload:
- Failure responses include `success`, `error`, `filings`, and `symbol`.

### `get_investment_opinions` spec
Parameters:
- `symbol`: Asset ticker/code input (required)
- `limit`: Maximum detailed opinion rows to return (default: 10)
- `market`: Optional explicit market (`kr`, `us`)

Behavior:
- KR requests keep the existing Naver Finance path and return recent analyst opinions plus consensus statistics.
- US requests use yfinance and keep the public top-level shape: `symbol`, `count`, `opinions`, `consensus`, plus optional `warning`.
- US `opinions` remains the recent Yahoo `upgrades_downgrades` event list (firm/rating/date plus row-level `target_price` when Yahoo provides one).
- US top-level `count` remains `len(opinions)`; it does **not** represent aggregate analyst coverage.
- US `consensus.total_count` is the aggregate analyst coverage count from the current Yahoo `recommendationTrend` / `ticker.recommendations` row (`period="0m"` preferred).
- US aggregate count mapping is:
  - `buy_count = strongBuy + buy`
  - `hold_count = hold`
  - `sell_count = sell + strongSell`
  - `strong_buy_count = strongBuy`
  - `total_count = strongBuy + buy + hold + sell + strongSell`
- US target statistics (`avg_target_price`, `median_target_price`, `min_target_price`, `max_target_price`, `current_price`, `upside_pct`) come from Yahoo `analyst_price_targets` after numeric normalization.
- US target normalization accepts Yahoo raw dicts such as `{raw, fmt}`, plain numbers, and pandas/numpy scalars; `0`, negative, empty, and non-numeric placeholders are treated as unavailable.
- When Yahoo analyst counts or target statistics are unavailable, the corresponding US `consensus` fields are returned as `null` instead of fabricated zeroes.
- When Yahoo provides neither usable aggregate counts nor usable analyst target data, the US response includes a top-level `warning`.

### `investment_report_create` item contract

`investment_report_create` persists one advisory report bundle and does not submit broker orders. The report idempotency key is `(report_type, market, market_session, account_scope, execution_mode, kst_date, generator_version)`. To create a new row for an updated draft, bump `generator_version` or another keyed field.

Each `items[]` object requires:
- `client_item_key`: caller-stable item key within the report.
- `item_kind`: `action`, `watch`, or `risk`.
- `intent`: `buy_review`, `sell_review`, `risk_review`, `trend_recovery_review`, or `rebalance_review`.
- `rationale`: human-readable thesis.

Optional typed item fields:
- `evidence`: `[{source, metric, value, as_of, freshness}]`; `source` is required.
- `freshness`: `fresh`, `soft_stale`, `stale`, or `unknown`.
- `entry_plan`: `[{label, price, quantity, notional, currency, condition, rationale}]`.
- `stop_loss`: `{price, quantity, notional, currency, condition, rationale}`.
- `target_price`: `{price, quantity, notional, currency, condition, rationale}`.
- `linked_order_ids`: `[{broker, account_scope, order_no, odno, ledger_id, report_item_uuid, raw}]`.

The lite quality basis `item_evidence_lite` reads `evidence[]` and item-level `freshness`; arbitrary `evidence_snapshot` keys are not counted as typed evidence. Typed trade-plan fields round-trip under reserved keys in `items[].evidence_snapshot`.

Unknown top-level item keys are rejected with `error: "invalid_items"`. Put caller-specific extension data under `metadata` or raw `evidence_snapshot` explicitly.

Order linkage note: `linked_order_ids` is report-side reference metadata. For new live orders, pass the report item's `item_uuid` as `report_item_uuid` to the order tool so ROB-473 ledger audit linkage is populated.

Watch execution context fields:
- `trigger_checklist`: `string[]`; copied into watch alert notifications so the operator can re-check the trigger.
- `max_action`: structured watch execution-plan JSON. `account_mode` is required when `max_action` is present; it also requires `side` and exactly one of `quantity` or `notional`. Optional keys include `amount_krw`, `limit_price`, `limit_price_hint`, and `ladder_level`.
- Do not send `planned_action` in item input. `planned_action` is derived from `max_action` when Hermes watch payloads are built.

### `manage_watch_alerts` — removed (ROB-265)

The legacy Redis-backed `manage_watch_alerts` MCP tool was removed by
ROB-265 along with the `watch_alerts` / `watch_order_intent_ledger` /
`watch_scanner` Redis surface. Report-scoped watches now flow through
`investment_report_activate_watch` (which copies an approved watch
item into `investment_watch_alerts` as an immutable activation
snapshot) and the `investment_watch_scanner` job (which evaluates
those alerts, writes `investment_watch_events` with the full trigger
identity snapshot, and emits Hermes review-trigger notifications).
Watches are review triggers, not automatic order instructions —
delivery state is auditable per event row (`delivery_status` /
`delivery_reason` / `delivered_at` / `delivery_attempts`).

### `list_active_watches`

Read-only active watch discovery for `review.investment_watch_alerts`.

Parameters:
- `market`: optional `kr`, `us`, or `crypto`.
- `symbol`: optional exact symbol filter.
- `include_expired_status_rows`: default `false`. When `false`, only returns `status='active'` rows whose `valid_until` is still in the future. When `true`, includes rows that remain `status='active'` even if `valid_until` has passed, for scanner-lag diagnostics.
- `limit`: default `100`, clamped to `1..250`.

Response includes `active_watches[]` with `symbol`, `operator`, `threshold`, `valid_until`, `rationale`, `source_report_uuid`, and `source_item_uuid`.

### `get_operating_briefing`

Read-only one-call bootstrap for a new operating session.

Parameters:
- `market`: required `kr`, `us`, or `crypto`.
- `account_scope`: optional. Defaults are `kr/us -> kis_live`, `crypto -> upbit_live`.
- `session_context_limit`: default `10`, clamped by the session context service.
- `include_current_price`: default `true`.
- `cohort`: optional, default `live_gated`. Realized trade-journal cohort to load (e.g., `live_gated`, `mock_counterfactual`).
- `include_counterfactual_delta`: default `false`. When `true`, returns aggregates delta scoreboard comparing `live_gated` and `mock_counterfactual` cohorts.

Response sections:
- `holdings`: summary and top movers derived from `get_holdings`.
- `pending_orders`: pending-order snapshot with `expected_expiry` when factually derivable.
- `active_watches`: same active watch rows as `list_active_watches`.
- `latest_report`: latest report summary and item status counts, or `null`.
- `session_context`: recent ROB-516 handoff entries.
- `staleness`: per-section `as_of`, freshness, and unavailable reason where available. If an optional DB-backed section (`active_watches`, `latest_report`, or `session_context`) raises, the tool still returns `success=true`; that section is returned as an empty or null fallback and `staleness.<section>.freshness_status` is `unavailable` with `unavailable_reason`.
- `trading_scoreboards`: trading scoreboard or counterfactual delta metrics, depending on `include_counterfactual_delta` parameter.

The tool never submits, modifies, cancels, reconciles, activates, expires, or mutates orders/watches/session context.


### `get_trading_scoreboard`

Query setup-tagged trade-journal aggregates over closed round-trips reconstructed from fills.

Parameters:
- `market`: optional `kr`, `us`, or `crypto`.
- `account_mode`: optional.
- `date_from`: optional date (YYYY-MM-DD).
- `date_to`: optional date (YYYY-MM-DD).
- `setup_tag`: optional tag filter.
- `min_sample`: default `1`.
- `cohort`: default `live_gated`. Realized trade-journal cohort to load (e.g., `live_gated`, `mock_counterfactual`).
- `include_counterfactual_delta`: default `false`. When `true`, returns aggregates delta scoreboard comparing `live_gated` and `mock_counterfactual` paired by shared `report_item_uuid` where available. `correlation_id` is still considered for legacy rows, but live place-time IDs are account-scoped and should not be expected to equal `mirror:{item_uuid}`.
- `min_pair_threshold`: default `20`. Only affects `pairing_health`; it does not filter rows.
- When `include_counterfactual_delta=True`, `market`, `account_mode`, `date_from`, `date_to`, `setup_tag`, `min_sample`, and `min_pair_threshold` are passed into the delta builder and echoed under `filters`.

Returns Win-rate, expectancy (% and R-multiple), profit factor, average/worst MAE and MFE.

When `include_counterfactual_delta=true`, the response additionally carries:
- `pairing_diagnostics`: closed-trade and key-coverage counts used to explain why pairs did or did not form.
- `pairing_health`: `ok`, `warming_up`, or `needs_design_review` based on `paired_count`, closed sample availability, and `min_pair_threshold`.

**Order linkage note**: For report-originated live orders, passing `report_item_uuid` is required for counterfactual pairing; without it, `paired_count` can remain zero even when both live and mock cohorts have closed trades.


### `screen_stocks` spec
Parameters:
- `market`: Market to screen - "kr", "kospi", "kosdaq", "konex", "all", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sector`: Sector filter for KR/US stocks (default: None). Not supported for crypto or KR ETF/ETN requests
- `exclude_sectors`: Sector exclusion list for KR/US stocks (default: None). Values are de-duplicated case-insensitively for ASCII labels
- `instrument_types`: Instrument taxonomy filter list - "common", "preferred", "etf", "reit", "spac", "unknown" (default: None)
- `adv_krw_min`: Minimum 30-day average daily value in KRW. Use 1,000,000,000 for a conservative liquidity floor or 5,000,000,000 for an aggressive liquidity floor
- `market_cap_min_krw`: Minimum market capitalization in KRW (default: None)
- `market_cap_max_krw`: Maximum market capitalization in KRW (default: None)
- `sort_by`: Sort criteria - "volume", "trade_amount", "market_cap", "change_rate", "dividend_yield", "rsi" (default: crypto="rsi", KR/US="volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US; not supported for crypto)
- `max_per`: Maximum P/E ratio filter (not applicable to crypto)
- `min_dividend_yield`: Minimum dividend yield filter (accepts both decimal, e.g., 0.03, and percentage, e.g., 3.0; values > 1 are treated as percentages) (not applicable to crypto)
- `min_dividend`: Alias for `min_dividend_yield`. Accepts same format. If both specified, they must be equal
- `min_analyst_buy`: Minimum analyst buy count filter (default: None). Only supported for KR/US stocks (not ETF/ETN)
- `max_rsi`: Maximum RSI filter 0-100 (not applicable to sorting by dividend_yield in crypto)
- `limit`: Maximum results 1-100 (default: 50)

Market-specific behavior:
- **KR market**:
  - `market="konex"` screens KONEX only; `market="all"` screens KOSPI, KOSDAQ, and KONEX
  - Default `asset_type in {None, "stock"}` + `category=None` requests use tvscreener only when verified KR stock-query capabilities cover the request; otherwise they fall back to the legacy KRX/Naver path before entering tvscreener
  - Successful stock responses expose `meta.source = "tvscreener"` and include `adx`, `instrument_type`, and 30-day ADV fields when TradingView provides them
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy KRX fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy path falls back to OHLCV-based RSI enrichment
  - ETF/category requests stay on the legacy KRX/Naver path
  - KRX data cached with 300s TTL (Redis) + in-memory fallback
  - Trading date auto-fallback (up to 10 days back)
  - Category filter auto-limits to ETFs if `asset_type=None`
  - ETN (`asset_type="etn"`) not supported - returns error

- **US market**:
  - Default `asset_type in {None, "stock"}` requests use tvscreener only when verified US stock-query capabilities cover the request
  - US `category`/`sector` alias requests stay on the tvscreener path only when the TradingView sector filter capability is verified; otherwise they fall back to legacy before running the tv query
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy yfinance path falls back to OHLCV-based RSI enrichment
  - Successful stock responses expose `meta.source = "tvscreener"`, include `adx`, `instrument_type`, 30-day ADV fields, and preserve public enrichment fields (`sector`, `analyst_buy`, `analyst_hold`, `analyst_sell`, `avg_target`, `upside_pct`) from tvscreener when available
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy yfinance fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - Post-screen enrichment skips per-row Finnhub/yfinance fan-out when those public fields are already populated; missing fields fall back to lightweight yfinance/Finnhub enrichment
  - Unsupported or unverified tvscreener request-critical capabilities fall back to the legacy yfinance path
  - Legacy yfinance maps: `min_market_cap` → `intradaymarketcap`, `max_per` → `peratio.lasttwelvemonths`, `min_dividend_yield` → `forward_dividend_yield`
  - Legacy yfinance sort maps: `volume` → `dayvolume`, `market_cap` → `intradaymarketcap`, `change_rate` → `percentchange`
  - Legacy yfinance screen enrichment reuses a request-scoped session for repeated analyst-target lookups
  - Yahoo OHLCV (`day/week/month`) requests use Redis closed-candle cache at the service boundary
  - Closed-bucket cutoff uses NYSE session close via `exchange_calendars` (`XNYS`), including DST/holidays/early close

- **Crypto market**:
  - Default success path uses tvscreener `CryptoScreener` filtered by `EXCHANGE == "UPBIT"`
  - Default sort remains `sort_by="rsi"`, `sort_order="asc"`; a requested crypto `sort_by="rsi", sort_order="desc"` is coerced to ascending and reported in `warnings` plus `filters_applied.sort_order`
  - `trade_amount_24h` maps to TradingView `CryptoField.VALUE_TRADED` and keeps the public KRW traded-value contract
  - `volume_24h` keeps the legacy Upbit 24h volume meaning (`acc_trade_volume_24h`); `VOLUME_24H_IN_USD` is never used as a public replacement for either `trade_amount_24h` or `volume_24h`
  - Result symbols are normalized back to Upbit format such as `KRW-BTC`
  - Successful tvscreener responses still restore legacy public crypto fields including `rsi_bucket`, `market_cap_rank`, `market_warning`, `volume_ratio`, `candle_type`, `plus_di`, and `minus_di`
  - Warning/crash metadata (`filtered_by_warning`, `filtered_by_crash`) and CoinGecko cache metadata are preserved on the tvscreener success path
  - Stop-loss cooldown filter: symbols in an 8-day stop-loss cooldown window (after a stop-loss sell) are excluded from results; count available in `meta.filtered_by_stop_loss_cooldown`
  - `sort_by="volume"` is not supported for crypto and returns an error
  - Crypto response payload does not include `volume`; use `trade_amount_24h`
  - `market_cap` sorting is supported; public `market_cap` prefers CoinGecko cache values and falls back to TradingView `MARKET_CAP`, and final ordering uses that public value without silently falling back to `trade_amount_24h`
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored
  - `sector`, `exclude_sectors`, `instrument_types`, `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`, and `min_analyst_buy` filters are not supported for crypto - returns error

Filter compatibility and error semantics:
- `sector` filter: Supported for KR/US stocks only. Returns error for crypto or KR ETF/ETN requests
- `exclude_sectors`: Supported for KR/US stocks only. Cannot overlap with `sector`
- `instrument_types`: Supported for KR/US only. `asset_type="etf"` conflicts with `instrument_types=["common"]`
- `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`: Non-negative integers only. `market_cap_min_krw` must be less than or equal to `market_cap_max_krw`
- `min_analyst_buy` filter: Supported for KR/US stocks only (not ETF/ETN). Returns error for crypto or non-stock asset types
- `min_dividend` / `min_dividend_yield`: These are aliases. Accepts decimal (0.03) or percentage (3.0) formats. If both are specified with different values, returns error. Not supported for crypto
- `category` and `sector`: These are aliases for US market. If both are specified with different values, returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored

#### Crypto Composite Score Formula (`recommend_stocks`)

Crypto market uses a dedicated composite score formula instead of strategy-weighted scoring:

```
Total Score = (100 - RSI) * 0.4 + (Vol_Score * Candle_Coef) * 0.3 + Trend_Score * 0.3
```

**Components:**
- **RSI Score** (40%): `100 - RSI` - Lower RSI (oversold) gives higher score
- **Volume Score** (30%): `min(vol_ratio * 33.3, 100)` where `vol_ratio = today_volume / avg_volume_20d`
- **Trend Score** (30%): Based on ADX/DI indicators
  - `plus_di > minus_di` → 90 (uptrend)
  - `adx < 35` → 60 (weak trend)
  - `35 <= adx <= 50` → 30 (moderate trend)
  - `adx > 50` → 10 (strong trend, possibly exhausted)

**Candle Coefficient** (applied to volume score):
- Uses completed candle (index -2, fallback to -1)
- `total_range == 0` → coef=0.5, type=flat
- Bullish (close > open) → coef=1.0, type=bullish
- Lower shadow > body*2 → coef=0.8, type=hammer
- Body > range*0.7 and bearish → coef=0.0, type=bearish_strong
- Other bearish → coef=0.5, type=bearish_normal

**Default values for missing data:**
- RSI missing → rsi_score = 50
- ADX/DI missing → trend_score = 30 (conservative)
- Volume missing → vol_score = 0
- Final score is clamped to 0-100

**Crypto recommend_stocks behavior:**
- Top 30 candidates pre-filtered by 24h traded value (`trade_amount`)
- Enriched with composite metrics (RSI, ADX/DI, volume ratio, candle type)
- Sorted by composite score (descending)
- Equal-weight budget allocation
- `score` field is always numeric (0-100)
- Timeout/429 errors return partial results with warnings instead of failing

Advanced filters subset behavior (KR/US):
- **Note**: `min_market_cap` is NOT an advanced filter for KR/US - it uses already available KRX/yfinance fields and does not trigger extra fetches.
- Advanced filters (PER, dividend yield, RSI) require external enrichment in KR market.
- KR/US RSI enrichment subset limit: `min(len(candidates), limit*3, 150)`.
- Parallel fetch with `asyncio.Semaphore(10)`.
- Timeout: 30 seconds.
- Individual failures don't stop overall operation.

Crypto enrichment behavior:
- Uses a dedicated crypto composite enrichment subset: `min(max(limit*3, 30), 60)`.
- `min_market_cap` is not applied as a filter in crypto; it is returned as a warning only.

Response format:
```json
{
  "results": [
    {
      "code": "005930",
      "name": "삼성전자",
      "close": 80000.0,
      "change_rate": 0.05,
      "volume": 10000000,
      "market_cap": 480000000000000,
      "per": 15.0,
      "dividend_yield": 0.03,
      "rsi": 45.5,
      "adx": 23.1,
      "sector": "Technology",  // Industry sector (can be null for some stocks)
      "analyst_buy": 15,  // Number of analyst buy ratings (default: 0)
      "analyst_hold": 3,  // Number of analyst hold ratings (default: 0)
      "analyst_sell": 2,  // Number of analyst sell ratings (default: 0)
      "avg_target": 85000.0,  // Average analyst target price (can be null)
      "upside_pct": 6.25,  // Upside percentage based on analyst targets (can be null)
      "market": "kr"
      "market": "kr"
    }
  ],
  "total_count": 2400,  // Total stocks that passed all filters (before sort/limit). If data source provides total, uses that; otherwise uses fetched candidates count.
  "returned_count": 20,  // Actual number of results returned (after limit)
  "filters_applied": {
    "market": "kr",
    "asset_type": "stock",
    "sector": "Technology",  // Applied sector filter (if specified)
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "min_dividend_input": 3.0,  // Original min_dividend value if specified
    "min_analyst_buy": 5,  // Applied minimum analyst buy count filter
    "max_rsi": 70
    "asset_type": "stock",
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "max_rsi": 70
  },
  "meta": {
    "source": "tvscreener",
    "rsi_enrichment": {
      "attempted": 0,
      "succeeded": 0,
      "failed": 0,
      "rate_limited": 0,
      "timeout": 0,
      "error_samples": []
    }
  },
  "timestamp": "2026-02-10T14:20:59.123456"
}
```

### `recommend_stocks` spec (DEPRECATED — registry-hidden, ROB-359)
> **This tool is no longer registered on the MCP surface.** It is parked, not
> deleted: `recommend_stocks_impl` remains in `analysis_tool_handlers` for a
> future narrow `build_buy_plan` tool. The spec below documents the retained
> implementation only. For candidate discovery use `screen_stocks`.

Parameters:
- `budget`: Total budget to allocate (required, must be positive)
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `strategy`: Scoring strategy - "balanced", "growth", "value", "dividend", "momentum" (default: "balanced")
- `exclude_symbols`: List of symbols to exclude from recommendations (optional)
- `sectors`: List of sectors/categories to filter (uses first value only)
- `max_positions`: Maximum number of positions to recommend 1-20 (default: 5)

> Breaking change: `account` parameter is removed from `recommend_stocks`.

Strategy descriptions:
- **balanced**: 균형 잡힌 포트폴리오. RSI, 밸류에이션, 모멘텀, 배당을 균등하게 고려
- **growth**: 성장주 중심. 높은 모멘텀과 거래량 가중
- **value**: 가치투자 중심. 낮은 PER/PBR, 적정 RSI 가중 (max_per=20, max_pbr=1.5, min_market_cap=300억)
- **dividend**: 배당주 중심. 높은 배당수익률 가중 (min_dividend_yield=1.5%, min_market_cap=300억)
- **momentum**: 모멘텀 중심. 강한 상승 모멘텀과 거래량 가중

Strategy default thresholds (KR market):
- **value**: `max_per=20`, `max_pbr=1.5`, `min_market_cap=300` (억원)
- **dividend**: `min_dividend_yield=1.5` (percent), `min_market_cap=300` (억원)

2-stage relaxation (value/dividend only):
- When strict screening yields fewer candidates than `max_positions`, a fallback screening is triggered with relaxed thresholds:
  - **value**: `max_per=25`, `max_pbr=2.0`, `min_market_cap=200`
  - **dividend**: `min_dividend_yield=1.0`, `min_market_cap=200`
- Fallback candidates are added to fill remaining positions (deduped by symbol)
- For value strategy: candidates with missing PER/PBR receive score penalties (-12 for PER, -8 for PBR)
- For dividend strategy: candidates with missing or zero dividend_yield are excluded from fallback
- The `fallback_applied` field indicates whether fallback was used

Scoring weight factors:
- `rsi_weight`: RSI 기반 기술적 과매수/과매도 점수 비중
- `valuation_weight`: PER/PBR 기반 밸류에이션 점수 비중
- `momentum_weight`: 등락률 기반 모멘텀 점수 비중
- `volume_weight`: 거래량 기반 유동성 점수 비중
- `dividend_weight`: 배당수익률 기반 인컴 점수 비중

Behavior:
- Invalid `market` values raise `ValueError` (no silent fallback)
- Strategy-specific `screen_params` are applied per market and unsupported filters are ignored with warnings
- KR screens candidates using internal screener (max 100 candidates)
- Crypto prefilters top 30 candidates by 24h traded value, then enriches with RSI/composite metrics
- US uses `get_top_stocks(market="us", ranking_type="volume")` for candidate collection (max 50 candidates)
- Dividend threshold input is normalized as percent when `>= 1` (e.g., `1.0 -> 0.01`, `3.0 -> 0.03`)
- Excludes user holdings from all accounts (internal `account=None` query)
- Applies strategy-weighted composite scoring for KR/US (0-100); crypto uses dedicated composite score
- Sorts by score and allocates budget with integer quantities
- Remaining budget is added to top recommendation if possible

Response format:
```json
{
  "recommendations": [
    {
      "symbol": "005930",
      "name": "삼성전자",
      "price": 80000.0,
      "quantity": 10,
      "amount": 800000.0,
      "score": 75.5,
      "reason": "[balanced] RSI 45.0 (저평가 구간) | PER 12.0 (적정)",
      "rsi": 45.0,
      "per": 12.0,
      "change_rate": 2.5
    }
  ],
  "total_amount": 950000.0,
  "remaining_budget": 50000.0,
  "strategy": "balanced",
  "strategy_description": "균형 잡힌 포트폴리오 구성을 위한 전략...",
  "candidates_screened": 100,
  "diagnostics": {
    "raw_candidates": 100,
    "post_filter_candidates": 95,
    "per_none_count": 5,
    "pbr_none_count": 3,
    "dividend_none_count": 10,
    "dividend_zero_count": 2,
    "strict_candidates": 80,
    "fallback_candidates_added": 0,
    "fallback_applied": false,
    "active_thresholds": {
      "min_market_cap": 500,
      "max_per": null,
      "max_pbr": null,
      "min_dividend_yield": null
    }
  },
  "fallback_applied": false,
  "warnings": [],
  "timestamp": "2026-02-13T02:11:52.950534+00:00"
}
```

Crypto recommendation example (`market="crypto"`):
```json
{
  "recommendations": [
    {
      "symbol": "KRW-BTC",
      "name": "비트코인",
      "price": 142000000.0,
      "quantity": 1,
      "amount": 142000000.0,
      "score": 78.4,
      "reason": "Composite Score 78.4 | RSI 39.2(저평가) | 캔들 bullish | 거래량 1.3배",
      "rsi": 39.2,
      "per": null,
      "change_rate": 1.8,
      "volume_24h": 12543.21,
      "volume_ratio": 1.32,
      "candle_type": "bullish",
      "adx": 27.41,
      "plus_di": 31.52,
      "minus_di": 18.07
    }
  ],
  "total_amount": 142000000.0,
  "remaining_budget": 8000000.0,
  "strategy": "balanced",
  "warnings": [],
  "timestamp": "2026-02-15T00:00:00+00:00"
}
```

Diagnostics fields:
- `raw_candidates`: Number of candidates from screener
- `post_filter_candidates`: After normalization
- `per_none_count`: Candidates with missing PER
- `pbr_none_count`: Candidates with missing PBR
- `dividend_none_count`: Candidates with missing dividend_yield
- `dividend_zero_count`: Candidates with zero dividend_yield
- `strict_candidates`: After exclusion/dedup
- `fallback_candidates_added`: Additional candidates from 2-stage relaxation
- `fallback_applied`: Whether fallback screening was triggered
- `active_thresholds`: The strict stage thresholds used
- `fallback_thresholds`: (optional) Fallback thresholds if fallback was applied

Error response format (unexpected internal failure):
```json
{
  "error": "recommend_stocks failed: RuntimeError",
  "source": "recommend_stocks",
  "query": "market=kr,strategy=balanced,budget=5000000,max_positions=5",
  "details": "Traceback (most recent call last): ..."
}
```

### `get_cash_balance` spec
Parameters:
- `account`: optional account filter (`upbit`, `kis`, `kis_domestic`, `kis_overseas`)

Broker-specific contract:
- **Upbit (`account="upbit"`)**
  - `balance`: total KRW (`balance + locked`)
  - `orderable`: orderable KRW (`balance`)
  - `formatted`: formatted total KRW string (e.g. `"700,000 KRW"`)
- **KIS domestic (`account="kis_domestic"`)**
  - `balance`: `stck_cash_objt_amt` (`intgr-margin`)
  - `orderable`: domestic integrated-margin orderable (`stck_cash100_max_ord_psbl_amt`). ROB-596: this KIS field already nets accepted-but-unfilled buy orders in real time, so pending buys are **not** subtracted again (no double-count). It is the single source shared by `get_available_capital` and the `place_order` KRW buy pre-check.
- **KIS overseas (`account="kis_overseas"`)**
  - `balance`: USD cash balance (`frcr_dncl_amt1` fallback `frcr_dncl_amt_2`)
  - `orderable`: USD orderable cash (`frcr_gnrl_ord_psbl_amt`). ROB-596: this KIS field already nets accepted-but-unfilled buy orders in real time, so pending buys are **not** subtracted again (no double-count). It is the single source shared by `get_available_capital` and the `place_order` USD buy pre-check.
- **Toss (`account="toss"`, only when `TOSS_API_ENABLED=true`)**
  - `balance`: Toss buying power for the row currency
  - `orderable`: `0.0`; Toss portfolio integration is read-only in ROB-532, while order mutation tools are delivered separately
  - Emits one KRW row when KRW buying power is available and one USD row when USD buying power is available
  - If `account="toss"` is requested and the Toss API read fails, the tool fails closed; in all-account mode it records a partial `toss_api` error

Response shape:
- `accounts`: per-account cash entries
- `summary.total_krw`: sum of KRW `balance` fields
- `summary.total_usd`: sum of USD `balance` fields
- `errors`: per-source partial failures in non-strict mode

### `get_available_capital` spec
Parameters:
- `account`: optional account filter (`upbit`, `kis`, `kis_domestic`, `kis_overseas`, `toss`)
- `include_manual`: whether to include manual cash in aggregation (default: `true`)

Behavior:
- Aggregates orderable cash across all broker accounts (Upbit, KIS domestic, KIS overseas)
- Converts USD orderable amounts to KRW equivalents using current exchange rate
- Includes manual cash (Toss/non-API cash) when `include_manual=True`
- Marks manual cash as stale when older than 3 days

Response shape:
- `accounts`: per-account cash entries with `krw_equivalent` added for USD accounts
- `manual_cash`: manual cash details with `amount`, `updated_at`, and `stale_warning`
- `summary.total_orderable_krw`: total orderable amount in KRW across all sources
- `summary.exchange_rate_usd_krw`: USD to KRW exchange rate used for conversion
- `summary.as_of`: ISO timestamp of when the data was retrieved
- `errors`: per-source partial failures

### `get_holdings` spec
Parameters:
- `account`: optional account filter (`kis`, `upbit`, `toss`, `samsung_pension`, `isa`)
- `market`: optional market filter (`kr`, `us`, `crypto`)
- `include_current_price`: if `True`, tries to fetch latest prices and calculate PnL fields
- `minimum_value`: optional numeric threshold. When `None` (default), per-currency thresholds apply: KRW=5000, USD=10. Explicit number uses uniform threshold. Positions below threshold are excluded only when `include_current_price=True`

Filtering rules:
- If `include_current_price=False`, `minimum_value` filtering is skipped
- When `minimum_value=None`, per-currency thresholds are automatically applied based on `instrument_type`: `equity_kr` and `crypto` use 5000, `equity_us` uses 10
- When `minimum_value` is a number, that uniform threshold is applied to all positions
- KIS US holdings keep KIS-provided snapshot values when the KIS snapshot is numerically valid: `current_price > 0`, `evaluation_amount > 0`, and `profit_loss` / `profit_rate` are parseable numbers
- KIS US holdings fall back to Yahoo only when that KIS snapshot is missing or invalid; Yahoo is a fallback refresh path, not the default for valid KIS US holdings
- Upbit crypto current prices are fetched via batch ticker request (`/v1/ticker?markets=...`)
- During Upbit holdings collection, coins that raise `UpbitSymbolNotRegisteredError` or `UpbitSymbolInactiveError` on name lookup are silently skipped (not added to `errors`).
- Before batch ticker request, tradable markets are loaded from `upbit_symbol_universe` and only valid holdings symbols are included in the batch
- Non-tradable symbols (delisted/unsupported) are excluded from ticker request and treated as 0 value for `minimum_value` filtering (counted in `filtered_count`)
- Value is primarily based on `evaluation_amount`
- If current price lookup fails (`current_price=null`), value is treated as `0` for minimum filtering

Response contract additions:
- `filtered_count`: number of positions excluded by `minimum_value` filter
- `filter_reason`: filter status string, e.g. `minimum_value < 1000` or `equity_kr < 5000, equity_us < 10, crypto < 5000`
- `errors`: includes per-symbol price lookup failures for holdings price refresh (example fields: `source`, `market`, `symbol`, `stage`, `error`)
- `filters.minimum_value`: when `minimum_value=None` in the request, this field contains the per-currency threshold dict that was applied
- When `TOSS_API_ENABLED=true`, Toss Open API holdings are emitted with `broker="toss"`, `source="toss_api"`. `order_routable` (and `get_cash_balance` `orderable`, `/invest` home `isTradeable`/`sellableQuantity`) are gated on `TOSS_LIVE_ORDER_MUTATIONS_ENABLED` (ROB-549): reference-only (`order_routable=false`, `orderable=0`, `isTradeable=false`, `sellableQuantity=0`) while disabled, and routable with the API-provided `sellable_quantity` (Toss `/api/v1/sellable-quantity`) once the operator arms live mutations.
- When Toss API holdings succeed, duplicate Toss `manual_holdings` rows for the same market/symbol are hidden from normal output. KIS and Toss holdings for the same symbol are not deduplicated because they are separate broker subaccounts.
- When Toss API holdings fail, existing Toss `manual_holdings` rows remain visible as fallback and the response includes a partial `source="toss_api"` error.

Market routing:
- `market` can override routing: `crypto|upbit`, `kr|kis|krx|kospi|kosdaq`, `us|yahoo|nasdaq|nyse`
- If `market` is omitted, routing is heuristic: KRW-/USDT- prefix -> crypto, 6-digit code -> KR equity, otherwise -> US equity
- Crypto symbols must include `KRW-` or `USDT-` prefix

### `get_portfolio_allocation` spec

Parameters:
- `account`: optional account filter matching `get_holdings` and `get_cash_balance` (`kis`, `upbit`, `toss`, `samsung_pension`, `isa`, `paper`, `paper:<name>`)
- `market`: optional holdings market filter (`kr`, `us`, `crypto`); cash is still included when `include_cash=true` unless `account` excludes the cash account
- `include_cash`: include cash balances in the allocation denominator, default `true`
- `include_positions`: include per-position normalized rows, default `false`
- `target_weights`: optional mapping from asset class to target percent; when omitted, no over/underweight flags are emitted
- `drift_threshold_pct`: threshold for `overweight` / `underweight` labels when `target_weights` is provided, default `5.0`
- `account_mode`: same routing selector as `get_holdings` (`db_simulated`, `kis_mock`, `kis_live`)

Behavior:
- Read-only only. The tool performs no order preview, order placement, mutation, reconciliation, or live approval action.
- Converts USD holdings and USD cash to KRW using the same exchange-rate service used by portfolio cash tools.
- Aggregates direct US equity as `us_equity`, KR equity as `kr_equity`, Upbit holdings as `crypto`, and cash as `cash`.
- Looks through KR-listed ETFs when KRX ETF metadata is available. KR ETFs classified as `미국주식` by `app.services.krx.classify_etf_category()` are counted as effective `us_equity`, while their surface account remains KR/KIS/Toss.
- Non-US foreign, commodity, bond, and unclear ETF categories are counted as `other` rather than Korean equity.
- If KRX ETF metadata lookup fails, the tool records a degraded `krx_etf` error and keeps KR ETF positions in their surface `kr_equity` bucket.
- Positions whose valuation is unavailable are excluded from the denominator and listed in `warnings` with `reason="position_value_unavailable"`.

Response shape:
- `summary`: KRW total, invested value, cash value, valued/unvalued position counts
- `asset_classes`: value, weight, direct/look-through split, target/drift fields, and optional weight status
- `accounts`: account-level KRW roll-up with asset-class children and `profit_loss_krw` (cash sub-accounts carry 0)
- `lookthrough`: KR ETF rows whose effective exposure differs from surface exposure
- `positions`: returned only when `include_positions=true`
- `cash`: normalized cash rows when `include_cash=true`
- `errors`: broker, cash, exchange-rate, or KRX ETF partial failures
- `warnings`: non-fatal valuation omissions

### `get_trading_policy` spec

- `get_trading_policy(market, lane)`
  - Query trading policy judgment thresholds.
  - Read-only, single source `config/trading_policy.yaml`, operator-PR-edited (no write tool).
  - Args `market ∈ {kr,us,crypto}` × `lane ∈ {buy,sell,discovery}`.
  - An unknown key maps to `success=false, error=unknown_key`.
  - **Version-stamping contract**: consumers cite `{version, content_hash}` (from `get_trading_policy` or the `policy_version` field of `get_operating_briefing`) in `report_item.evidence_snapshot`, `trade_retrospectives`, and forecast records so the judging criteria are recoverable.
  - The buy-preview `sector_concentration` field is **fail-open** advisory (never blocks).

### route_request — advisory lane router (ROB-649)

`route_request(intent, market)` maps a coarse intent
(`buy_analysis`/`profit_taking`/`discovery`/`market_brief`) to the standard tool
sequence, advisory allowed/blocked tools, `get_trading_policy` thresholds +
version stamp, and hard constraints for that lane. Deterministic; registered on
every profile; read-only.

**Divergence from tradingcodex:** the original has no route MCP tool — it
injects lane guidance via a hook and maps lane→role→tool indirectly. auto_trader
exposes a **direct lane→tool advisory** tool with **no enforcement**. Blocking
middleware (mutation tools only, reads unrestricted, caller-header-keyed because
MCP session state resets on reconnect — ROB-469) is a separate follow-up issue.

Lane definitions come from the machine-readable `lanes:` blocks in
`docs/playbooks/trading-decision-playbook.md`; `route_request_lanes.LANE_SEQUENCES`
is kept in sync by `tests/test_route_request_registry_diff.py`. Every DEFAULT
tool must be classified into `READ_ONLY_ADVISORY_TOOLS` or a mutation set or CI
fails (silent-drift guard).

**Market-aware execution mapping (ROB-658):** the playbook lane sequences are
KR-centric — their place steps hard-code `toss_place_order`/`kis_live_place_order`.
On crypto/US profiles those tools are unregistered, so `route_request` substitutes
the market's generic execution surface via `MARKET_EXECUTION_TOOLS`
(`crypto`/`us` → `place_order`; `kr` → empty, already in the sequence). When a
lane places orders but none of its KR place tools survive the profile
intersection, the generic `place_order` is injected as the execution step and
counted as the lane's own mutation — so it appears in `standard_tool_sequence` +
`allowed_tools` instead of being misclassified into `blocked_actions`. KR output
is unchanged.


### User Settings Tools

- `get_user_setting(key)` - Get a user setting value by key. Returns the JSON value or None if not found.
- `set_user_setting(key, value)` - Set a user setting value by key (upsert). Returns the serialized setting with key, value, and updated_at.

These tools provide a generic key-value storage for user preferences and settings. Values are stored as JSON and can be any valid JSON-serializable data structure.

Common settings:
- `manual_cash`: Stores manually-managed cash amounts (e.g., `{"amount": 15000000}`) for accounts not backed by APIs (Toss, etc.)
- `account_costs`: Stores broker fee/cost profiles and thresholds used for routing suggestions.

### `account_costs` user setting

`set_user_setting(key="account_costs", value={...})` stores operator-maintained
broker cost metadata used by `suggest_order_account`, `get_available_capital`,
and `get_operating_briefing`.

Required shape:

```json
{
  "version": 1,
  "routing": {
    "position_consolidation_threshold_bps": {"kr": 25, "us": 40}
  },
  "accounts": {
    "kis_domestic": {
      "broker": "kis",
      "markets": {"kr": {"commission_bps": 14.7, "fx_spread_bps": 0}}
    },
    "kis_overseas": {
      "broker": "kis",
      "markets": {"us": {"commission_bps": 25, "fx_spread_bps": 20}}
    },
    "toss": {
      "broker": "toss",
      "limits": {"max_order_notional_krw": 1000000},
      "markets": {
        "kr": {"commission_bps": 0, "fx_spread_bps": 0},
        "us": {"commission_bps": 10, "fx_spread_bps": 1.7}
      }
    }
  }
}
```

Values are basis points. `25` means 0.25%. If the setting is missing or invalid,
the system uses default seed values and marks the result `review_required`.

### `update_manual_holdings` spec

Parameters:
- `holdings`: List of holding objects to upsert/remove (required)
- `broker`: Broker identifier - `"toss"`, `"samsung"`, `"kis"` (required)
- `account_name`: Account name - `"기본 계좌"`, `"퇴직연금"`, `"ISA"` (default: `"기본 계좌"`)
- `dry_run`: Preview mode without DB changes (default: `true`)

Holding object fields:
- `symbol`: Ticker/symbol (e.g., `"AAPL"`, `"005930"`, `"KRW-BTC"`). Takes precedence over `stock_name`.
- `stock_name`: Company name or alias (e.g., `"삼성전자"`, `"애플"`). Used for symbol resolution when `symbol` is not provided.
- `quantity`: Number of shares/coins (required for upsert)
- `avg_buy_price`: Average purchase price (optional). If not provided, calculated from `eval_amount`, `profit_loss`, and `quantity`.
- `eval_amount`: Current evaluation amount (optional, used for avg_price calculation)
- `profit_loss`: Unrealized profit/loss (optional, used for avg_price calculation)
- `profit_rate`: Profit rate percentage (optional, informational)
- `market_section`: Market type - `"kr"`, `"us"`, `"crypto"` (required)
- `action`: Operation - `"upsert"` or `"remove"` (default: `"upsert"`)

Validation rules:
- **US ticker resolution**: US holdings must use a real ticker or a pre-registered alias in `stock_alias` table.
- **US name-like input**: If a US name-like string fails lookup, the tool raises an error asking to add `stock_alias` mapping or supply the ticker directly.
- **US avg_buy_price**: Must be in USD. Values above `1000` are rejected with error: `"USD 단위로 입력해주세요 (현재 값: {value}, KRW로 의심됩니다)"`.
- **Quantity zero/negative**: `qty <= 0` payloads are treated as delete/cleanup intent:
  - If a matching holding exists, it is removed (same as `action="remove"`)
  - If no matching holding exists, a warning is generated
- **dry_run behavior**: When `dry_run=True`, no DB mutations occur. The response still includes `added_count`, `updated_count`, `removed_count`, `unchanged_count`, and `diff` so callers can validate the planned changes before execution. Dry-run diff actions are `would_add`, `would_update`, `would_remove`, and `unchanged`; live execution actions remain `added`, `updated`, and `removed`.

Response format:
```json
{
  "success": true,
  "dry_run": false,
  "message": "Holdings updated successfully",
  "broker": "samsung",
  "account_name": "기본 계좌",
  "parsed_count": 3,
  "holdings": [...],
  "warnings": [],
  "added_count": 1,
  "updated_count": 1,
  "removed_count": 1,
  "unchanged_count": 0,
  "diff": [...]
}
```

Dry-run remove preview example:
```json
{
  "success": true,
  "dry_run": true,
  "message": "Preview only (set dry_run=False to update DB)",
  "broker": "toss",
  "account_name": "기본 계좌",
  "parsed_count": 0,
  "holdings": [],
  "warnings": [],
  "added_count": 0,
  "updated_count": 0,
  "removed_count": 2,
  "unchanged_count": 0,
  "diff": [
    {"action": "would_remove", "ticker": "IONQ", "market_type": "US"},
    {"action": "would_remove", "ticker": "TSM", "market_type": "US"}
  ]
}
```

Error response format:
```json
{
  "success": false,
  "error": "USD 단위로 입력해주세요 (현재 값: 14966.0, KRW로 의심됩니다)"
}
```

### `get_user_setting` spec
Parameters:
- `key`: Setting key string (required)

Returns:
- The JSON value stored for the key, or `None` if the key doesn't exist

### `set_user_setting` spec
Parameters:
- `key`: Setting key string (required)
- `value`: Any JSON-serializable value (required)

Returns:
```json
{
  "key": "manual_cash",
  "value": {"amount": 15000000},
  "updated_at": "2026-04-01T08:00:00+00:00"
}
```

Behavior:
- Creates the setting if it doesn't exist, updates it if it does (upsert)
- `updated_at` is automatically set to the current timestamp
- The (user_id, key) pair is unique; attempting to create a duplicate key for the same user will update the existing entry

## Caller Identity Header (required)

All MCP callers (Scout, Trader, CIO bridges, and any future client) MUST send
`x-paperclip-agent-id: <calling agent's Paperclip agent id>` on every
`tools/call` request. The value is the caller's Paperclip agent id, not the
target trader agent id.

- The `CallerIdentityMiddleware` added in ROB-214 (ST-3.1) reads this header,
  stores it in a request-scoped contextvar, and records the extraction source
  (`http_header` | `env_fallback` | `none`) on each call.
- Caller-identity-gated tools (e.g. `place_order(..., defensive_trim=True)`
  after ST-3.2) reject calls where the contextvar is `None`, so a missing
  header in a production path is an outage, not a soft warning.
- Local dev / stdio transports that cannot send HTTP headers may export
  `MCP_CALLER_AGENT_ID` as an env fallback. This is a dev convenience only —
  production callers must send the header explicitly. `MCP_CALLER_AGENT_ID`
  MUST NOT be set in production HTTP deployments because it re-opens a caller
  spoofing vector for requests that omit `x-paperclip-agent-id`.

### Scout / Trader curl bridge

When an agent runs under a harness that does not register the auto_trader MCP
server in-process (current state for Scout and Trader on `claude_local`),
they use a JSON-RPC curl bridge at `/tmp/mcp_call.sh`. The canonical template
lives at `scripts/templates/mcp_call.sh.tmpl`; both agents MUST regenerate
their local `/tmp/mcp_call.sh` from that template so the header is present.

```bash
# From the repo root, per operator host/session:
export MCP_ENDPOINT="http://127.0.0.1:8765/mcp"
export MCP_AUTH_TOKEN="<value from env.MCP_AUTH_TOKEN>"
export MCP_SESSION_ID="<MCP session id>"
export PAPERCLIP_AGENT_ID="<calling agent's Paperclip agent id>"
envsubst '$MCP_ENDPOINT $MCP_AUTH_TOKEN $MCP_SESSION_ID $PAPERCLIP_AGENT_ID' \
  < scripts/templates/mcp_call.sh.tmpl > /tmp/mcp_call.sh
# 0700 — owner-only. The rendered script bakes MCP_AUTH_TOKEN in plaintext,
# so group/other read bits must be stripped.
chmod 700 /tmp/mcp_call.sh

# Smoke test — should return a tool payload, not 401/403/reject:
/tmp/mcp_call.sh get_quote '{"symbol":"005930","market":"kr"}'
```

The rendered bridge intentionally calls curl with `-N --max-time 15` and sends
`Connection: close`. It only consumes the first SSE `data:` line, so no-buffer
mode and the timeout keep the helper from holding a completed Paperclip run open
if the server keeps the stream alive.

If the Trader adapter is later migrated to an in-process MCP client (for
example a Claude Code `.mcp.json` entry or an SDK-level `default_headers`
config), that client must also set `x-paperclip-agent-id`; do not rely on
the shell bridge as the long-term header injection point.

## Run (docker-compose.prod)
Environment variables:
- `MCP_TYPE` : `streamable-http` (default) | `sse` | `stdio`
- `MCP_HOST` : `0.0.0.0`
- `MCP_PORT` : `8765`
- `MCP_PATH` : `/mcp`
- `MCP_GRACEFUL_SHUTDOWN_TIMEOUT` : `10` (seconds, HTTP transports only: `sse` / `streamable-http`)
- `MCP_USER_ID` : `1` (manual holdings 조회에 사용할 기본 사용자 ID)
- `MCP_CALLER_AGENT_ID` : DEV/stdio only — MUST NOT be set in production HTTP deployments (re-opens caller spoofing vector)

Example:
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

> Note: current prod compose uses `network_mode: host`, so port publishing is handled by the host network.

---

## MCP Profiles (ROB-56)

### Overview

The `MCP_PROFILE` env var selects which tool subset is registered at startup.

| Profile | Value | Order surface |
|---|---|---|
| Default | `default` (or unset) | Legacy `place_order`/`cancel_order`/`modify_order`/`get_order_history` + typed `kis_live_*` + typed `kis_mock_*`; crypto-only, Alpaca/us-dual paper, and Kiwoom tools are absent |
| Paper/mock-only | `hermes-paper-kis` | Typed `kis_mock_*` only — live surface **physically absent** |
| Crypto | `crypto` | Default read-only/research surface plus crypto-only tools (`get_crypto_fear_greed`, `get_crypto_market_regime`, `get_upbit_index`, ...) **plus** the generic `place_order`/`cancel_order`/`modify_order`/`get_order_history` (crypto live entry point) and `live_reconcile_orders`; typed `kis_live_*`/`kis_mock_*` are absent |
| US paper | `us-paper` | Default read-only/research surface plus Alpaca paper and `us_dual_paper_*` tools; no KIS/generic order tools |
| DB paper simulator | `db-paper` | Default read-only/research surface plus internal `paper.paper_*` simulator account, analytics, and journal bridge tools; no KIS/generic order tools |
| Kiwoom mock | `kiwoom` | Default read-only/research surface plus typed `kiwoom_mock_*` variants only (no KIS/generic order tools) |
| Analysis readonly | `analysis_readonly` | Codex/headless read/analysis allowlist only: `get_operating_briefing`, `route_request`, `get_trading_policy`, selected quote/fundamental/analysis tools, `suggest_order_account`, `get_holdings`, `toss_get_positions`, and explicitly labeled analysis persistence. No order/cancel/modify/reconcile/preview/settings/watch/admin/manual-holdings mutation tools are registered. |

### Profile: `hermes-paper-kis`

Set `MCP_PROFILE=hermes-paper-kis` on paper-only deployments (e.g., where `KIS_MOCK_ENABLED=true`).

- `kis_live_place_order`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_get_order_history` are **not registered**.
- The legacy ambiguous `place_order`, `cancel_order`, `modify_order`, `get_order_history` are **not registered**.
- Only `kis_mock_*` typed order tools are registered.
- Shared read-only research and portfolio tools remain available; split-profile-only tools such as Alpaca paper, crypto-only, and Kiwoom are absent.

**Operator validation:** after deploying with `hermes-paper-kis`, check the MCP `/mcp` listing and confirm that none of `kis_live_*` or the legacy ambiguous order tools appear.

### Profile: `analysis_readonly` (ROB-745)

Use `MCP_PROFILE=analysis_readonly` for Codex/headless consumers that need market analysis tools but must not see the operator's full order-capable MCP surface.

Allowed tools:
- `get_operating_briefing`
- `route_request`
- `get_trading_policy`
- `get_market_index`
- `get_quote`
- `analyze_stock_batch`
- `get_support_resistance`
- `get_indicators`
- `screen_stocks`
- `screen_stocks_snapshot`
- `get_top_stocks`
- `get_news`
- `get_fx_rate`
- `suggest_order_account`
- `get_holdings`
- `toss_get_positions`
- `get_intraday_investor_flow`
- `analysis_artifact_save`
- `analysis_artifact_get`
- `forecast_save`
- `session_context_append`
- `session_context_get_recent`

Forbidden by physical non-registration:
- order placement, cancel, modify, history, reconcile, and preview tools
- KIS live/mock order variants
- Kiwoom order variants
- Alpaca/DB paper order surfaces
- Toss place/modify/cancel/history/orderable-cash/reconcile/preview
- manual holdings mutation
- user settings tools
- watch/admin/report-writing surfaces

Persistence tools on this profile require explicit provenance:
- pass `created_by="codex"` for `analysis_artifact_save`
- pass `created_by="codex"` in every `session_context_append` entry
- pass `created_by="codex"` to `forecast_save`

### Codex Config Example

Here is an example Codex config file to connect to the analysis-readonly MCP server with relaxed approval:

```toml
# ~/.codex/config.toml
# Relaxed approval is scoped to the analysis-readonly MCP server only.
[mcp_servers.auto_trader_analysis_readonly]
url = "http://127.0.0.1:8768/mcp"
bearer_token_env_var = "MCP_ANALYSIS_READONLY_AUTH_TOKEN"
default_tools_approval_mode = "auto"

http_headers = { "x-paperclip-agent-id" = "codex-analysis-readonly" }
```

### Typed KIS order tools

The `default` and `hermes-paper-kis` profiles provide explicitly-named KIS
typed variants:

**Mock (KIS official mock / paper):**
- `kis_mock_place_order` — hard-pinned `is_mock=True`; fails closed if KIS mock config missing
- `kis_mock_cancel_order`
- `kis_mock_modify_order`
- `kis_mock_get_order_history`

**Live (real-money):**
- `kis_live_place_order` — hard-pinned `is_mock=False`
- `kis_live_cancel_order`
- `kis_live_modify_order`
- `kis_live_get_order_history`

Each typed tool rejects any `account_mode` value other than its own pinned mode.

### Fail-closed behavior

`kis_mock_*` tools return a structured error (without delegating) when KIS mock config is incomplete:

```json
{
  "success": false,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY",
  "source": "kis",
  "account_mode": "kis_mock"
}
```

With all mock vars missing, the `hermes-paper-kis` profile is effectively read-only KIS — the safe state for a misconfigured paper deployment.
