# ROB-1489 — Strategy data plan: what the strategies need, what AT already has, and how to load the rest

Status: decision draft for the operator (hk task 2713). Documents only — no code, no migration,
no scheduler, no deploy. Base: `origin/main` at `6deb46f42` (2026-11-02).

Evidence read 2026-11-02/03. Production reads were aggregate, bounded and read-only through the
active API container; every query is in Appendix A. The goal is the operator's: **before building
strategies, decide which data they need and how it will be loaded — reuse what exists, then list
what is missing.**

Inputs reused, not re-derived:

- `docs/architecture/rob-1488-feature-inventory.md` (branch `ROB-1488-inventory`, PR #2284) —
  tool/table inventory, measured row counts, and the "used but silently not loading" list (§5).
- `docs/research/data-plane-contract-v1.md`, `...-collector-inventory-2026-11-01.md`,
  `...-broker-sources-inventory-2026-11-01.md` (ROB-1443/ROB-1448) — the existing PIT file
  contract, collector verdicts, and broker bar sources.
- `research/stock_selection/README.md` (ROB-1474) and `research/experiments/reports/rob-1474-*.md`
  — what the daily cross-sectional harness actually reads.
- `scripts/mock_data_refresh.py` + `docs/runbooks/rob1474-mock-data-refresh.md` — the paper-runner
  file producer and its observation-date membership rule.
- dots results: ROB-1444, ROB-1449 (via 1444/1490), ROB-1452, ROB-1490 (landed 2026-11-03 07:26
  KST during this review), ROB-1474/1475, ROB-1447. Fold-in status in Appendix C.

---

## 0. Decisions in one screen

1. **The research input already has a better home than the serving tables.** The PIT data plane
   (`research/data_plane`, contract v1) is the only store in this repo that records `available_at`,
   write-once partitions, source, content hash and fallbacks. It is **implemented and running since
   2026-11-02** (Dagu DAGs `data-plane-{kr,us,crypto}` registered on the scheduler, worker = the NCP
   host; operator approval hk:doc decision/2026-11-03/data-plane-dagu-registration). The first build
   step is to let it accumulate and watch its health, not to write another collector.
2. **The DB candle tables are serving caches, not research panels.** They are adjusted-only (KR),
   adjusted with `adj_close` 100% NULL (US), Upbit-only (crypto), upserted in place, and shallow:
   only 451 KR and 302 US symbols have ≥250 daily bars, and every symbol's history is capped at
   362/365 sessions. They cannot answer a cross-sectional question over a delisted-inclusive
   universe.
3. **Survivorship is the largest correctness gap.** The DB universes are current-vintage
   (12 KR delisted rows, 33 US delisted rows, 0 crypto delisted). The delisted-inclusive work
   exists only as research exports (`research/kr_delist_repair`, `research/us_universe`,
   `research/crypto_universe`) and is not joined to bars anywhere.
4. **Five datasets do not exist at all**: corporate actions (as data), options/vol, orderbook/trade
   tape, macro/rates, and index membership. Nothing in the DB is PIT, and news is only a partial,
   receipt-time feed. Four datasets exist only as short, overwritten snapshots: fundamentals, flows,
   crypto derivatives, and earnings/disclosure events.
5. **The first strategy validation needs five inputs, all reusable today**: delisted-inclusive
   universe, raw + adjusted daily bars with `available_at`, corporate actions, exchange calendars,
   and a liquidity/traded-value measure. No news, fundamentals, flows, options or macro are needed
   for it. Build order follows that line (§5).

| capability | today | first needed by |
|---|---|---|
| PIT daily bars, KR/US/crypto (files) | contract + code exist, **not run** | first validation |
| Delisted-inclusive universe | research exports exist, **not joined** | first validation |
| Corporate actions | Yahoo-only collector, **not run**; DB has none | first validation |
| Exchange calendars | `exchange_calendars` in `research/data_plane`/`stock_selection` | first validation |
| Traded value (liquidity) | US close×volume estimate; KR missing actual turnover | first validation |
| Earnings/disclosure dates | DB rows exist, overwritten, KR disclosures have no symbol | family extension |
| Fundamentals | daily KR snapshot (43 days) + empty US/financial/consensus tables | family extension |
| Flows (foreign/institutional/short/borrow) | KR 65-day overwritten snapshot; no US/borrow | family extension |
| News + relevance | partial feed, judge stopped 2026-09-01, 5,248 pending | family extension |
| Crypto funding/OI/LS | 57-day screener snapshot, 15–18% NULL | crypto futures family |
| Orderbook/trade tape | 7 quote rows total; live `get_orderbook` only | execution quality |
| Options/vol | none | options family |
| Macro/FX/rates | FX live-only; economic calendar stopped 2026-06-10 | regime/macro extension |
| Index membership | table modeled, absent from prod | index-relative family |

---

## 1. Strategy families and required datasets

### 1.1 Families in scope

| id | family | prediction / decision | horizon | what "good" depends on |
|---|---|---|---|---|
| **F1** | KR equity daily cross-sectional selection | rank KOSPI/KOSDAQ names for next-day / multi-day up or down | 1–10 sessions | delisted-inclusive PIT panel, adjusted + raw bars, actions, halt/VI state |
| **F2** | US equity daily cross-sectional selection | same, NYSE/NASDAQ/AMEX | 1–10 sessions | same, plus actual dollar turnover (close×volume is an estimate) |
| **F3** | crypto spot daily / intraday | Upbit KRW and Binance USDT pair ranking and timing | UTC day, 1m–4h | venue-specific bars incl. delisted pairs, native quote turnover, listing episodes |
| **F4** | crypto futures (derivatives) | funding/OI/liquidation context, perp vs spot basis, intraday signals | 5m–1d | funding history, OI history, perp spec/mark/index, liquidations |
| **F5** | execution quality | order placement vs marketable, fill probability, IS/markout, cost model | per order | L1/L2, trades, own fills, fee/tax model, session constraints |

Out of scope until separately registered: options strategies (the data survey exists in ROB-1452
but AT has no options path), long/short borrow, and any intraday equity strategy beyond the 1m
tables that already exist for a 39/31-symbol watchlist.

### 1.2 Dataset catalogue

The brief's dataset list, made concrete. "PIT" here always means the ROB-1443 rule: a consumer
joins a row only when `available_at <= decision_time`, and `observed_at` records when the collector
read it.

| id | dataset | grain | why a strategy needs it |
|---|---|---|---|
| **D1** | PIT OHLCV, raw **and** adjusted | (market, instrument, session, price_basis, vintage) | features, labels, execution reference; dividends/splits change both price and volume |
| **D2** | corporate actions | (market, instrument, action type, ex-date, fact hash) | separate real return from split/dividend artifacts; label crossings must be visible |
| **D3** | listing / delisting / survivorship | (market, instrument, episode interval) | a panel built from today's list loses the losers; terminal outcome is part of the label |
| **D4** | fundamentals, disclosures, earnings dates | (market, instrument, period, revision) | value/quality features and event-window exclusion; earnings dates are known-future inputs |
| **D5** | flows: foreign / institutional / individual / short / borrow | (market, instrument, session, metric) | KR 수급 is a first-class local signal; short/borrow constrains feasibility |
| **D6** | news / sentiment | (article key, first-seen, symbol judgment, judge version) | event and sentiment features; must be append-only to be PIT |
| **D7** | options / implied vol | (underlying, expiry, strike, side, quote, greeks) | vol risk premium, skew, event pricing |
| **D8** | funding / open interest / long-short / liquidations | (venue, contract, metric, metric time) | the only native crypto-derivatives signals |
| **D9** | orderbook / trades | (venue, instrument, session, sequence/time) | execution quality, spread/slippage, fill probability |
| **D10** | macro / FX / rates / calendar | (series, period, vintage) | regime, FX P&L on USD assets, event blackout |
| **D11** | index membership changes | (index, instrument, effective interval) | benchmark-relative features, index-add/delete events |
| **D12** | trading costs & constraints | (market, instrument/session, fee/tick/eligibility/halt/VI) | the strategy must trade something legal and available; costs are part of the label |

### 1.3 Family × dataset requirement

P0 = required for the first validation of that family; P1 = required before a production claim;
P2 = only when a registered hypothesis names it.

| dataset | F1 KR | F2 US | F3 crypto spot | F4 crypto futures | F5 execution |
|---|---|---|---|---|---|
| D1 PIT OHLCV raw+adjusted | **P0** | **P0** | **P0** | **P0** (mark/index + bars) | **P0** (intraday) |
| D2 corporate actions | **P0** | **P0** | P2 (no dividends/splits) | P2 | P1 |
| D3 listing/delisting | **P0** | **P0** | **P0** | **P0** (perp listing/settlement) | P2 |
| D4 fundamentals/disclosures/earnings | P1 | P1 | P2 | P1 (funding events, not financials) | P2 |
| D5 flows | P1 | P1 (short volume/interest) | P2 | P1 (LS ratio is D8) | P2 |
| D6 news/sentiment | P1 | P1 | P1 | P1 | P2 |
| D7 options/vol | P2 | P2 | n/a | P2 | P2 |
| D8 funding/OI/liquidations | n/a | n/a | P1 (context) | **P0** | P2 |
| D9 orderbook/trades | P2 | P2 | P1 | P1 | **P0** |
| D10 macro/FX/rates | P2 | P1 (FX for USD P&L) | P2 | P2 | P2 |
| D11 index membership | P2 | P2 | n/a | n/a | P2 |
| D12 costs/constraints | **P0** | **P0** | **P0** | **P0** | **P0** |

---

## 2. Coverage matrix (dataset × market)

Measured in production on 2026-11-03 07:20–07:35 KST (2026-11-02 22:20–22:35 UTC), read-only.
"Depth" is the observed min/max and the number of distinct symbols/sessions. "PIT" is a verdict,
not a feature list: `none` = no `available_at` and the row is overwritten; `partial` = has a
timestamp but the vintage is lost; `contract` = the ROB-1443 file contract applies.

### 2.0 Master matrix

| dataset | KR | US | crypto spot | crypto futures | verdict |
|---|---|---|---|---|---|
| D1 daily bars | `kr_candles_1d`, adjusted-only, 585k rows/4,022 syms/362 sessions, overwritten | `us_candles_1d` 1.05M/9,409/493, adjusted, `adj_close` NULL, overwritten | `crypto_candles_1d` 60k/309 Upbit KRW instruments/370 days, closed-only | none | **serving cache, not PIT** |
| D1 intraday | `kr_candles_1m` 1.16M/39 syms since 2026-08-17 | `us_candles_1m` 304k/31 syms since 2026-08-31 | `crypto_candles_1m` **empty** | none | watchlist only |
| D2 corporate actions | none in DB; Yahoo collector unrun | same | n/a | n/a | **missing** |
| D3 survivorship | current-vintage: 12 delisted rows | current-vintage: 33 delisted rows | 0 delisted rows | none | **missing in DB; exports exist** |
| D4 fundamentals | daily snapshot 43 days/4,401 syms, overwritten; financials table empty | financials/consensus **empty** | insight snapshots 18 rows, dead | funding event dates absent | **partial, not PIT** |
| D4 earnings/disclosures | 202,801 DART rows but **no symbol**; 28,648 earnings on 201 symbols | 15,813 earnings rows/5,844 syms, fresh | n/a | n/a | **partial** |
| D5 flows | 236k rows/3,982 syms/65 days, KR only, overwritten | **none** (no short/borrow) | none | long-short only in D8 | **partial KR, missing US** |
| D6 news | feed stopped 2026-09-30 except on-demand; relevance frozen 2026-09-01 | same, Finnhub live | same, Finnhub general live | n/a | **partial, not PIT** |
| D7 options/vol | none | none | n/a | n/a | **missing** |
| D8 funding/OI/LS | n/a | n/a | 57-day snapshot, 15–18% NULL | same snapshot + no liquidations | **partial** |
| D9 orderbook/trades | own fills only | own fills only | own fills only | none | **missing market tape** |
| D10 macro/FX/rates | FX live-only; economic calendar stopped 2026-06-10 | same | same | same | **missing** |
| D11 index membership | table modeled, absent from prod | none | n/a | n/a | **missing** |
| D12 costs/constraints | after-market list stale 2026-09-29; warnings empty; no tick/fee table | halts heuristic only; no fee table | tick/lot/min_notional in `crypto_instruments` | perp spec absent | **partial** |

### 2.1 D1 — PIT OHLCV, raw and adjusted

| | KR | US | crypto |
|---|---|---|---|
| store / collector | `public.kr_candles_1d`; TaskIQ `candles.daily.kr.sync` `30 16 * * 1-5` KST, KIS → Toss fallback | `public.us_candles_1d`; `candles.daily.us.sync` `0 7 * * 2-6` KST, KIS → Yahoo → Toss | `public.crypto_candles_1d`; `candles.daily.crypto.sync` `0 9 * * *` KST, Upbit |
| rows / symbols / sessions | 585,026 / 4,022 / 362 | 1,045,382 / 9,409 / 493 | 60,415 / 309 / 370 |
| range | 2025-05-09 → 2026-11-02 | 2024-12-27 → 2026-11-02 | 2025-10-28 → 2026-11-01 |
| price basis | KIS `FID_ORG_ADJ_PRC=0` ⇒ **adjusted only**; no raw series | KIS `MODP=1` ⇒ **adjusted only**; `adj_close` column **100% NULL** | unadjusted (crypto); `is_closed` true for all rows |
| source mix | `kis` 100% | `kis` 1,044,812; `yahoo` 482; `yahoo_fallback` 88 | `upbit` 100%, **KRW pairs only**: 309 Upbit KRW instruments exist, while Upbit lists 356 BTC-quoted and 258 USDT-quoted markets that are absent. Binance has 11 instruments and **no** candles |
| panel shape | 42 syms <30 bars, 172 at 30–119, **3,357 at 120–249**, 440 at 250–361, 11 at 362 | 1,194 <30, 2,426 at 30–119, **5,487 at 120–249**, 272 at 250–364, 30 at 365 | 54 of 309 have ≥300 days; median 209; min 6 |
| nulls | `value` 17,742 (3.0%), `volume` same | `value` 21,354 (2.0%) | none |
| recent coverage | 3 active symbols have no bar in the last 10 days | 0 active common stocks missing | 89 distinct UTC days in 90 |
| PIT | **none** — `ingested_at` is the last write; the last 10 bars are rewritten every run; a KIS run overwrites fallback rows; adjusted values are retroactively revised | **none** — same upsert, plus `adj_close` unused | **none** — conditional upsert by `(instrument, time, source)` |

Consequences: the DB is a **serving cache**. It is fine for `get_ohlcv`/indicators (its actual
consumers), but a research panel built from it is (a) survivor-only, (b) adjusted-only, (c) shallow
(median 126 KR / 129 US bars per symbol), and (d) rewritten when a source revises history. The
data-plane contract already solves all four; it is simply not running.

Raw vs adjusted per the broker inventory: KIS KR `FID_ORG_ADJ_PRC` 0=adjusted, 1=raw; KIS US
`MODP` 1=adjusted, 0=raw; Toss `adjusted=true/false`; Kiwoom/NHPLUG are not research sources
(Kiwoom mock-only; NHPLUG has no adjusted flag). Yahoo's `close` is split-adjusted retroactively,
so a backfilled row is the provider's view at observation time, not a contemporaneous vintage.

### 2.2 D2 — Corporate actions

| | status |
|---|---|
| DB | **no table** in `public`/`review`/`research`/`paper` (no dividend, split or action table) |
| existing collector | `research/data_plane` `corporate_actions` dataset — Yahoo dividends/splits + listing diff, first-observed `available_at`, 60-day lookback with outage extension. Running daily since 2026-11-02 (KR/crypto first partitions 2026-11-02) |
| coverage limits (contract §Known limits) | Yahoo only; rights issues, mergers, KR capital reductions are **not** covered; no broker client implements dividends or splits |
| PIT | contract: `fact_hash` identity, a revised amount is a new fact, `available_at` = first observation. No announcement time exists in the source |
| impact on F1/F2 | a split/dividend between t and t+h makes `close(t+h)/close(t)-1` meaningless unless the action is applied; ROB-1474 already marks action-crossing outcomes `unknown` from Yahoo event dates, which is the correct fail-closed behaviour |

### 2.3 D3 — Listing, delisting and survivorship

| | KR | US | crypto |
|---|---|---|---|
| DB universe | `kr_symbol_universe` 4,052 rows: 3,958 ACTIVE/active, 47 ACTIVE/inactive, 32 null-status/inactive, 12 DELISTED (12 with `delist_date`), 1 SCHEDULED | `us_symbol_universe` 14,188: 12,475 ACTIVE/active (+402 null/active), 424 ACTIVE/inactive, 33 DELISTED (8 dated), 2 DELISTED but active | `crypto_instruments` 320: 309 Upbit KRW + 11 Binance USDT, all active, **0 with `delisted_at`**; `upbit_symbol_universe` 928 markets (356 BTC / 314 KRW / 258 USDT) |
| writer | TaskIQ `symbols.kr.universe.sync` 07:10 KST, KIS master, `is_active` toggled in place | `30 21 * * *` KST, KIS overseas master, same | Upbit/Binance sync, same in-place toggle |
| PIT | **none** — no history of membership, no delisted-name retention beyond the row | **none** | **none** |
| research export | `research/kr_delist_repair` — main scope 2015–2024, 562 lifecycle resolutions, 404 ended listings, primary terminal classification 555/555 | `research/us_universe` — NASDAQ Trader/Wayback intervals from 2008, bracketed dates | `research/crypto_universe` — Binance 756 traded/256 delisted (33.9%); Upbit 287 live, **0 delisted recovered** (544 probes 404) |
| verdict | DB unusable for survivorship; export usable as the membership key | same | same, and Upbit delisted completeness is **unproven** |

A panel built from today's universe deletes the exact observations that make a cross-sectional
long/short result honest. ROB-1474 is explicit that its current numbers are survivor-only/bounded
diagnostics for this reason.

### 2.4 D4 — Fundamentals, disclosures, earnings dates

| store | rows | depth | freshness | PIT | notes |
|---|---:|---|---|---|---|
| `public.invest_kr_fundamentals_snapshots` | 185,175 | 43 days (2026-08-31→11-02), 4,401 symbols, median 43 days/symbol | daily, 90,525 rows/30d | **overwrite** (`DO UPDATE`) | price, PER, PBR, dividend yield, ROE, sector/industry; tvscreener/KRX |
| `public.market_valuation_snapshots` | 842,830 (KR 192,699 / US 650,131) | 53 days (2026-08-31→11-02), 15,049 symbols | 25 write-days/30d | **overwrite** (`computed_at`) | PER NULL 93% KR / 84% US; PBR NULL 90% / 72%; market cap present; 2 sources per symbol-day (KR naver+toss, US toss+tvscreener) |
| `public.financial_fundamentals_snapshots` | **0** | — | — | — | US collector failed 2026-11-01 (2 fails/7d); KR DART builder manual |
| `public.analyst_consensus_snapshots` | **0** | — | — | — | builder has no schedule, no reader |
| `public.market_events` (earnings) | 44,461 (KR 28,648 on 201 symbols; US 15,813 on 5,844 symbols) | event_date 2026-01-19→2027-01-01 | US created 2026-11-02; KR 2026-11-02 | **overwrite** on `source_event_id`; `fetched_at` is NULL on every row | US forward calendar 3,026 events/3,006 symbols in the next 60 days |
| `public.market_events` (KR disclosure) | 202,801 | 192 event dates (2026-01-19→11-02) | created 2026-11-02 | **overwrite** | **`symbol` is empty on every row** — the PIT join to a security is broken |
| `public.market_events` (economic) | 5,065 | 2026-05-06→2026-07-10 | **last created 2026-06-10** | overwrite | ForexFactory flow exists but unregistered |
| `public.research_reports` | 10 | last 2026-05-25 | 0 writes/30d | insert-only | `published_at` NULL on all rows |
| KIS financial APIs | — | — | **service ends 2026-11-11 16:00 KST** (7 domestic financial-analysis TRs) | — | do not build a new dependency on them (ROB-1444 correction) |

Verdict: KR fundamentals are a recent, overwritten snapshot — usable for "what is true today",
not for a historical feature. US fundamentals are effectively absent.

### 2.5 D5 — Flows (foreign / institutional / individual / short / borrow)

| | KR | US |
|---|---|---|
| store | `public.investor_flow_snapshots` | **none** |
| rows / symbols / depth | 236,087 / 3,982 / 65 days (2026-07-27→2026-10-30) | — |
| fields | foreign/institution/individual net, ranks, consecutive days, close/change/volume, foreign holding shares/rate | — |
| writer | TaskIQ (gated) + Prefect 18:10; Naver finance | — |
| PIT | **overwrite** on `(market,symbol,snapshot_date,source)`, `collected_at` rewritten; possible double trigger | — |
| short / borrow | `get_short_interest` is a live KIS read; **no table** | FINRA short volume/interest not collected; SEC 10c-1a borrow reporting is deferred to 2028-09/2029-03 |

The KR series is the only flow dataset with real depth, and it is still too short (65 sessions)
and mutable for a training feature. US short interest, borrow availability and fees are absent.

### 2.6 D6 — News and sentiment

| | measured |
|---|---|
| `public.news_articles` | 123,814 total: KR 53,120 (from 2025-06-22), US 63,809 (from 2019-01-24), crypto 6,885 (from 2025-12-30); 1,492 rows/30d |
| live writers | scheduled feeds stopped **2026-09-30**; on-demand `get_news` still writes `naver_item_news` (1,745), `finnhub_company_news` (961), `finnhub_general_news` (88) through 2026-11-02 |
| symbol linkage | `stock_symbol` populated for 2,735 KR rows (67 distinct symbols) and 1,993 US rows (52 symbols); crypto 0 — the news feed is not symbol-complete |
| `symbol_news_relevance` | 7,343: pending 5,248 (KR 2,893 / US 2,026 / crypto 329, created through 2026-11-02), confirmed 1,336, excluded 759. **Judgments frozen at 2026-09-01** (`judged_at` max) |
| `news_article_related_symbols` | 20,929 rows, **0 writes in 30d** |
| `crypto_insight_snapshots` | 18 rows, 2026-09-01/02 only (fear&greed, TVL, stablecoin, breadth, funding, BTC dominance); collector dead since |
| PIT | publisher time is nullable and self-claimed; the ingest path stores receipt time in two different naive zones (KST and UTC); judgments overwrite in place |

Verdict: the reader works, the writer that matters (the relevance judge) stopped two months ago.
This is exactly ROB-1488 §5.5 and the collector inventory's "retire now, rebuild on demand" verdict.

### 2.7 D7 — Options and implied volatility

No table, no tool, no collector. `get_market_index`/`get_crypto_*` do not provide an option chain.
ROB-1452 documents what is reachable if an options hypothesis is ever registered:

- **KR**: KIS REST/WS has the KOSPI200 call/put board with bid/ask, open interest and
  delta/gamma/vega/theta/rho/IV (domestic real/demo split); Kiwoom *OpenAPI+* (Windows OCX) has the
  chain and Greeks, Kiwoom **REST does not**; NH Futures (a separate entity from NH PLUG) has
  domestic/overseas option quotes and paper; Toss has no option endpoint.
- **US**: Alpaca options from 2024-02 but the free indicative feed is derived, not OPRA, with
  delayed/deduced fills; Tradier sandbox has no Greeks; IBKR needs both option and underlying
  subscriptions; Cboe publishes VIX history from 1990 as CSV, with unverified redistribution terms.

Verdict: missing, and any build is licence-first (OPRA or a broker entitlement), not code-first.

### 2.8 D8 — Funding, open interest, long/short, liquidations

| store | rows | depth | coverage | PIT |
|---|---:|---|---|---|
| `public.invest_crypto_screener_snapshots` | 11,400 | 57 days (2026-09-01→11-02), 299 symbols | `funding_rate` NULL 1,707 (15.0%), `open_interest_usd` NULL 2,007 (17.6%), `long_short_account_ratio` NULL 1,980 (17.4%) | daily snapshot, `DO UPDATE` |
| `public.crypto_insight_snapshots` | 18 | 2 days | funding_rate only 4 rows | effectively dead |
| liquidations | none | — | — | — |

Source semantics: the screener enrichment asks Binance USD-M for the whole perp universe in one
`premiumIndex` call and keeps the symbols that have a Binance perp; Upbit-only coins stay NULL
(fail-closed, never fabricated). OI/LS are per-symbol and were deferred in the ROB-443 phase 1.
REST history is short (funding 1,000 rows; OI about one month / 500 rows; aggTrades 48 h), so a
multi-year OI/liquidation history is **not** reachable from the live API.

### 2.9 D9 — Orderbook, trades and our own fills

| item | measured |
|---|---|
| `public.market_quote_snapshots` | **7 rows**, 2026-10-05 — effectively unused |
| `get_orderbook` | live read per call; nothing stored |
| `review.execution_ledger` | 5,537 fills: Upbit reconciler 3,476 / websocket 1,506 / manual 11; KIS reconciler 246 / websocket 72 / manual 40; Toss reconciler 186. `filled_at` 2026-02-01→2026-11-02. 0 duplicate idempotency keys |
| fill quality fields | `fee_amount` NULL on 1,578 websocket + 51 manual rows (no fee evidence); no pre-trade quote, no spread, no slippage vs arrival price |
| reconcile runs | KIS 1,434 and Upbit 1,426 non-dry-run runs in 30 days |
| other records | `review.trades` 462 (KR 242 / US 165 / crypto 55); `paper.paper_trades` 35 |

Verdict: we can compute *our own* fill outcomes from the ledger, but not execution quality —
there is no quote or tape at decision/send time, so IS, markout and fill-probability studies are
impossible today.

### 2.10 D10 — Macro, FX and rates

| item | status |
|---|---|
| FX | `exchange_rate_service` reads Toss primary + open.er-api fallback at call time; **not stored**. `get_fx_rate` live |
| rates | **no table** |
| macro calendar | `market_events` economic rows stopped 2026-06-10; `app/flows/forexfactory_calendar_flow.py` exists but has no deployment |
| rights | ROB-1490: FRED's current API terms prohibit ML/AI training and API storage/caching of the data, so FRED is **not** a default data-lake candidate; Fed DDP / ECB / BOK as original publishers are the alternative |

### 2.11 D11 — Index membership

- `public.kospi200_constituents` is **modeled but absent from the production DB** (migration not
  applied), and the `kospi200` router reads it (ROB-1488).
- No US index membership table; `get_market_index` is a live quote, not a constituent list.
- ROB-1490 confirms KRX index-constituent menus exist but the export path requires login and the
  historical start/completeness is unverified; US index constituents are in Norgate's index tables
  (S&P 500 from 1957, some composites marked *Inferred*) with an EULA that forbids sharing and
  requires deleting originals at expiry.

### 2.12 D12 — Costs and constraints

| constraint | store | measured | verdict |
|---|---|---|---|
| KRX after-market eligibility | `public.krx_after_market_eligibility` | 2,766 symbols, `list_asof` **2026-09-29**, 0 writes/30d | by contract the list must be ≤7 days old ⇒ **every KRX-after trade is refused** (ROB-1488 §5.4) |
| KR trading warnings / VI | `public.kr_stock_warnings` empty; VI not stored | 0 rows (no active warnings today); `get_short_interest` live | warning history absent; VI requires a KIS `inquire-vi-status` collector (history start unverified) |
| halts | heuristic `halted_suspect` (3 sessions frozen) | code only; `krx_halt_master: unavailable` | fail-open by design; not a master |
| per-symbol live sizing | `public.symbol_trade_settings` | 17 rows (6 KR, 11 US), last write 2025-12-02 / 2026-02-04 | stale; live sizing only, not research |
| crypto contract spec | `crypto_instruments` | 320 rows with `tick_size`, `lot_size`, `min_notional`, `status` | usable per instrument; no perp spec |
| fees/taxes | `config/trading_policy.yaml` + broker code | no fee table; fee evidence partially missing in the ledger | cost model must be assembled from config + venue schedules |
| calendars | `exchange_calendars` (XKRX/XNYS) + CSAT late-close list in the data plane | code | usable, already used by the harness |

### 2.13 Quality issues inherited from ROB-1488 that change a data decision

- §5.4 KRX after-market stale list (safety-relevant, above).
- §5.5 two relevance tables, one alive (`symbol_news_relevance` touched by new pending rows) and
  one dead (`news_article_related_symbols`); news ingest run log stops 2026-09-30.
- §5.6 sector labels frozen at 2026-07-27 (`symbol_sectors` 137 rows).
- §5.12 economic calendar is manual and stopped.
- §5.13 `paper_daily_snapshots` empty (the ROB-1450 export would emit nothing).
- §5.16 `crypto_candles_1m` empty while `get_ohlcv` silently falls back to Upbit HTTP.
- §5.17 `research.kr_candles_1m_toss` stopped 2026-09-29 (collector scheduleless by contract).
- Table-level: `market_valuation_snapshots` has multiple sources per symbol-day and heavy NULLs;
  `market_events` has 15,812 duplicate earnings rows in the last window and `fetched_at` NULL;
  `invest_momentum_event_snapshots` is 745k rows / 1.3 GB with no retention.

---

## 3. Gaps and candidate sources

"Free first, licensed second" follows the ROB-1444/ROB-1490 verdicts. A documented route is not a
verified entitlement: no account, key or order was used for any of these.

### 3.1 Gap list

| # | gap | markets | why it blocks | candidate (free / existing) | licensed fallback |
|---|---|---|---|---|---|
| G1 | No running PIT bar collector | KR, US, crypto | every feature/label depends on it | `research/data_plane` (KIS→Toss→Yahoo; Binance/Upbit) — **code exists, unrun** | — |
| G2 | No raw price series (adjusted overwrites) | KR, US | cannot reconstruct actions, cannot reproduce a vintage | data plane `bars_1d` raw + `bars_1d_adjusted` vintage | FnGuide (KR), CRSP (US) |
| G3 | No delisted price history joined to the panel | KR, US, crypto | survivorship bias | KR: KIS + `kr_delist_repair`; US: U3 intervals; crypto: Binance archive | FnGuide, CRSP, Norgate, Tardis (Upbit) |
| G4 | Corporate actions are Yahoo-only and unrun; no event ledger | KR, US | split/dividend labels | data-plane `corporate_actions`; KIS `ksdinfo` dividend/rev-split needs a client | FnGuide events, CRSP |
| G5 | KR disclosures have no symbol; events are overwritten | KR, US | event-window exclusion, PIT event features | OpenDART full financials (2015+, key, ~20k req/day) and disclosure search; SEC XBRL (2009+, 10 req/s) | FnGuide, Refinitiv/IBES |
| G6 | US fundamentals/consensus tables empty | US | value/quality features | SEC company facts/XBRL; existing yfinance path (serving only) | CRSP/Compustat, IBES |
| G7 | KR flows 65 days, mutable; US flows/borrow absent | KR, US | 수급 and short feasibility | KRX investor-by-type / foreign holdings (login); Naver (current); FINRA daily short volume (2018-08-01, 18:00 ET; revised later) and short interest (biweekly) | KRX licensed, FnGuide, FIS/Equilend borrow |
| G8 | News is not PIT; judge stopped | all | sentiment/event features | rebuild per collector-inventory §News (first_seen, one TZ, append-only judgments); GDELT 15-min (DOC2 API searches only 3 months) | licensed news (Tiingo/EODHD/FMP already surveyed) |
| G9 | Options absent | KR, US | vol features | KIS domestic option board (chain + Greeks); Cboe VIX CSV (1990) | OPRA, Alpaca Plus, Tradier, IBKR |
| G10 | Funding/OI/LS history short; no liquidations | crypto | derivatives features | Binance USD-M REST (funding 1,000 rows, OI ~1 month, aggTrades 48 h, depth 1,000) + Bybit | Tardis (exchange archives), Coinglass-type aggregators |
| G11 | No orderbook/trade tape | KR, US, crypto | execution-quality measurement | Binance archive/aggTrades + depth; Upbit trades; KIS websocket capture (the fillwire socket already exists) | NYSE Daily TAQ (1993+), Tardis, OPRA |
| G12 | Macro/FX/rates absent; FRED unusable for ML | all | regime and FX P&L | Fed DDP (H.10/H.15), ECB FX, BOK ECOS; existing Toss/open.er-api FX path | licensed macro vendor |
| G13 | Index membership absent | KR, US | benchmark-relative and index events | KRX index files (login), KOSPI200 table is missing; public S&P/Nasdaq lists for current membership | Norgate index constituents, S&P licence |
| G14 | Constraints partially stored and stale | KR, US | legality of the trade | refresh KRX-after list; KIND warning/VI via KIS; KRX tick table; venue fee schedules as config | KRX licensed |

### 3.2 Source verdicts worth carrying into a build (from the folded research)

- **KIS/Toss/Kiwoom/NH daily bars**: all documented; none guarantees raw + fully adjusted +
  delisted-inclusive history. Toss US adjusted ignores bases within 5%, so it is not a total-return
  series; Toss has no delisted history (404). Kiwoom adjusted prices must be queried backwards from
  the rights date. NH periods are `live` only for the KR/US period endpoints.
- **Yahoo is unverified/official-free, no SLA**, and its `close` is retroactively split-adjusted.
  It is a fallback, not a research authority.
- **KRX Open API** requires per-service usage approval, 10,000 calls/day per key, non-commercial
  display, no third-party provision; it is not a redistribution licence.
- **OpenDART** is free with a key, full financials from 2015, OFS/CFS separated; a correction
  changes the numbers (`last_reprt_at=N` includes corrections), so it is a revision source, not a
  complete PIT panel by itself.
- **SEC EDGAR** is keyless with fair-access limits (10 req/s), XBRL mandatory since 2009; `frames`
  is a latest-filing aggregate, not a historical PIT truth.
- **FINRA short volume ≠ short interest ≠ borrow**; short volume starts 2018-08-01 and excludes
  off-exchange/non-TRF volume. The SEC 10c-1a borrow reporting is deferred to 2028-09-28
  (public 2029-03-29), so "new SEC daily borrow feed" cannot be assumed available in 2026.
- **FRED** API terms currently restrict ML/AI training and API storage/caching — do not use it as a
  training-data source without a rights decision. Fed DDP/ECB/BOK are the original publishers.
- **Binance public data** is CC BY-NC-SA for non-production backtest research; live trading,
  commercial automated order flow and paid signals are restricted. Its archive (spot and
  futures) is the best free path for delisted pairs and long histories; the futures
  funding/OI/metrics archive coverage must be verified before planning around it.
- **Norgate** is $630/12 months, includes delisted from 1990 and index constituents with different
  start dates, and its EULA requires deleting originals/exports at expiry; derived data may be
  retained. This must be checked against the operator's processing environment before purchase.
- **Tardis** gives monthly free days (schema inspection) and tiered paid access; not a continuous
  free sample.

---

## 4. Ingestion design

### 4.1 One storage contract

Reuse and extend `data-plane-contract-v1` — do not invent a second one. The same envelope applies
to every dataset:

| field | rule |
|---|---|
| `instrument` | DB symbol format (KR 6-digit; US dot via `app.core.symbol`; crypto venue pair such as `BTCUSDT`/`KRW-BTC`); for futures add `contract` (e.g. `BTCUSDT-PERP`) |
| `venue` | exchange/feed identity (`KOSPI`, `NASDAQ`, `binance_usdt_spot`, `upbit_krw`, `binance_usdm`) |
| `event_time` | when the fact happened (bar close, ex-date open, funding time, quote timestamp) |
| `available_at` | earliest instant a decision could know the row; **the only join key for a feature** |
| `observed_at` | when the collector read it; `observed_at − available_at` is the observation lag |
| `source` / `source_version` | provider + API/version, e.g. `kis_daily_chart`/`kr-FHKST03010100`, `toss_candles_1d`/`api-v1`, `sec_xbrl`/`companyfacts-v1` |
| `content_hash` / `fact_hash` | sha256 of the canonical row without `observed_at`/`content_hash`; a re-observation hashes the same, a revision does not |
| `revision` | integer ≥1; a correction is a **new row** with `revision = previous+1` and `supersedes` = previous `content_hash`, never an in-place update |
| `payload` | dataset-specific fields only; secrets and account identifiers are never stored |

Storage layout (research, outside git):

```
<root>/<dataset>/<market>/date=YYYY-MM-DD/rows.jsonl.gz      write-once (os.link)
<root>/<dataset>/<market>/manifest.jsonl                     append-only, one record per partition
<root>/<dataset>/<market>/date=YYYY-MM-DD/meta-<sha>.json    write-once sidecar (crash repair)
```

Serving mirror (DB), only where a low-latency read needs it: a table per dataset with a **natural
key including the vintage/revision**, append-only for revisions, and TimescaleDB hypertable +
retention on the date column (ROB-1488 M5). No serving table may `DO UPDATE` a fact that a research
feature can read; either the row is immutable or the table is declared serving-only in code and in
the docs.

### 4.2 Dataset registry (keys, uniqueness, revision, retention, cadence)

| dataset | partition grain | unique key | revision rule | cadence | retention |
|---|---|---|---|---|---|
| `universe` | (market, day) | (market, instrument) per partition | listing status change = new partition row | daily | forever |
| `bars_1d` raw | (market, session) | (market, instrument, session) | corrected OHLCV = new revision | daily | forever |
| `bars_1d_adjusted` | (market, vintage day) | (market, instrument, bar_date) per vintage | new vintage every day; never overwrite | daily (own day only) | forever |
| `corporate_actions` | (market, day) | `fact_hash` (identity without observation time) | revised amount = new fact | daily | forever |
| `listing_outcomes` | (market) | (market, instrument) | terminal classification is append-only | event | forever |
| `events` | (market, day) | (market, category, source, source_event_id) | revised date/status = new revision | daily | forever |
| `fundamentals` | (market, day) | (market, instrument, fiscal_period, source, revision) | restatement = new revision | daily/weekly | forever |
| `flows` | (market, session) | (market, instrument, session, metric, source) | correction = new revision | daily | forever |
| `news_articles` | (market, day) | (url, content_hash) | corrected article = new row | 15 min | 10 years |
| `news_judgments` | (market, day) | (article, instrument, judge_version) | new judgment = new row | 15 min | 10 years |
| `derivatives` | (venue, day) | (venue, contract, metric, metric_time) | correction = new row | 1 h / daily | forever |
| `orderbook_trades` | (venue, instrument, day) | (venue, instrument, ts, seq) | append-only tape | streaming | 90 d hot / 2 y archive |
| `macro` | (series, period) | (series, period, vintage) | ALFRED-style vintage = new row | daily | forever |
| `index_membership` | (index, day) | (index, instrument, effective_from) | add/remove = interval rows | daily/monthly | forever |
| `costs_constraints` | (market, day) | (market, instrument/session, fact_type, source) | change = new row | daily/intraday | forever |

### 4.3 Batch vs streaming

| dataset | mode | why |
|---|---|---|
| D1 daily bars, D2 actions, D3 listing | batch (once per session after close + grace) | one row per instrument-day; write-once partition; broker budget is the limit |
| D4 fundamentals/events | batch (daily/weekly) | source cadence is daily at best; event dates are revised |
| D5 flows | batch (after close) | KRX/Naver daily; FINRA next-day file |
| D6 news | micro-batch (15 min) | first-seen must be captured close to crawl time; append-only judgments later |
| D8 derivatives | micro-batch (1 h) + daily archive | funding 8 h; OI/LS need polling; liquidations are event streams |
| D9 orderbook/trades | streaming capture + daily compaction | execution quality needs sequence and receive time; store hot then archive |
| D10 macro | batch (daily) | publication calendars, vintages |
| D11 index | batch (daily/monthly) + event check | changes are rare but dated |
| D12 constraints | batch + intraday checks | eligibility/halts/VI change during the session |

### 4.4 Backfill plan

| dataset | start | source order | budget | checks before publish |
|---|---|---|---|---|
| KR raw/adjusted bars | 2015-01-02 (frozen `kr-corpus-v1` before the data-plane start) | KIS → Toss → Yahoo | KIS 2 req/s research share; ~8,000 calls/day | coverage ≥0.95, no forming bars, `available_at` consistent |
| US raw/adjusted bars | 2016 (frozen corpora) / 2008 for universe | KIS → Toss → Yahoo | ~15,000 calls/day; US DAG timeout 6 h | same, plus venue mapping NASD/NYSE/AMEX |
| crypto spot bars | Binance 2017-08-17; Upbit 2017-09-25 | Binance archive/klines; Upbit days (+ portal/Tardis for delisted) | public archive | checksum (`.CHECKSUM`), UTC boundary, no-trade days stay missing |
| corporate actions | 2015 (Yahoo events), extend with filings | Yahoo → KIS ksdinfo (new client) → OpenDART/SEC | small | ex-date ≤ partition day, fact hash, revision visible |
| listing outcomes | 2000s | KR `kr_delist_repair`; US U3; crypto U4 | export | terminal outcome counts unresolved, never fills |
| events | 2015 | Finnhub (earnings), OpenDART (KR disclosures) | provider quota | symbol key present, revision, `available_at` |
| fundamentals | 2015 KR / 2009 US | OpenDART, SEC XBRL | 20k/day KR, 10 req/s US | fiscal period + filing date, OFS/CFS, revision |
| flows | 2015 KR / 2018-08 US | KRX/Naver, FINRA | daily | session key, no overwrite, borrow separate |
| news | 2019 (US) / 2025 (KR) / 2025-12 (crypto) | existing NI + Finnhub/Naver/GDELT | 15 min | first_seen end-to-end, one TZ, append-only judgments |
| derivatives | as available | Binance/Bybit REST + archive | rate limits | metric units, contract identity, missing ≠ zero |
| orderbook/trades | forward only | exchange capture | disk | sequence/gap markers, receive time |
| macro | series start | Fed DDP/ECB/BOK | daily | vintage, units, publication time |
| index | index start | KRX / public lists / Norgate | monthly | effective intervals, no retro-filtering |

Backfills are **partitioned by day** and refuse a day before its first universe partition; a
corrected older bar is a new revision in a new partition, never a rewrite of the original day.

### 4.5 Freshness SLOs and alerts

Due time = the earliest instant the dataset should be readable for a decision; a miss raises after
the grace. These are proposals for the operator, not registered schedules.

| dataset | due (local) | grace | alert |
|---|---|---|---|
| universe KR / US / crypto | 07:30 KST / 21:45 KST / 00:30 UTC | 30 m | missing partition, symbol drop >10% |
| bars raw KR / US / crypto | close + 30 m (16:00 KST / 16:30 ET / 00:05 UTC) | 1 h (Dagu) or 3 h (contract default) | `health` exit 1 → ping `/fail` |
| bars adjusted KR / US | same day 20:00 | 1 h | missing own-day vintage |
| corporate actions | 20:00 | 1 h | first-observation gap |
| earnings / disclosures | 08:00 KST | 2 h | partition missing, symbol-key missing |
| fundamentals KR / US | 20:00 KST / weekly Mon 09:00 | 6 h | revision count drop |
| flows KR / US short | 18:30 KST / next day 09:00 KST | 2 h | session missing |
| news | continuous 15 m | 30 m | first-seen lag p95, judge pending age |
| derivatives | hourly | 15 m | metric NULL rate >20%, contract missing |
| orderbook/trades | continuous | 5 m | sequence gap, receive-time lag |
| macro / index | daily 09:00 | 2 h | series missing, membership change |
| constraints | KRX-after weekly; halts/VI intraday | 1 d / 5 m | eligibility list age >7 d |

Alert wiring, in the order that already exists:

1. **`research.data_plane.cli health`** already fails on `gap`/`stale`/`corrupt`/`low_coverage`/
   `subset_partition`/`repaired_unverified`/`empty` and can ping a checksum URL
   (`--ping-url-env`, `<url>/fail` on failure). Extend it to the new datasets with the same rules.
2. **Writer heartbeat** (ROB-1488 H3): a read-only diagnostic that compares each dataset's declared
   cadence with the last successful write, surfaced in `get_operating_briefing`'s attention section.
   This is the guard that would have caught the relevance judge (stopped 2026-09-01), the KRX-after
   list (2026-09-29), and the economic calendar (2026-06-10).
3. **Grafana Cloud** (the fleet's existing external dashboards, ROB-1455/1486): alert rules on
   manifest age, coverage, and health exit per dataset/market, routed to the ops channel. The repo
   holds no Grafana config, so the rules are created on the Grafana side and referenced here.
4. **No new scheduler** is registered by this document. The three data-plane DAGs were registered
   separately on 2026-11-02 and approved 2026-11-03; any new-dataset DAG needs its own approval
   (hard rule 6).

### 4.6 Where it runs

| layer | what runs there | status |
|---|---|---|
| `research/data_plane` on the NCP host with broker credentials | KR/US/crypto universe, bars, adjusted vintage, actions | code exists; Dagu DAGs `deploy/dagu/data-plane-*.yaml` are **registered and running since 2026-11-02** |
| Prefect (`robin-prefect-automations`) | news crawl, screener/fundamentals/valuation/insight flows | runs today for serving; must write the PIT contract if reused for research |
| TaskIQ (`at-scheduler`/`at-worker`) | in-app serving writers (daily candles, universes, flows, watch scan) | runs today; **serving only** |
| Dagu (new datasets) | earnings/fundamentals/flows/news/derivatives DAGs | proposal only |
| desk systemd timers (NCP) | one-shot CLIs (INSIGHT/screener snapshot jobs) | operator-owned, outside this repo |
| streaming capture | fillwire / a new quote-tape process | fillwire exists for fills; quote tape is new |

Rule of thumb: *external fetch → Prefect/Dagu batch; in-app mutations → TaskIQ; research reads →
files.* Nothing in this plan asks the scheduler to run automatically.

### 4.7 Fit to the target split

The target architecture (ROB-1488 §6) is Go execution core, Python research batch writing
snapshots, thin MCP reading them. The data plan is the boundary between the last two:

- **Research batch** owns collection and writes PIT partitions; it is the only writer of research
  datasets. It already has a budget, a broker read-view and a health contract.
- **Thin MCP** reads snapshots (or one core endpoint) instead of calling brokers per request. Every
  dataset in §4.2 that a tool needs becomes a snapshot read.
- **Go execution core** owns live quotes, order send and the fill ledger; it should **produce** the
  execution-quality tape (quote at send, fill, fee) as an append-only stream into the same
  contract, instead of AT reconstructing it after the fact.

The split makes the storage decision easy: anything the strategy *model* consumes is a batch
snapshot; anything the strategy *does* (order, fill, cancel) is core-owned and streamed.

---

## 5. Minimal first set and ordered build list

### 5.1 Minimal first set for the first strategy validation

The first validation is F1+F2 daily cross-sectional (next-day/multi-day up-down), the ROB-1474
harness. It needs exactly five inputs, and all five have a code path today:

| # | input | source | acceptance |
|---|---|---|---|
| S1 | delisted-inclusive universe with stable instrument ids | KR `kr_delist_repair` export + KIS master; US `us_universe` PIT intervals | every name that traded in the window is present; delisted count reported, not silently dropped |
| S2 | raw daily bars with `available_at` | data-plane `bars_1d` (KIS→Toss→Yahoo) | coverage ≥0.95 per partition; no forming bar; observation lag recorded |
| S3 | adjusted vintage + corporate actions | data-plane `bars_1d_adjusted` + `corporate_actions` | action-crossing outcomes are `unknown`, never silently adjusted |
| S4 | exchange calendars | `exchange_calendars` (XKRX/XNYS), CSAT/early-close exceptions | non-session days are never gaps; benchmark gaps fail closed |
| S5 | traded value / liquidity | KIS `acml_tr_pbmn` for KR (actual), `close×volume` estimate for US until TAQ | the basis is recorded per row; estimates are labeled |

Explicitly **not** in the minimal set: news, fundamentals, flows, options, macro, index membership,
orderbook. Each enters only when a registered hypothesis names it (ROB-1474/1475's conclusion).

Acceptance for the first validation as a whole: the ROB-1474 harness re-runs on the new panel with
unchanged code, reports delisted-inclusive coverage and every exclusion reason, and still refuses a
market-wide readiness claim. Nothing is promoted to policy.

### 5.2 Ordered build list (one PR-sized unit each)

Reuse first; new collectors second. "Ops" items are approvals/execution, not code PRs.

| # | unit | kind | depends on | acceptance | what it replaces |
|---|---|---|---|---|---|
| **B0** | Let the data plane accumulate KR/US/crypto (registered 2026-11-02; no backfill before the first universe partition, by the point-in-time design), keep the health pings green | ops (no PR) | done: Dagu registered and approved, broker credentials on the worker | `health` green for 5 consecutive sessions; coverage ≥0.95 | — |
| **B1** | Data plane: add `traded_value` + `value_basis` to equity bars (KIS `acml_tr_pbmn`, Yahoo estimate labeled) | PR (research) | B0 | contract test + manifest field; estimate never labeled actual | close×volume-only liquidity |
| **B2** | Data plane: `listing_outcomes` dataset from U2/U3/U4 exports + join rule for bar selection | PR (research) | B0 | delisted names appear in the panel with terminal status; no forward-fill | DB universe as a panel |
| **B3** | Data plane: corporate actions from a broker source (KIS `ksdinfo` client) with Yahoo fallback | PR | B0 | dividend/split rows reconcile with Yahoo for a sample; revisions visible | Yahoo-only actions |
| **B4** | Writer heartbeat + freshness SLO guard (ROB-1488 H3) covering every collected dataset | PR (read-only) | B0 | a deliberately stopped writer turns the briefing/brief red; no writes | silent stops (§5.4/5.5/5.12) |
| **B5** | Serving candle writers: stop rewriting bars older than the last session and label the tables serving-only | PR | none | a row-level diff shows only today's bar updated; docs/tests state serving-only | in-place revision of up to 10 bars |
| **B6** | Events dataset: earnings + KR disclosures with symbol key and `available_at` | PR | B0 | every event joins to an instrument; DART symbol mapping fixed | `market_events` overwrite + empty symbol |
| **B7** | Fundamentals dataset: KR OpenDART full financials (2015+) with revisions | PR | B6 | fiscal period + filing date + OFS/CFS; correction = new revision | empty financial tables |
| **B8** | US fundamentals: SEC XBRL company facts | PR | B7 pattern | 10 req/s respected; filing date is `available_at` | empty US fundamentals |
| **B9** | Flows: KR investor flow append-only + US FINRA short volume/short interest | PR | none | no `DO UPDATE`; session key; short volume ≠ short interest labeled | 65-day mutable KR series |
| **B10** | News PIT rebuild: `first_seen_at` end-to-end, one timezone, append-only judgments | PR | none | judge backlog drains; `judged_at` advances; pending age alert | frozen relevance, receipt-time rows |
| **B11** | Crypto derivatives archive: funding, OI, long-short (Binance/Bybit) with contract identity | PR | B0 | missing ≠ 0; interval/unit preserved; OI window documented | 57-day screener snapshot |
| **B12** | Crypto spot completion: Binance archive + delisted pairs; Upbit delisted via portal/Tardis if licensed | PR | B0 | delisted pairs counted; no-trade days stay missing | Upbit-live-only 309 pairs |
| **B13** | Execution tape: quote snapshot at proposal/send + fill/fee stream from the core | PR | Go core boundary | IS/markout computable per order; no account ids stored | 7 quote rows total |
| **B14** | Macro/FX/rates: Fed DDP/ECB/BOK + a macro calendar; FRED excluded by its terms | PR | none | vintage per series; publication time is `available_at` | live-only FX, dead calendar |
| **B15** | Index membership PIT (KOSPI200 table, US index lists) | PR | none | effective intervals, no retro-filtering | absent table |
| **B16** | Constraints: refresh KRX-after list, halt/VI collector, fee/tick tables | PR | none | list age ≤7 d alert; VI history start documented | stale eligibility, no VI |
| **B17** | Options (only if an options hypothesis is registered) | PR (licence first) | operator licence decision | chain + Greeks + quotes; indicative vs OPRA labeled | none |

Ordering rationale: B0–B5 make the *first validation* honest with existing code; B6–B12 add the
datasets a second hypothesis would ask for; B13–B17 are market-microstructure/options scope and
should wait for a hypothesis and (usually) a licence. B4 comes early because the measured failure
mode in this repo is not "wrong data" but "a writer stopped two months ago and nobody saw it".

### 5.3 Do not build now

- A second bar collector: `research/data_plane` already implements source order, budget, fallback,
  write-once partitions and health.
- A new DB table for daily bars: the serving tables exist; the research presence is files.
- News, fundamentals, flows, options, macro collectors for a strategy that has not been registered.
- Scheduler registration of any kind from this document.
- Any change to a default-off gate, host allowlist or cap (hard rule 4).

---

## Appendix A — Production read-only queries used

Path: read from the desk host into the active API container; `SELECT` / `information_schema` only,
`statement_timeout` 15–60 s, `default_transaction_read_only = on`, aggregate output only. No order
row, account number or secret was read into this document.

```bash
ssh ncp 'C=$(cat /root/at-run/api-active-color); docker exec -i at-api-$C sh -c "cd /app && /app/.venv/bin/python -"' < script.py
```

**A1 — table/column census** (bounded to the four app schemas)

```sql
select table_schema, table_name,
       string_agg(column_name, ',' order by ordinal_position) cols
from information_schema.columns
where table_schema in ('public','review','research','paper')
group by 1,2 order by 1,2;
```

**A2 — coverage per table** (run once per table; `<t>` = strongest timestamp)

```sql
select count(*) n,
       count(distinct <t>) dates,
       min(<t>) min_t, max(<t>) max_t,
       count(distinct <sym>) syms
from <schema>.<table>;

select count(*) from <schema>.<table> where <t> >= now() - interval '30 days';
```

**A3 — panel shape**

```sql
select count(*) syms,
       count(*) filter (where n>=250) ge250,
       count(*) filter (where n>=60)  ge60,
       percentile_cont(0.5) within group (order by n) med_bars,
       min(n), max(n)
from (select symbol, count(*) n from public.kr_candles_1d group by 1) x;

select case when n<30 then '<30' when n<120 then '30-119'
            when n<250 then '120-249' when n<362 then '250-361' else '362' end b,
       count(*)
from (select symbol, count(*) n from public.kr_candles_1d group by 1) x
group by 1 order by 1;
```

**A4 — source mix / basis / nulls**

```sql
select source, venue, count(*) n, count(distinct symbol) syms,
       min(time) mn, max(time) mx
from public.kr_candles_1d group by 1,2 order by 3 desc;

select source, count(*) n, count(distinct symbol) syms,
       min(time) mn, max(time) mx, sum((adj_close is null)::int) adj_null
from public.us_candles_1d group by 1 order by 2 desc;

select i.venue, i.product, count(*) n, count(distinct c.instrument_id) syms,
       min(c.time) mn, max(c.time) mx, sum((c.is_closed is false)::int) open_rows
from public.crypto_candles_1d c
join public.crypto_instruments i on i.id = c.instrument_id
group by 1,2 order by 3 desc;
```

**A5 — universes / survivorship**

```sql
select listing_status, is_active, count(*) n, count(delist_date) delist_dated
from public.kr_symbol_universe group by 1,2 order by 3 desc;

select listing_status, is_active, count(*) n, count(delist_date) delist_dated,
       count(*) filter (where is_common_stock) common
from public.us_symbol_universe group by 1,2 order by 3 desc;

select venue, status, count(*) n, count(delisted_at) delisted_dated
from public.crypto_instruments group by 1,2 order by 3 desc;
```

**A6 — freshness / writer evidence**

```sql
select market, feed_source, max(created_at) mx_created, max(scraped_at) mx_scraped,
       sum((article_published_at is null)::int) pub_null, count(*) n
from public.news_articles
where created_at >= now() - interval '60 days'
group by 1,2 order by 5 desc;

select status, count(*) n, min(judged_at), max(judged_at),
       min(updated_at), max(updated_at)
from public.symbol_news_relevance group by 1 order by 2 desc;

select category, market, count(*) n, max(fetched_at) mx_fetch,
       max(created_at) mx_created, count(distinct symbol) syms
from public.market_events group by 1,2 order by 3 desc;

select category, market, count(*) n,
       count(*) filter (where symbol is not null and symbol <> '') with_symbol
from public.market_events group by 1,2 order by 3 desc;

select list_source, list_asof, count(*) from public.krx_after_market_eligibility
group by 1,2;

select market, source, count(*) n, count(distinct (snapshot_date,symbol)) keys
from public.market_valuation_snapshots group by 1,2 order by 3 desc;
```

**A7 — execution ledger (own fills only)**

```sql
select broker, account_mode, source, count(*) n, count(distinct symbol) syms,
       min(filled_at), max(filled_at),
       sum((filled_qty is null)::int), sum((filled_price is null)::int),
       sum((fee_amount is null)::int)
from review.execution_ledger group by 1,2,3 order by 4 desc;

select count(*) from (
  select broker, account_mode, venue, broker_order_id, fill_seq, count(*) c
  from review.execution_ledger group by 1,2,3,4,5 having count(*) > 1) x;

select broker, dry_run, count(*) runs, min(finished_at), max(finished_at)
from review.execution_ledger_reconcile_runs
where finished_at >= now() - interval '30 days'
group by 1,2 order by 3 desc;
```

**A8 — crypto derivatives coverage**

```sql
select count(*) n, max(snapshot_date) mx,
       sum((funding_rate is null)::int) fr_null,
       sum((open_interest_usd is null)::int) oi_null,
       sum((long_short_account_ratio is null)::int) ls_null
from public.invest_crypto_screener_snapshots;
```

Not completed: a follow-up batch (event source breakdown and candle ingest lag) did not return
because SSH to the host stopped answering during the review. It is not needed for any number in
this document; every figure above comes from A1–A8.

---

## Appendix B — measured inventory (2026-11-03 07:20–07:35 KST)

| table | rows | depth | last write | PIT verdict |
|---|---:|---|---|---|
| `public.kr_candles_1d` | 585,026 | 362 sessions, 4,022 syms, 2025-05-09→2026-11-02 | daily; 74,955 rows/30d | serving only |
| `public.us_candles_1d` | 1,045,382 | 493 sessions, 9,409 syms, 2024-12-27→2026-11-02 | daily; 189,119 rows/30d | serving only |
| `public.crypto_candles_1d` | 60,415 | 370 days, 309 Upbit KRW instruments, 2025-10-28→2026-11-01 | daily; 8,587 rows/30d | serving only |
| `public.kr_candles_1m` | 1,161,123 | 39 syms, since 2026-08-17 | 30d | serving only |
| `public.us_candles_1m` | 303,738 | 31 syms, since 2026-08-31 | 30d | serving only |
| `public.crypto_candles_1m` | **0** | — | — | dead |
| `research.kr_candles_1m_toss` | 1,171,860 | 832 syms, 2026-07-21→2026-09-29 | 0/30d | stopped |
| `public.kr_symbol_universe` | 4,052 | 12 delisted | 2026-11-02 | current vintage |
| `public.us_symbol_universe` | 14,188 | 33 delisted | 2026-11-02 | current vintage |
| `public.upbit_symbol_universe` | 928 | markets | 2026-10-27 | current vintage |
| `public.crypto_instruments` | 320 | 309 Upbit KRW + 11 Binance USDT, 0 delisted_at | 2026-10-28 | current vintage |
| `public.market_valuation_snapshots` | 842,830 | 53 days | 2026-11-02 | overwritten |
| `public.invest_kr_fundamentals_snapshots` | 185,175 | 43 days, 4,401 syms | 2026-11-02 | overwritten |
| `public.financial_fundamentals_snapshots` | **0** | — | — | absent |
| `public.analyst_consensus_snapshots` | **0** | — | — | absent |
| `public.market_events` | 252,327 | 269 event dates | 2026-11-02 | overwritten |
| `public.news_articles` | 123,814 | 2019→2026-11-03 | 2026-11-03 | receipt-time, not PIT |
| `public.symbol_news_relevance` | 7,343 | 5,248 pending | judged 2026-09-01 | frozen |
| `public.news_article_related_symbols` | 20,929 | — | 0/30d | dead |
| `public.crypto_insight_snapshots` | 18 | 2 days | 2026-09-02 | dead |
| `public.investor_flow_snapshots` | 236,087 | 65 days, 3,982 syms | 2026-10-30 | overwritten |
| `public.invest_screener_snapshots` | 442,754 | 51 days | 2026-11-02 | overwritten |
| `public.invest_crypto_screener_snapshots` | 11,400 | 57 days, 299 syms | 2026-11-02 | overwritten |
| `public.invest_momentum_event_snapshots` | 745,246 | 45 days | 2026-11-02 | overwritten |
| `public.krx_after_market_eligibility` | 2,766 | asof 2026-09-29 | 0/30d | stale |
| `public.kr_stock_warnings` | 0 | — | — | empty (no active warnings) |
| `public.symbol_sectors` | 137 | — | 2026-07-27 | stale |
| `public.market_quote_snapshots` | 7 | — | 2026-10-05 | dead |
| `public.research_reports` | 10 | — | 2026-05-25 | dead |
| `public.symbol_trade_settings` | 17 | — | 2026-02-04 | stale |
| `review.execution_ledger` | 5,537 | 2026-02-01→2026-11-02 | 23/30d | own fills |
| `review.execution_ledger_reconcile_runs` | 15,975 | 31/30d | 2026-11-02 | — |
| `review.trades` | 462 | 2026-04-26→2026-11-02 | 12/30d | own trades |
| `paper.paper_trades` | 35 | 2026-10-17→2026-10-31 | 12/30d | sim |

Derived facts used above: KR bars-per-symbol histogram (42 / 172 / 3,357 / 440 / 11 for <30 /
30–119 / 120–249 / 250–361 / 362); US (1,194 / 2,426 / 5,487 / 272 / 30 for <30 / 30–119 /
120–249 / 250–364 / 365); crypto 54/309 with ≥300 days, median 209.

---

## Appendix C — external research folded in

| ref | what it adds here | status |
|---|---|---|
| **ROB-1444** (dots, 2026-11-01) | broker/free source verification for KR/US daily bars, master, corporate actions, TPS, terms; KIS financial API shutdown 2026-11-11 | folded |
| **ROB-1449** | category matrix; read here through ROB-1444 and ROB-1490 citations, no standalone hk doc found | folded indirectly |
| **ROB-1452** (dots, 2026-11-01) | options: KIS KR board + Greeks, Alpaca/Tradier/IBKR US limits, Cboe VIX | folded (§2.7, G9) |
| **ROB-1490** (dots, 2026-11-02, **landed during this review 07:26 KST**) | rights/PIT contract for KRX/KOGL4, OpenDART, SEC XBRL, FINRA short volume vs short interest (SEC 10c-1a deferred 2028/2029), FRED ML restriction, Binance archive terms, Bybit/Tardis, Norgate EULA, GDELT; P0/P1/P2 minimal sets per family | folded (§3, §5) |
| **ROB-1474 / ROB-1475** (dots, 2026-11-02) | what the harness reads, the survivor-only/bounded verdict, TFT input classes, next-feature candidates | folded (§1, §5.1) |
| **ROB-1447** (dots, 2026-11-01) | limit vs marketable orders, IS/unfilled cost, why an execution-quality label needs the whole order intent | folded (§1 F5, §2.9) |
| **ROB-1445 / ROB-1446** | named in the brief as prior dots results | **not found in hk** at review time (no doc under `dots/results/`); not used |
| ROB-1443 / ROB-1448 | data-plane contract, collector decisions, broker sources and budget | reused as the base contract (§4) |
| ROB-1488 | table counts, health §5, target split §6, retire-first §7 | reused throughout |

---

## Appendix D — limits of this review

- Production reads are aggregates from one moment. A table's depth can change tomorrow; the
  verdicts are structural (overwrite/no `available_at`/current-vintage), not only numeric.
- The data-plane artifact root was absent on the desk host used here. The code and contract were
  read; no partition was inspected, so "not running" here was wrong: the DAGs had been running on the
  NCP host since 2026-11-02 (corrected by operator-desk 2026-11-03).
- Broker entitlements, quotas and storage/redistribution rights were **not** exercised. Every
  provider claim is a documented route with the limits the dots research found; none is a verified
  account capability.
- No local database or fixture server was started. No write, migration, deploy or scheduler change
  was made. No order, account or credential was touched.
- The ROB-1474 harness numbers are survivor-only/bounded diagnostics (k=2) and are not used here
  as a performance claim.
