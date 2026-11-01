# Data collector inventory and keep/fix/rebuild/retire decisions (ROB-1443)

Taken 2026-11-01 for the TFT/boosting data plane (plan units U7/U8). Read-only:
nothing was paused, resumed, deleted or re-enabled.

**Scope.** Every job in this repo and in `robin-prefect-automations` that
fetches external data and writes it somewhere, plus the research collectors
that U2/U3/U4 added. Monitors, reconcilers and session kickoffs are not
collectors; they are listed at the end for completeness.

**Revisions read.**

| repo | revision | note |
|---|---|---|
| auto_trader | `9b082cbbc` (this PR's base) | |
| robin-prefect-automations (RPA) | `ec5f154` (`origin/main` as of the local clone's last fetch) | local working tree was 5 commits behind; files were read with `git show ec5f154:<path>`. No `git fetch` was run, so newer remote commits may exist (UNVERIFIED) |
| news-ingestor (NI) | `47b515d` (2026-06-11, local copy) | production runs the copy installed in the auto_trader image; the local copy may be stale (UNVERIFIED) |

**Health evidence** comes from the read-only fleet schedule inventory taken
2026-11-01 19:55 KST (desk; not in this repo). "Prefect: Completed 11-01" means
the inventory saw that deployment's last run complete. TaskIQ labels run inside
the `at-scheduler` container; the inventory lists them as one row ("~35 cron
labels"), so **per-label health of TaskIQ collectors is UNVERIFIED** unless
noted. Which gated TaskIQ labels are switched on in production is UNVERIFIED
(flags default false; production env was not read).

Link shorthand: `AT:` paths are in this repo; `RPA:` paths are
`https://github.com/mgh3326/robin-prefect-automations/blob/ec5f154/<path>`.

## What point-in-time (PIT) means here

The data contract (`docs/research/data-plane-contract-v1.md`) needs, for every
row, the instant it became knowable (`available_at`) and a guarantee that the
stored value is the value known then. A collector fails that test when it:

1. **overwrites** a stored value (`ON CONFLICT DO UPDATE`, delete-then-insert,
   in-place status flips) — the old vintage is lost;
2. **refreshes its own timestamp** on every run (`ingested_at = now()`), so the
   timestamp no longer says when the value first existed;
3. stores **no fetch time**, or a time of a later hop (receipt instead of crawl);
4. stores **naive timestamps** in mixed time zones.

## Decision table

Decisions are about use as **U7/U8 research input**. "Keep (serving)" means the
job keeps feeding the live app, but research must not train on its table.

| # | collector | trigger | source → destination | PIT problems | health evidence | decision |
|---|---|---|---|---|---|---|
| 1–3 | News crawl KR / US / crypto core | RPA `flows/auto_trader/news_ingestion.py:167-224` (hourly, registered paused) → NI `news-ingestor run --store` | RSS/Naver feeds → NI Postgres `raw_articles` | NI keeps `first_seen_at`, but re-seen articles update `raw_json` in place | Prefect pool `pyri-process` has had **no worker since 2026-10-30 08:44 UTC**; before that **50/50 runs failed** (`psycopg OperationalError: connection is bad`) | **Retire now; rebuild only on demand** (see below) |
| 4 | News pending push kr/us/crypto | RPA `news_ingestion.py:247-299`, `src/robin_automation/news_ingestor.py:213-234` (every 15 min) | NI outbox → AT `POST /api/v1/news/ingest/bulk` (`AT: app/routers/news_analysis.py:83`) → `news_articles` (`AT: app/services/llm_news_service.py:284-442`) | `scraped_at`/`created_at` = **AT receipt time** (KST-naive), not crawl time; NI `first_seen_at` is not sent; raw payload not stored | same: no worker since 10-30 | **Retire now; rebuild only on demand** |
| 5 | On-demand symbol news (`get_news`) | MCP call; `AT: app/services/symbol_news_service.py:346-375` | Naver/Finnhub → `news_articles`, `symbol_news_relevance` | insert-only, but timestamps **UTC-naive** while path #4 writes KST-naive into the same columns | not a scheduled job | Keep (serving) |
| 6 | News relevance judge | `AT: app/tasks/news_relevance_judgment_tasks.py:21`, `app/routers/news_relevance.py:34` | LLM judge (out of process) → `symbol_news_relevance` | status/score/reason **overwritten in place** (`AT: app/services/symbol_news_store.py:256-294`) | UNVERIFIED | Keep (serving); fix before any research use (append-only judgments) |
| 7 | Related-symbols backfill | `AT: scripts/backfill_news_related_symbols.py` (manual) | matcher → `news_article_related_symbols` | rows added later than the article; filter on `created_at` | manual | Keep (manual) |
| 8 | Daily candles KR | TaskIQ `candles.daily.kr.sync` `30 16 * * 1-5` KST (`AT: app/tasks/daily_candles_tasks.py:22-27`) | KIS, Toss fallback → `kr_candles_1d` | **upsert DO UPDATE** with `ingested_at = now()` (`AT: app/services/daily_candles/repository.py:409-419`); rewrites the last 10 bars every run; KIS overwrites fallback rows | UNVERIFIED (inside at-scheduler) | Keep (serving: `get_ohlcv`, indicators). **Rebuild for research** → `bars_1d` (this PR) |
| 9 | Daily candles US | `candles.daily.us.sync` `0 7 * * 2-6` KST (`:30-35`) | KIS → Yahoo → Toss → `us_candles_1d` | as #8, plus `adj_close` is back-adjusted for later splits/dividends (future information in old rows) | UNVERIFIED | Keep (serving). **Rebuild for research** → `bars_1d` |
| 10 | Daily candles crypto | `candles.daily.crypto.sync` `0 9 * * *` KST (`:38-43`) | Upbit → `crypto_candles_1d` | DO UPDATE unless the stored row is closed with the same source (`repository.py:326-349`) | UNVERIFIED | Keep (serving). **Rebuild for research** → `bars_1d` |
| 11 | Candle write-back on read | `AT: app/services/market_data/service.py:773,828` | Yahoo/Toss/KIS → `kr/us_candles_1d` | same conditional upsert, outside any schedule | side effect | Keep (serving) |
| 12 | Daily candles backfill | `AT: scripts/backfill_daily_candles.py` (manual) | KIS/Upbit/Yahoo → `*_candles_1d` | upsert | manual | Keep (manual) |
| 13–15 | Alpaca US daily / Kiwoom KR daily / NHPLUG period | `AT: scripts/alpaca_collect_us_daily.py`, `scripts/kiwoom_collect_kr_daily.py`, `scripts/nhplug_collect_period.py` (manual) | → `*_candles_1d` | insert-only (`DO NOTHING`) — first value kept | manual | Keep (manual) |
| 16–17 | Intraday 1m KR / US | TaskIQ `candles.kr.sync` / `candles.us.sync` `*/10` | KIS → `kr/us_candles_1m` | DO UPDATE when values differ; no `ingested_at` found (a later migration adding one is UNVERIFIED) | UNVERIFIED | Keep (serving); out of scope (daily inputs only) |
| 18 | Toss 1m research | RPA `flows/auto_trader/kr_candles_1m_toss.py:69,120-126` (20:40 KST, paused) → `AT: scripts/backfill_kr_candles_1m_toss.py` | Toss → `research.kr_candles_1m_toss` | insert-only; `time_utc` = bar end | **paused, never ran** | Keep (paused; event studies, not U7/U8) |
| 19 | Promote 1m to research | `AT: scripts/promote_kr_candles_to_research.py` (manual) | internal → `research.kr_candles_*` | insert-only | manual | Keep (manual) |
| 20 | Crypto 1m (Binance WS) | `AT: app/services/minute_candles/repository.py:92-117`, `app/services/brokers/binance/ingest.py` | Binance WS → `crypto_candles_1m` | DO UPDATE | **no entrypoint found** (UNVERIFIED whether anything runs it) | Retire-candidate (needs owner confirmation; not touched here) |
| 21 | KR symbol universe | TaskIQ `symbols.kr.universe.sync` `10 7 * * *` KST | KIS master → `kr_symbol_universe` | `is_active` toggled in place, no delist date (`AT: app/services/kr_symbol_universe_service.py:281-292`); U2 found 12/4,011 rows with a delist date | UNVERIFIED | Keep (serving). **Rebuild for research** → `universe` partitions (this PR) + U2 export |
| 22 | US symbol universe | `us_symbol_universe` sync `30 21 * * *` KST | KIS overseas master → `us_symbol_universe` | same in-place toggle | UNVERIFIED | Keep (serving). **Rebuild for research** → `universe` + U3 PIT table |
| 23 | Upbit symbol universe | `upbit_symbol_universe` sync `15 6 * * *` KST | Upbit → `upbit_symbol_universe` | same | UNVERIFIED | Keep (serving). **Rebuild for research** → `universe` + U4 PIT table |
| 24 | Toss symbol master | RPA `flows/auto_trader/toss_symbol_master_sync.py:122-139` (07:20 Mon–Fri) | Toss → `toss_master_*` columns, `market_valuation_snapshots` | in-place update + upsert | Prefect: Completed | Keep (serving) |
| 25 | US common-stock flags | `AT: scripts/sync_us_common_stock_flags.py` (manual) | NASDAQ Trader → `us_symbol_universe.is_common_stock` | in place | manual | Keep (manual) |
| 26 | Toss warnings | RPA `flows/auto_trader/toss_warnings_sync.py:102-117` (07:30) | Toss → `kr_stock_warnings` | **delete then insert** per symbol | Prefect: Completed | Keep (serving); fix (append-only vintages) before research use |
| 27–28 | KR investor flow (TaskIQ gated + Prefect) | `AT: app/tasks/investor_flow_snapshot_tasks.py:28,89-91` (`30 8 * * 1-5`, gated) and RPA `flows/auto_trader/investor_flow_snapshots.py:138,173-187` (18:10 Mon–Fri) | Naver → `investor_flow_snapshots` | DO UPDATE on `(market,symbol,snapshot_date,source)`, `collected_at` overwritten | Prefect: Completed; whether the TaskIQ label is on is UNVERIFIED (possible double trigger) | Keep (serving); fix before research use |
| 29–30 | Market valuation KR / US | `AT: app/tasks/market_valuation_snapshot_tasks.py:82-84` (gated); RPA `market_valuation_snapshots_us.py:175,215-230` (08:30 Tue–Sat) | Naver/Toss/tvscreener/Finnhub/Yahoo → `market_valuation_snapshots` | DO UPDATE, `computed_at` overwritten | US Prefect: Completed; KR UNVERIFIED | Keep (serving) |
| 31–32 | Screener snapshots KR / US | `AT: app/tasks/invest_screener_snapshot_tasks.py:348-397` (gated); RPA `invest_screener_snapshots_us.py:152,188-202` (06:10 Tue–Sat) | → `invest_screener_snapshots` | DO UPDATE on `(market,symbol,snapshot_date)` | US Prefect: Completed | Keep (serving) |
| 33 | Support proximity | `AT: scripts/build_support_proximity_snapshot.py` | derived | in place | no schedule | Keep |
| 34 | Crypto screener | RPA `invest_crypto_screener_snapshots.py:149,173-184` (09:20) | Upbit/Binance/tvscreener → `invest_crypto_screener_snapshots` | DO UPDATE | Prefect: Completed 11-01 | Keep (serving) |
| 35 | Crypto insight | RPA `invest_crypto_insight_snapshots.py:221,247-259` (09:20) | alternative.me, CoinGecko, DefiLlama, Binance → `crypto_insight_snapshots` | keyed by `snapshot_at` (append-like) | Prefect: Completed 11-01 | Keep; closest to PIT of the serving jobs |
| 36 | KR fundamentals | RPA `invest_kr_fundamentals_snapshots.py:178,208-220` (18:00) | tvscreener/KRX → `invest_kr_fundamentals_snapshots` | DO UPDATE | Prefect: Completed 11-01 | Keep (serving) |
| 37 | US fundamentals | RPA `us_fundamentals_snapshots.py:162,196-209` (Sun 09:00) → `AT: scripts/build_us_fundamentals_snapshots.py` | yfinance/SEC → `financial_fundamentals_snapshots` | **restatements overwrite** (DO UPDATE on `(market,symbol,fiscal_period,source)`) though `source_collected_at`/`effective_at` exist | **Failed 11-01 09:00 (RuntimeError), 2 fails/7d** | **Fix** (failure) for serving; rebuild as a data-plane dataset before any fundamentals feature |
| 38 | KR financial fundamentals | `AT: scripts/build_financial_fundamentals_snapshots.py` (manual) | DART → same table | same | manual | Keep (manual) |
| 39–41 | Market events daily / DART 07:00 / DART 18:30 | RPA `market_events_ingestion.py:133,176-188` and ~`240-283` (code registers them paused; the inventory shows runs, so they were resumed) → `AT: scripts/ingest_market_events.py` | Finnhub earnings (−7..+60 d), DART → `market_events` | DO UPDATE on `source_event_id`/natural key; `fetched_at` and `status` overwritten (`AT: app/services/market_events/repository.py:43-90`) | daily: Completed 11-01; **dart-0700: 5 fails/7d**; dart-1830 ok | **Fix** dart-0700 for serving. **Rebuild next** as data-plane `events` (earnings dates matter to H-admit) |
| 42 | DART backfill / ForexFactory | `AT: scripts/backfill_dart_disclosures.py`; `app/flows/forexfactory_calendar_flow.py:36` | → `market_events` | same | manual / no deployment | Keep (manual) |
| 43 | Naver momentum events | `AT: app/tasks/invest_momentum_event_tasks.py:86-88` (`*/10 9-15`, gated) | Naver → `invest_momentum_event_snapshots` (+ theme tables) | keyed by `snapshot_at`; child stock rows deleted and reinserted | UNVERIFIED | Keep (serving) |
| 44 | Analyst consensus | `AT: app/tasks/analyst_consensus_snapshot_tasks.py:19` (no schedule) | Naver/yfinance → `analyst_consensus_snapshots` | DO UPDATE | no schedule | Keep |
| 45 | Market quote snapshots | `AT: app/tasks/market_quote_snapshot_tasks.py:19` (no schedule) | KIS/Upbit/Toss/Yahoo → `market_quote_snapshots` | keyed by `snapshot_at` | no schedule | Keep |
| 46 | Research reports | `AT: app/routers/research_reports.py` (token POST) | broker reports via NI → `research_reports` | insert-only by `dedup_key`; `received_at` = receipt | **who schedules the push is UNVERIFIED** | Keep (serving) |
| 47 | KRX after-market list | `AT: scripts/import_krx_after_market_eligibility.py` (manual) | operator file → `krx_after_market_eligibility` | replaces the whole table | manual | Keep (manual) |

### Research collectors (files under an artifact root; no DB, no scheduler)

| collector | path | PIT status | decision |
|---|---|---|---|
| crypto-universe-v1 (U4) | `AT: research/crypto_universe/` | snapshots append-only per UTC day; PIT table with listing episodes | **Keep; reused** (its snapshot sources and parsers feed `universe/crypto`) |
| us-universe-v1 (U3) | `AT: research/us_universe/` | append-only snapshots; PIT intervals as evidence bounds | **Keep; reused** (`parse_symbol_directory` feeds `universe/us`; write-once store helpers reused) |
| kr-delist-repair (U2) | `AT: research/kr_delist_repair/` | causal export with `available_at`, fail-closed joins | **Keep; reused** (`parse_kis_master` feeds `universe/kr`; delisting outcomes for KR) |
| kr-corpus-v1 | `AT: research/kr_corpus/` | immutable snapshot 2026-08-03 (pykrx) | Keep frozen (historical training set) |
| us-corpus-v1 | `AT: research/us_corpus/` | **survivorship-biased** (Yahoo, today's listings) | Keep frozen; never for market-wide claims (superseded for universes by U3) |
| crypto-corpus-v1 | `AT: research/crypto_corpus/` | survivor-only enumeration | Keep frozen (superseded for universes by U4) |
| us-intraday-corpus-v1 | `AT: research/us_intraday_corpus/` | Alpaca | Keep frozen |
| r4_p0 Binance PIT | `AT: scripts/r4_p0_collector.py` | SQLite with `received_at` | Keep (manual) |

### New collectors (this PR)

| collector | trigger | source → destination | PIT | decision |
|---|---|---|---|---|
| `universe` kr/us/crypto | `research.data_plane.cli collect` (manual; Dagu proposal) | KIS master, NASDAQ Trader, Binance exchangeInfo, Upbit market/all → `universe/<market>/date=P/` | write-once, `available_at` = observation | **Build (this PR)** |
| `bars_1d` kr/us/crypto | same | KR/US: KIS daily chart → Toss candles → Yahoo chart v8 fallback (ROB-1448; `served_by` in the manifest); crypto: Binance klines, Upbit day candles → `bars_1d/<market>/date=P/` | write-once; refuses forming bars and rows dated after `P` | **Build (this PR)** |
| `bars_1d_adjusted` kr/us | same | KIS → Toss → Yahoo (derived) adjusted bars of the last 20 sessions as published on `P` → `bars_1d_adjusted/<market>/date=P/` | write-once vintage; raw stays in `bars_1d` | **Build (ROB-1448)** |
| `corporate_actions` kr/us/crypto | same | Yahoo dividends/splits (KR/US) + listing changes from the universe diff (all) → `corporate_actions/<market>/date=P/` | first-observed semantics | **Build (this PR)** |

### Not collectors (for completeness)

KR live session kickoff (starts Claude sessions), KIS live reconcile (48 failed
runs / 7 d per the inventory; `apply=False` on RPA `origin/main`), Toss fill
poll, `execution_ledger` reconcile, `research_run_refresh`, `watch_scan`, and the
`pyri-process` monitors (runtime monitor, freshness monitor, error watchdog,
MacBook server-mode monitor — all dead since 10-30 with the pool's worker).

## News ingestion: why retire, and what a PIT rebuild would need

**Retire the current path now.** It has not produced data since before 10-30
(100% failure, then no worker), no registered hypothesis (ROB-1411,
H-entry-price, H-admit) uses a news feature, and re-enabling it as it is would
accumulate rows that cannot be used for training:

* the time stored with an article is when auto_trader *received the push*, not
  when the crawler first saw it; NI's `first_seen_at` is dropped on the wire;
* the two write paths store naive timestamps in different zones (KST vs UTC)
  in the same columns;
* `article_published_at` is publisher-claimed and nullable;
* relevance judgments overwrite each other with no history.

**What a PIT-safe rebuild would take** (only when a registered hypothesis asks
for a news feature):

1. carry NI `first_seen_at` (tz-aware UTC) end to end and make it the article's
   `available_at`; store `received_at` separately;
2. one time zone (UTC, offset-aware) on every path, including `get_news`;
3. append-only article vintages keyed by `(url, content_hash)` instead of
   in-place `raw_json` updates;
4. relevance judgments as append-only rows `(article, symbol, judge_version,
   judged_at, verdict)`; a feature reads the latest judgment with
   `judged_at <= decision_time`;
5. land them as data-plane partitions (`news/<market>/date=P/`) with a manifest
   and the same health check, run on a worker that has a database connection
   that actually works (the 10-30 failures were `connection is bad`).

## Advisories for the operator (no action taken)

* RPA news deployments (7) and the `pyri-process` pool: delete or keep paused;
  10 stale offline workers are still registered (inventory).
* US fundamentals failed 11-01; DART 07:00 failed 5×/7 d — serving fixes, separate issues.
* KR investor flow may be triggered twice (TaskIQ gated label + Prefect) — check the flag.
* `crypto_candles_1m` ingest has no entrypoint in this repo — confirm owner or retire.
