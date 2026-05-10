# ROB-173 /invest Stock-Detail MVP — Repo Plan & Dependency Map

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement ROB-174 / ROB-175 task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This document is **the parent (ROB-173) plan**: it captures the architecture decision, the dependency map, and a *short* implementation checklist for the two children (ROB-174 backend, ROB-175 frontend). It is intentionally not a full TDD step-by-step like prior K0/K1 plans — the per-step expansion happens in the children's plans before they are executed. ROB-176 is reserved for follow-up polish (analysis card, news lazy-load, freshness indicators) and is mapped here only as scope-defined backlog.

**Linear:** [ROB-173 — parent](https://linear.app/mgh3326/issue/ROB-173) · [ROB-174 — backend](https://linear.app/mgh3326/issue/ROB-174) · [ROB-175 — frontend](https://linear.app/mgh3326/issue/ROB-175) · [ROB-176 — follow-ups](https://linear.app/mgh3326/issue/ROB-176)

**Branch model:**
- Backend implementation lands on `feature/ROB-174-stock-detail-backend` (rebased off the worktree branch already created at `rob-174-stock-detail-backend`).
- Frontend implementation lands on `feature/ROB-175-stock-detail-frontend`, opened **after** ROB-174 ships and the JSON contract is frozen.
- Each child PR targets `main`; `production` is untouched per `CLAUDE.md` worktree rules.

**Goal:** Ship a read-only Toss-style stock-detail page at `/invest/stocks/{market}/{symbol}` that composes existing data sources (holdings, candles, profile/valuation, KR orderbook, recent news, recent AI analysis, screener snapshot, filled order history) into a single above-the-fold view-model plus a small set of lazy sub-endpoints. **Read-only.** No broker mutations, no order placement, no watch/order intent writes, no options.

---

## Architecture Decision: hybrid composed shell + lazy sub-endpoints

**Decision (recommended):** Add **one** above-the-fold composed view-model endpoint and **three** lazy sub-endpoints. Do **not** ship a single mega-endpoint; do **not** force the frontend to fan out 7 requests on page open.

**Endpoints (ROB-174):**

| Verb | Path | Returns | Lazy? |
|------|------|---------|-------|
| GET | `/invest/api/stock-detail/{market}/{symbol}` | `StockDetailResponse` — header + summary blocks | No (above-the-fold) |
| GET | `/invest/api/stock-detail/{market}/{symbol}/candles?period=` | `StockDetailCandlesResponse` | Yes |
| GET | `/invest/api/stock-detail/{market}/{symbol}/news?cursor=&limit=` | `StockDetailNewsResponse` | Yes |
| GET | `/invest/api/stock-detail/{market}/{symbol}/orders?cursor=&limit=` | `StockDetailOrdersResponse` | Yes |

`{market}` ∈ `{"kr","us","crypto"}`. `{symbol}` is the **DB canonical** form (dot-separated for US per `app/core/symbol.py`). The handler normalises any inbound hyphen/slash via `to_db_symbol()` and rejects unknown universes with 404.

### Why hybrid (and not the alternatives)

**Option A — Single mega view-model.** Backend orchestrates everything (header + candles + orderbook + news + orders + analysis) in one request. We rejected this because (a) Naver-Finance valuation HTML scraping and KIS daily candles together are unbounded latency on the page-open critical path; (b) news + order-history pagination requires cursors that don't fit a single response; (c) frontend lazy-rendering and skeletons become harder, not easier.

**Option B — Pure compose, no new aggregator.** The frontend opens 6+ existing endpoints (account-panel for holdings, `/trading/api/v1/trading/ohlcv`, `/trading/api/v1/trading/orderbook`, `/invest/api/feed/news`, plus three new piecewise endpoints for valuation / analysis / filled orders). We rejected this because (a) the existing `/trading/api/v1/...` path is namespaced for the legacy non-SPA trading UI and isn't conventional for `/invest`; (b) we'd duplicate symbol-resolution and not-found handling 6 times on the frontend; (c) it breaks the existing `/invest/api/<feature>` pattern (`/invest/api/home`, `/invest/api/account-panel`, `/invest/api/calendar` are all composed view-models — see `app/routers/invest_api.py`).

**Why hybrid wins:**
- Matches the existing `/invest/api/<feature>` view-model convention (ROB-127, ROB-138, ROB-147, ROB-170 all followed it).
- The above-the-fold response is pre-aggregated → one round trip, one skeleton, one 404 surface.
- Heavy/slow data (candles full-history, news pagination, order-history pagination) stays out of the critical path.
- Sub-endpoints are independently cacheable and reusable by future panels.
- Read-only safety: every new path is a `GET` and the orchestrator service must live under `app/services/invest_view_model/` so the existing `tests/test_invest_view_model_safety.py` regex denies broker/order mutation imports automatically (this is the same gate ROB-147 added; we extend its target set).

### Above-the-fold response shape (frozen here so ROB-174 can implement and ROB-175 can mock)

```jsonc
// GET /invest/api/stock-detail/{market}/{symbol}
{
  "symbol": "005930",                           // DB canonical
  "market": "kr",                               // kr | us | crypto
  "displayName": "삼성전자",
  "exchange": "KOSPI",                          // KOSPI|KOSDAQ|NASD|NYSE|AMEX|KRW (Upbit quote)
  "instrumentType": "equity_kr",                // equity_kr|equity_us|crypto
  "currency": "KRW",                            // KRW|USD
  "assetType": "equity",                        // equity|etf|crypto|fund|other
  "assetCategory": "kr_stock",                  // kr_stock|us_stock|crypto

  "quote": {
    "price": 71200,
    "previousClose": 70900,
    "changeAmount": 300,
    "changeRate": 0.0042,
    "asOf": "2026-05-10T15:30:00+09:00",
    "priceState": "live"                        // live|stale|missing
  },

  "screenerSnapshot": {                         // null if no snapshot row (e.g. crypto today)
    "snapshotDate": "2026-05-09",
    "consecutiveUpDays": 3,
    "weekChangeRate": 0.018,
    "dailyVolume": 18234000,
    "closesWindow": [70300, 70500, 70700, 70900, 71200],
    "source": "kis",
    "freshness": "fresh"                        // fresh|stale|missing — derived from snapshot_date vs today
  },

  "valuation": {                                // null if scrape fails or unsupported (crypto)
    "per": 11.2,
    "pbr": 1.4,
    "roe": 0.092,
    "dividendYield": 0.018,
    "high52w": 88800,
    "low52w": 64500,
    "marketCap": null,                          // best-effort, may be null
    "source": "naver_finance",                  // naver_finance|yfinance
    "asOf": "2026-05-10T07:00:00+09:00",
    "freshness": "stale"                        // ok|stale|unsupported|error
  },

  "holding": {                                  // null if user does not hold this symbol
    "totalQuantity": 50,
    "averageCost": 68000,
    "costBasis": 3400000,
    "valueNative": 3560000,
    "valueKrw": 3560000,
    "pnlKrw": 160000,
    "pnlRate": 0.047,
    "includedSources": ["kis_kr", "manual_toss"],
    "priceState": "live"
  },

  "latestAnalysis": {                           // null if no rows in stock_analysis_results
    "id": 12345,
    "modelName": "gemini-2.5-pro",
    "decision": "hold",                         // buy|hold|sell
    "confidence": 62,
    "appropriateBuyRange": [66000, 69000],      // null tuple if missing
    "appropriateSellRange": [78000, 82000],
    "reasonsTop3": ["...", "...", "..."],
    "createdAt": "2026-05-09T22:14:00+09:00"
  },

  "orderbookSupport": {                         // declares whether to render the orderbook block
    "supported": true,
    "reason": null                              // null|"us_unsupported"|"crypto_lazy_only"
  },
  "orderbook": {                                // present only if orderbookSupport.supported && market=="kr"
    "asOf": "2026-05-10T15:30:00+09:00",
    "asks": [{ "price": 71200, "quantity": 1430 }, ...],   // 10 levels
    "bids": [{ "price": 71100, "quantity": 2210 }, ...]    // 10 levels
  },

  "capabilities": {                             // explicit, machine-readable feature flags
    "candles": { "supported": true, "intradaySupported": true },
    "orderbook": { "supported": true, "reason": null },
    "news": { "supported": true },
    "orders": { "supported": true },
    "liveStreaming": { "supported": false, "reason": "out_of_mvp_scope" },
    "execution": { "supported": false, "reason": "read_only_mvp" },
    "options": { "supported": false, "reason": "out_of_mvp_scope" }
  },

  "meta": {
    "computedAt": "2026-05-10T15:30:01+09:00",
    "warnings": []                              // ["valuation_unavailable", "orderbook_kr_only", ...]
  }
}
```

### US/crypto coverage matrix (frozen contract for the orchestrator)

| Block | KR | US | Crypto | Source / fallback |
|---|---|---|---|---|
| header (display name, exchange, asset type) | ✓ | ✓ | ✓ | universe tables (`kr_symbol_universe`, `us_symbol_universe`, `upbit_symbol_universe`) |
| quote | ✓ | ✓ | ✓ | KIS (KR), Yahoo (US), Upbit (crypto) — go through `InvestQuoteService.fetch_kr_prices/fetch_us_prices` for stocks; reuse Upbit reader for crypto |
| screenerSnapshot | ✓ | ✓ | `null` | `InvestScreenerSnapshotsRepository.get_fresh([symbol])` — no crypto snapshots today; render `null` and warn `screener_snapshot_unavailable` |
| valuation | ✓ | ✓ | `null` | `_fetch_valuation_naver` (KR) / `_fetch_valuation_yfinance` (US) — extracted into a service from the MCP tool; freshness `unsupported` for crypto |
| holding | ✓ | ✓ | ✓ | `groupedHoldings` lookup against `AccountPanelService.build_account_panel` cache (or call directly) |
| latestAnalysis | ✓ | ✓ | ✓ | `StockAnalysisService.get_latest_analysis_by_symbol(symbol)` (already exists) |
| orderbook | ✓ | ✗ | deferred | KIS only in MVP. Crypto orderbook service exists (`upbit_orderbook.fetch_orderbook`) but is **not** rendered above-the-fold; surfaced via the lazy candles/orders panel only if ROB-176 picks it up. US is permanently unsupported — no NBBO provider integrated. |
| candles | ✓ daily+intraday | ✓ daily+intraday | ✓ daily/weekly/monthly only | lazy endpoint, see below |
| news | ✓ | ✓ | ✓ | lazy endpoint, see below |
| orders (filled history) | ✓ | ✓ | ✓ | lazy endpoint, see below |

---

## Dependency Map (existing things ROB-174 reuses)

This section is the canonical inventory; subagents implementing the children should treat it as ground truth and verify line ranges before quoting them.

### Routes & shells (verified during planning)

- Router registration: `app/main.py:186-188` includes `invest_api`, `invest_app_spa`, `invest_web_spa` in that order. The new `/invest/api/stock-detail/...` paths slot into `invest_api` and are matched **before** the SPA catch-all in `invest_web_spa.py:54-59`.
- Frontend SPA shell: `frontend/invest/index.html` + `frontend/invest/src/main.tsx` + `frontend/invest/src/routes.tsx:28-54` (React Router; canonical `/invest/<tab>` paths). Add `/stocks/:market/:symbol` here.
- API client convention: every existing tab has `frontend/invest/src/api/<feature>.ts` (e.g. `accountPanel.ts`, `screener.ts`, `feedNews.ts`). ROB-175 adds `frontend/invest/src/api/stockDetail.ts`.
- Confirmed absent: there is **no** existing `/invest/stocks/...`, `/invest/detail/...`, or `/invest/api/stock-detail/...` route. We're not stepping on prior work.

### Holdings & account panel (reused; not modified)

- Schema: `app/schemas/invest_home.py:47-67` (`Holding`), `:83-102` (`GroupedHolding`), `:133-140` (`InvestHomeResponse`). `app/schemas/invest_account_panel.py:32-39` (`AccountPanelResponse`).
- Service: `app/services/invest_view_model/account_panel.py` (`build_account_panel`) and the home reader composition `app/services/invest_home_readers.py` (`KISHomeReader`, `UpbitHomeReader`, `ManualHomeReader`).
- The orchestrator MUST NOT re-invent holdings; it looks up the symbol from the cached `groupedHoldings` and copies (`totalQuantity`, `averageCost`, `costBasis`, `valueNative`, `valueKrw`, `pnlKrw`, `pnlRate`, `includedSources`, `priceState`) into `StockDetailResponse.holding`. Returns `null` if not held.

### Candles / OHLCV (reused via existing services, exposed via new lazy endpoint)

- KR daily: `app/services/brokers/kis/domestic_market_data.py:415` (`inquire_daily_itemchartprice`). Cache: `app/services/kis_ohlcv_cache.py`.
- KR intraday (1m / 5m / 10m / 15m / 30m / 60m): `app/services/brokers/kis/domestic_market_data.py:531` (`inquire_minute_chart`). Persisted reads: `app/services/kr_intraday/_repository.py::read_kr_intraday_candles`.
- US daily / intraday (1m / 5m / 15m / 30m / 1h): `app/services/brokers/yahoo/client.py::fetch_ohlcv`. Persisted reads for intraday: `app/services/us_intraday_candles_read_service.py` (tables `us_candles_5m|15m|30m`).
- Crypto daily / weekly / monthly: `app/services/brokers/upbit/client.py::fetch_ohlcv`. **No minute-level crypto candles.**

### Profile / valuation / fundamentals

- KR HTML scrape: `app/services/naver_finance/valuation.py:25` (`_parse_valuation_from_soups` → `per`, `pbr`, `roe`, `dividend_yield`, `high_52w`, `low_52w`).
- US: `app/mcp_server/tooling/fundamentals/_valuation.py:37` (`handle_get_valuation`) wraps `_fetch_valuation_yfinance` (yfinance).
- **No REST endpoint today** — both currently surface only via the MCP tool. ROB-174 must extract `_fetch_valuation_naver` / `_fetch_valuation_yfinance` into a plain async service module under `app/services/invest_view_model/valuation_service.py` (or reuse from MCP via a thin wrapper) so the view-model orchestrator can call them without depending on MCP runtime. Treat `naver_finance` HTML scraping as best-effort: 429 / 5xx / parse error → `valuation: null` and warn `valuation_unavailable`.

### Feed / news per symbol (composed; existing service must accept a new symbol filter)

- Existing endpoint: `/invest/api/feed/news` at `app/routers/invest_api.py:151`.
- Service: `app/services/invest_view_model/feed_news_service.py:333` (`build_feed_news`).
- Today: `tab` (top|latest|hot|holdings|watchlist|kr|us|crypto) and `cursor`/`limit`/`include_quotes`. No `symbol` filter.
- For ROB-174 we **extend** `build_feed_news` to accept `symbol_filter: tuple[str, NewsMarket] | None` that joins `NewsArticleRelatedSymbol` on `(symbol, market)` rather than the current scope/tab logic. The new lazy endpoint `/invest/api/stock-detail/{market}/{symbol}/news` is a thin wrapper that always passes `symbol_filter=(symbol, market)` and forwards `cursor` / `limit`.
- DTOs unchanged: `app/schemas/invest_feed_news.py:23` (`NewsRelatedSymbol`), `:39` (`FeedNewsItem`). Frontend reuses the existing `NewsCard` component.

### Orderbook (KR-only above-the-fold; explicit unsupported flag elsewhere)

- KR endpoint already exists: `/trading/api/v1/trading/orderbook` at `app/routers/trading.py:578`. The orchestrator does **not** call this HTTP endpoint — it calls the underlying `app/services/market_data/service.py:422` (`get_orderbook`) directly so it stays in-process.
- Crypto: `app/services/upbit_orderbook.py:57` (`fetch_orderbook`). Capable, but not surfaced above-the-fold in MVP; document as "deferred".
- US: **no integration** — `capabilities.orderbook = { supported: false, reason: "us_unsupported" }`.

### Order history (filled, per symbol)

- Existing service: `app/services/n8n_filled_orders_service.py:30-150` (`fetch_filled_orders(days, markets)`). Returns normalised `{symbol, price, quantity, side, filled_at, account, order_id, ...}` rows across KR (KIS), US (KIS), crypto (Upbit).
- Existing endpoint is n8n-only (`app/routers/n8n.py:417`) and **not** exposed under `/invest/api`.
- For ROB-174: add a thin per-symbol filter wrapper in `app/services/invest_view_model/stock_detail_orders_service.py` that calls `fetch_filled_orders(days=N, markets=[market])` and filters results to the requested symbol (after symbol normalisation). Do not move the n8n endpoint. Surface only via the new lazy `/invest/api/stock-detail/{market}/{symbol}/orders`.

### Latest AI analysis per symbol

- Lookup already exists: `StockAnalysisService.get_latest_analysis_by_symbol(symbol)` at `app/services/stock_info_service.py:209-220`.
- Model: `app/models/analysis.py:51-106` (`StockAnalysisResult`).
- The orchestrator calls this directly and maps the row into `StockDetailResponse.latestAnalysis` (mapping omits `prompt`, `detailed_text` — those stay server-side; we expose `decision`, `confidence`, `appropriateBuyRange`, `appropriateSellRange`, `reasonsTop3` (first 3 of `reasons`), `createdAt`, `id`, `modelName`).

### Screener snapshot per symbol

- Repository: `app/services/invest_screener_snapshots/repository.py:63-80` (`InvestScreenerSnapshotsRepository.get_fresh(market, symbols, on_or_after)` accepts an iterable — pass `[symbol]` for the one-shot lookup).
- Model: `app/models/invest_screener_snapshot.py:22-78`. Map `consecutive_up_days`, `week_change_rate`, `daily_volume`, `closes_window`, `source`, `snapshot_date` into the response.
- Freshness rule: if `snapshot_date == today_utc_local` → `fresh`; if within 7 days → `stale`; else → `missing`. Crypto market currently returns `null` (no snapshots).

### Universe / metadata lookup

- KR: `app/services/kr_symbol_universe_service.py:420-429` (`search_kr_symbols`) → `{symbol, name, instrument_type, exchange, is_active}`.
- US: `app/services/us_symbol_universe_service.py:362-370` → `{symbol, name (kr/en), instrument_type, exchange, is_active}`.
- Upbit: `app/services/upbit_symbol_universe_service.py:481-490` → `{symbol (e.g. BTC-KRW), name, instrument_type=crypto, exchange=quote_currency, is_active, market_warning}`.
- Symbol normalisation utility: `app/core/symbol.py::to_db_symbol` / `to_kis_symbol` / `to_yahoo_symbol`. The router accepts any of `BRK.B`, `BRK-B`, `BRK/B`, normalises to DB form, then resolves universe row.

### Read-only safety gate (already shipping; we extend its scope)

- `tests/test_invest_view_model_safety.py` parses every module imported under `app/services/invest_view_model/` and rejects any import of `app/services/kis_trading_service`, `app/services/upbit_trading_service`, `app/services/order_*` mutation modules, `watch_order_intent_ledger`, etc.
- ROB-174 puts the new orchestrator under `app/services/invest_view_model/stock_detail_*.py` so the gate auto-extends. No new safety test is required, just a one-line list extension if a new module path is introduced.

---

## Unsupported capabilities (explicit MVP guardrails)

These are surfaced via `capabilities.*.supported: false` in the response and the frontend renders an explanatory placeholder instead of an empty state.

| Capability | Status | Reason | Where to communicate |
|---|---|---|---|
| **US orderbook** | unsupported, indefinite | No NBBO / L2 data provider integrated. KIS overseas API does not return depth at parity with KR `inquire_orderbook`. Upgrading would require a paid feed. | `capabilities.orderbook.supported = false`, `reason = "us_unsupported"`. Frontend hides the orderbook block for `market === "us"`. |
| **Crypto orderbook (above-the-fold)** | deferred to ROB-176 | Service exists (`upbit_orderbook.fetch_orderbook`) but render-density and refresh cadence need design before we surface it. | `capabilities.orderbook.supported = false`, `reason = "crypto_deferred"`. |
| **Live chart parity (websocket-streamed candles)** | out of MVP | We have no per-symbol WebSocket consumer that the SPA can reuse safely; KIS WebSocket monitor is out-of-process. | `capabilities.liveStreaming.supported = false`, `reason = "out_of_mvp_scope"`. Lazy candles endpoint returns historical candles only (no SSE / WS). |
| **Live order execution** | out of MVP, read-only enforced | Hard guardrail from task body. No "Buy/Sell" buttons on the detail page. | No mutation endpoints added. The page links out to existing `/trading/decisions` flows for any action. `capabilities.execution.supported = false`. |
| **Options execution** | out of MVP | Hard guardrail; we don't have an options instrument type in `stock_info` or universes. | `capabilities.options.supported = false`. |
| **Crypto intraday candles** | upstream limitation | Upbit `candles` API exposes `day` / `week` / `month` only via the existing client. | The lazy candles endpoint returns 400 with `unsupported_period` if `market=crypto` and `period=` is intraday. |
| **KIS intraday for the very first 30 minutes after market open** | partial | Known KIS issue (see `CLAUDE.md` § 문제 해결): `time_unit` parameter behaves unreliably. | Endpoint still serves what KIS returns; client surfaces `priceState=stale` when `asOf` is older than 5 minutes during market hours. Already handled by existing readers. |
| **Watchlist write ("watch this symbol")** | not in scope | The existing `symbol_settings` table is trading-config, not a generic watchlist. ROB-173 keeps everything read-only. | No `POST /watchlist` added. The detail page renders held/not-held only, sourced from holdings. |

---

## ROB-174 — Backend implementation checklist

Branch: `feature/ROB-174-stock-detail-backend` (rebased from this worktree's branch). Each task is one short PR-able commit; the executing agent expands them into TDD sub-steps.

### Backend Task B0 — Branch + plan link

- Files: none (git only).
- [ ] Confirm worktree branch matches `feature/ROB-174-stock-detail-backend` (rename from `rob-174-stock-detail-backend` if needed, or push under both refs).
- [ ] Add `Linear: ROB-174` to the PR body draft (no commit yet).

### Backend Task B1 — Stock-detail Pydantic schema

- Create: `app/schemas/invest_stock_detail.py`
- Create: `tests/test_invest_stock_detail_schemas.py`
- [ ] Define `StockDetailResponse`, `StockDetailQuote`, `StockDetailScreenerSnapshot`, `StockDetailValuation`, `StockDetailHolding`, `StockDetailLatestAnalysis`, `StockDetailOrderbook`, `StockDetailOrderbookLevel`, `StockDetailCapabilities`, `StockDetailCandlesResponse`, `StockDetailNewsResponse` (alias of existing `FeedNewsResponse`), `StockDetailOrdersResponse`, `StockDetailMeta`. All `*Literal` enums share the existing `MarketLiteral` / `CurrencyLiteral` / `PriceStateLiteral` / `AssetTypeLiteral` / `AssetCategoryLiteral` from `app/schemas/invest_home.py` — import, do not redefine.
- [ ] Tests must cover: market literal validation (rejects unknown), `capabilities.execution.supported` is hard-coded `False`, `holding` is optional, `valuation` is optional, `orderbook` is required-iff `orderbookSupport.supported`.

Smoke: `uv run pytest tests/test_invest_stock_detail_schemas.py -v`.

### Backend Task B2 — Symbol resolver utility

- Create: `app/services/invest_view_model/stock_detail_symbol_resolver.py`
- Create: `tests/test_stock_detail_symbol_resolver.py`
- [ ] One async function `resolve_symbol(market: MarketLiteral, raw_symbol: str, db) -> ResolvedSymbol` returning `{symbol_db, display_name, exchange, instrument_type, asset_type, asset_category, currency}` or raising `SymbolNotFound`.
- [ ] Implementation: normalise via `app/core/symbol.to_db_symbol`, query the appropriate universe table (`kr_symbol_universe` / `us_symbol_universe` / `upbit_symbol_universe`), 404-equivalent on miss.
- [ ] Tests: KR happy path, US with hyphen and slash inputs both resolving to dot, crypto via `BTC-KRW`, unknown symbol raises.

Smoke: `uv run pytest tests/test_stock_detail_symbol_resolver.py -v`.

### Backend Task B3 — Valuation service extraction

- Create: `app/services/invest_view_model/stock_detail_valuation_service.py`
- Modify (re-export only): `app/mcp_server/tooling/fundamentals/_valuation.py`
- Create: `tests/test_stock_detail_valuation_service.py`
- [ ] Extract `_fetch_valuation_naver` and `_fetch_valuation_yfinance` into the new service module so they can be imported without booting the MCP runtime. The MCP tool keeps its handler but imports the new helpers.
- [ ] One async function `fetch_valuation(market, symbol_db) -> StockDetailValuation | None`. Errors → `None` + structured warning, never raise.
- [ ] Tests: KR happy path with stubbed soup fixtures, US happy path with stubbed yfinance, crypto returns `None`, network error returns `None` and emits warning.

Smoke: `uv run pytest tests/test_stock_detail_valuation_service.py -v`.

### Backend Task B4 — Stock-detail orchestrator (above-the-fold view-model)

- Create: `app/services/invest_view_model/stock_detail_service.py`
- Create: `tests/test_stock_detail_service.py`
- Modify: list in `tests/test_invest_view_model_safety.py` (add new module path).
- [ ] One async function `build_stock_detail(user_id, market, symbol_db, db) -> StockDetailResponse` that:
  1. Calls `resolve_symbol`.
  2. Concurrently (via `asyncio.gather`, with per-task timeouts and `return_exceptions=True`):
     - quote (per market: KIS for KR, Yahoo for US, Upbit for crypto)
     - screener snapshot (`InvestScreenerSnapshotsRepository.get_fresh`)
     - valuation (`fetch_valuation`)
     - holding (lookup against cached `AccountPanelService.build_account_panel` for `user_id`; if not held → `None`)
     - latest analysis (`StockAnalysisService.get_latest_analysis_by_symbol`)
     - orderbook (KR only: `market_data_service.get_orderbook(symbol_db, "kr")`)
  3. Maps each subresult into the response, recording per-block warnings on failure (never raising — single-block failures degrade to `null`).
  4. Sets `capabilities` from the constants table above.
- [ ] Hard rule: orchestrator imports nothing from broker mutation modules; the safety test enforces this. Direct callers: only the router.
- [ ] Tests: held vs not-held, KR happy path with all blocks, US with valuation present and orderbook absent, crypto with screener+valuation+orderbook absent, valuation timeout produces warning + `null` block, snapshot stale produces `freshness=stale`.

Smoke: `uv run pytest tests/test_stock_detail_service.py tests/test_invest_view_model_safety.py -v`.

### Backend Task B5 — Stock-detail above-the-fold router

- Modify: `app/routers/invest_api.py` (add the `GET /stock-detail/{market}/{symbol}` handler).
- Create: `tests/test_invest_api_stock_detail.py`
- [ ] Handler: `async def get_stock_detail(market: MarketLiteral, symbol: str, user, db) -> StockDetailResponse`. 404 on `SymbolNotFound`. Auth via the same `get_authenticated_user` dependency used by `/invest/api/home`.
- [ ] Tests: 200 happy path, 404 on unknown, 401 anonymous, response shape matches schema, US hyphen/slash input both return same canonical `symbol`.

Smoke: `uv run pytest tests/test_invest_api_stock_detail.py -v`.

### Backend Task B6 — Lazy candles sub-endpoint

- Create: `app/services/invest_view_model/stock_detail_candles_service.py`
- Modify: `app/routers/invest_api.py` (add `GET /stock-detail/{market}/{symbol}/candles`).
- Create: `tests/test_stock_detail_candles.py`
- [ ] Service maps `(market, period)` to the existing client: KR daily / intraday → KIS, US daily / intraday → Yahoo, crypto daily/weekly/month → Upbit. Reject unsupported (market, period) combos with `unsupported_period`.
- [ ] Response: `{ candles: [{ts, open, high, low, close, volume}], period, source, capabilities: { intradaySupported } }`. Reuse existing OHLCV row shape from `app/schemas/trading.py::OHLCVData` if structurally identical, otherwise define local row to keep `/invest/api/...` independent of the legacy `/trading/api/...` schema.
- [ ] Tests: KR daily 200/intraday 200, US daily 200, crypto day 200, crypto 5m → 400 `unsupported_period`, US orderbook-style 400 not applicable here.

Smoke: `uv run pytest tests/test_stock_detail_candles.py -v`.

### Backend Task B7 — Lazy news sub-endpoint (extend `feed_news_service`)

- Modify: `app/services/invest_view_model/feed_news_service.py` (`build_feed_news`: add `symbol_filter` kwarg; when set, join `NewsArticleRelatedSymbol` on `(symbol, market)` and bypass tab logic).
- Modify: `app/routers/invest_api.py` (add `GET /stock-detail/{market}/{symbol}/news`).
- Create: `tests/test_stock_detail_news.py`
- [ ] The new endpoint forwards `cursor`, `limit`; it always sets `symbol_filter=(symbol_db, market)` and forces `include_quotes=False` for MVP.
- [ ] Tests: returns articles related to the symbol, paginates via cursor, returns empty list with stable schema for symbol-with-no-news, existing `/invest/api/feed/news` unchanged (regression test against the existing test file).

Smoke: `uv run pytest tests/test_stock_detail_news.py tests/test_invest_feed_news_service.py -v`.

### Backend Task B8 — Lazy filled-orders sub-endpoint

- Create: `app/services/invest_view_model/stock_detail_orders_service.py`
- Modify: `app/routers/invest_api.py` (add `GET /stock-detail/{market}/{symbol}/orders`).
- Create: `tests/test_stock_detail_orders.py`
- [ ] Service wraps `n8n_filled_orders_service.fetch_filled_orders(days=90, markets=[market])`, then filters to the requested symbol after normalisation. Cursor is offset-based for MVP (simpler than re-keying upstream).
- [ ] Tests: KR fills filtered correctly (KIS rows), US fills filtered (KIS overseas rows with hyphen-input → dot-canonical), crypto fills filtered (Upbit `KRW-BTC` → `BTC` mapping), empty list shape stable, days clamp at 365.

Smoke: `uv run pytest tests/test_stock_detail_orders.py -v`.

### Backend Task B9 — Read-only safety + capability regression tests

- Modify: `tests/test_invest_view_model_safety.py` (verify the new module paths are scanned).
- Create: `tests/test_stock_detail_capability_contract.py`
- [ ] Capability contract test asserts that for any `(market, symbol)` resolvable in fixtures, `capabilities.execution.supported` is always `False`, `capabilities.options.supported` is always `False`, `capabilities.orderbook.supported` is `True` only when `market == "kr"`. This test is non-negotiable — it's the read-only guardrail in CI.

Smoke: `uv run pytest tests/test_stock_detail_capability_contract.py tests/test_invest_view_model_safety.py -v`.

### Backend Task B10 — Manual smoke + PR

- [ ] `make lint` (Ruff + ty) clean.
- [ ] `make test` clean (or at minimum: `uv run pytest tests/ -k "stock_detail or invest_view_model_safety" -v`).
- [ ] Boot dev server and curl the four endpoints against a real held symbol and an unheld symbol:
  ```bash
  uv run uvicorn app.main:app --reload --port 8000 &
  curl -s -H "Cookie: <session>" 'http://localhost:8000/invest/api/stock-detail/kr/005930' | jq '.symbol,.holding,.capabilities'
  curl -s -H "Cookie: <session>" 'http://localhost:8000/invest/api/stock-detail/us/AAPL/candles?period=1d' | jq '.candles | length'
  curl -s -H "Cookie: <session>" 'http://localhost:8000/invest/api/stock-detail/kr/005930/news?limit=5' | jq '.items | length'
  curl -s -H "Cookie: <session>" 'http://localhost:8000/invest/api/stock-detail/kr/005930/orders?limit=5' | jq '.items | length'
  ```
- [ ] Open PR `feat(ROB-174): /invest/api/stock-detail composed view-model + lazy sub-endpoints`. Body links Linear ROB-173 (parent) and ROB-174 (this issue). Reviewer focus: capability contract, read-only safety test extension.

---

## ROB-175 — Frontend implementation checklist

Branch: `feature/ROB-175-stock-detail-frontend` (off `main` after ROB-174 ships). Frontend can mock the JSON contract while ROB-174 is in review — the schema in this doc is the source of truth.

### Frontend Task F1 — API client + types

- Create: `frontend/invest/src/api/stockDetail.ts`
- Create: `frontend/invest/src/types/stockDetail.ts`
- Create: `frontend/invest/src/api/__tests__/stockDetail.test.ts`
- [ ] Functions: `fetchStockDetail(market, symbol)`, `fetchStockDetailCandles(market, symbol, period)`, `fetchStockDetailNews(market, symbol, cursor?)`, `fetchStockDetailOrders(market, symbol, cursor?)`. All return typed promises. 404 → typed `StockDetailNotFoundError`.
- [ ] Types mirror the JSON contract above. Reuse `Market`, `Currency`, `PriceState`, `AssetType`, `AssetCategory` from `frontend/invest/src/types/invest.ts`.

Smoke: `cd frontend/invest && pnpm test stockDetail`.

### Frontend Task F2 — Routing + page shell

- Modify: `frontend/invest/src/routes.tsx` (add `/stocks/:market/:symbol` → lazy `StockDetailPage`).
- Create: `frontend/invest/src/pages/StockDetailPage.tsx` (responsive — desktop + mobile delegated to subcomponents under `frontend/invest/src/pages/desktop/DesktopStockDetailPage.tsx` and `.../mobile/MobileStockDetailPage.tsx`).
- [ ] Page fetches above-the-fold on mount. Skeleton on pending. 404 → "종목을 찾을 수 없습니다" empty state with link back to `/`.
- [ ] Lazy panels open candles / news / orders behind tabs (`Tabs` component reused from existing UI library).

Smoke: `cd frontend/invest && pnpm test StockDetailPage`.

### Frontend Task F3 — Header + holding card

- Create: `frontend/invest/src/components/stock-detail/StockDetailHeader.tsx`
- Create: `frontend/invest/src/components/stock-detail/StockDetailHoldingCard.tsx`
- [ ] Header shows display name, symbol, exchange chip, price + change rate (red/blue per existing convention in `HoldingsTable.tsx`).
- [ ] Holding card renders only when `holding` is non-null. Shows quantity, avg cost, value (native+KRW), pnl. "보유 안함" empty state otherwise.

### Frontend Task F4 — Screener snapshot + valuation cards

- Create: `frontend/invest/src/components/stock-detail/StockDetailSnapshotCard.tsx`
- Create: `frontend/invest/src/components/stock-detail/StockDetailValuationCard.tsx`
- [ ] Snapshot card: streak days, week change, daily volume, mini-sparkline from `closesWindow`.
- [ ] Valuation card: PER, PBR, ROE, dividend yield, 52w high/low. Renders "지표 정보 없음" when `valuation === null` (crypto / scrape failure).

### Frontend Task F5 — KR orderbook block (gated by capability)

- Create: `frontend/invest/src/components/stock-detail/StockDetailOrderbook.tsx`
- [ ] Render only when `capabilities.orderbook.supported && orderbook != null`. 10 ask + 10 bid levels, KR-specific styling (red/blue). For US/crypto, render a small explainer "현재 미지원 (US)" / "준비중 (crypto)".

### Frontend Task F6 — Latest AI analysis card

- Create: `frontend/invest/src/components/stock-detail/StockDetailAnalysisCard.tsx`
- [ ] Decision pill (buy/hold/sell color), confidence%, top-3 reasons, appropriate buy/sell ranges. "최근 분석 없음" empty state when `latestAnalysis === null`. **No CTA to run a new analysis** — read-only.

### Frontend Task F7 — Lazy panels (candles / news / orders)

- Create: `frontend/invest/src/components/stock-detail/StockDetailCandlesPanel.tsx`
- Create: `frontend/invest/src/components/stock-detail/StockDetailNewsPanel.tsx` (reuses existing `NewsCard`)
- Create: `frontend/invest/src/components/stock-detail/StockDetailOrdersPanel.tsx`
- [ ] Each panel fetches lazily (on tab switch / on intersection observer for mobile). Pagination via cursor where applicable.
- [ ] Candles panel respects `capabilities.candles.intradaySupported` — disable the 1m/5m toggle for crypto.

### Frontend Task F8 — Routing entry points + smoke

- Modify: `frontend/invest/src/components/home/HoldingsTable.tsx` — clicking a row navigates to `/stocks/{market}/{symbol}`.
- Modify: `frontend/invest/src/components/discover/RelatedSymbolsList.tsx` — clicking a related symbol pill navigates the same way.
- Modify: `frontend/invest/src/components/news/NewsCard.tsx` (or wherever symbol pills render in news) — same nav target.
- [ ] Manual smoke in browser: open `/invest/`, click a held symbol, verify above-the-fold renders < 2s and lazy panels load on tab switch. Confirm a US held symbol hides the orderbook block. Confirm a crypto symbol hides intraday candle toggles.

Smoke: `cd frontend/invest && pnpm build && pnpm test`.

---

## ROB-176 — Out-of-MVP follow-ups (recorded, not planned step-by-step here)

ROB-176 is for after ROB-174 + ROB-175 ship. Backlog (each one PR-sized):
- Crypto orderbook above-the-fold (use existing `upbit_orderbook.fetch_orderbook`).
- Stock-detail freshness banners (`screener_snapshot_unavailable`, `valuation_unavailable`, `kis_intraday_unreliable`) wired to a top-of-page warning bar.
- Live quote refresh (poll `/invest/api/stock-detail/.../quote-only` every N seconds; new sub-endpoint).
- Per-symbol research-reports widget (ROB-140 `research_reports` table → new lazy panel).
- Per-symbol market-events widget (ROB-128 `market_events` filtered by symbol → new lazy panel).
- TradingView-style candlestick component (currently a sparkline).

These are explicitly **not** in the scope of ROB-173 / ROB-174 / ROB-175 and should be kept out of those PRs.

---

## Smoke / verification one-liners

For reviewers and the agent that picks this up:

```bash
# Backend, after ROB-174 implementation:
uv run pytest tests/ -k "stock_detail" -v
uv run pytest tests/test_invest_view_model_safety.py -v
make lint

# Schema regression — ensure the read-only contract holds:
uv run pytest tests/test_stock_detail_capability_contract.py -v

# Local manual:
uv run uvicorn app.main:app --reload --port 8000
# (then curl examples in Backend Task B10)

# Frontend, after ROB-175 implementation:
cd frontend/invest && pnpm test && pnpm build
```

---

## Open questions / followups (record before implementation)

1. **Should the lazy news endpoint re-rank by symbol-relevance or keep insertion order?** MVP: insertion order (most recent first), matches `/invest/api/feed/news`. ROB-176 can add per-symbol scoring.
2. **Order history time window default.** Proposal: 90 days, max 365. Confirm with stakeholder before B8 lands.
3. **Display name source of truth.** When manual `display_name` differs from universe `name`, which wins? Proposal: prefer universe name for header, expose `holding.displayName` separately for the holding card. Confirm with ROB-175 reviewer.
4. **Intraday candle period names.** KR uses `1m/5m/...` strings already; US uses the same. Confirm these are exposed verbatim via the lazy candles endpoint, no aliasing.

---

## Self-review

- **Spec coverage:** Each of the four task-body items (route inspection, single-vs-compose decision, ROB-174/175 checklists, unsupported-capabilities flagging) maps to a section above (Dependency Map, Architecture Decision, Backend Task B1–B10, Frontend Task F1–F8, Unsupported Capabilities table).
- **Placeholder scan:** No "TBD" / "implement later" / "similar to Task N" left. Every backend/frontend task names exact files, exact tests, and a smoke command.
- **Type consistency:** `StockDetailResponse.*` field names match between the JSON contract block, the schema task (B1), the orchestrator task (B4), and the frontend types task (F1). `MarketLiteral` / `CurrencyLiteral` / `PriceStateLiteral` / `AssetTypeLiteral` / `AssetCategoryLiteral` are imported from `app/schemas/invest_home.py` and `frontend/invest/src/types/invest.ts` rather than re-declared.
- **Read-only guardrails:** Capability contract test (B9) and the existing `tests/test_invest_view_model_safety.py` extension are explicit gates; both are listed as PR-blocking.
- **Worktree rules:** Backend lives on `feature/ROB-174-stock-detail-backend` (this worktree's branch), frontend on a separate feature branch off `main`. Neither touches `production` or the legacy paths called out in `CLAUDE.md`.
