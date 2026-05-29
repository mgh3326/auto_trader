# ROB-367 — Event-driven equity data feasibility audit (KR + US)

Research / feasibility only. Read-only inspection of repo code, alembic migrations, and a
single read-only local-DB probe (SELECT/GROUP BY/`to_regclass` only). **No strategy, no
backtest, no parameter sweep, no broker/order/watch/order-intent/approval/trade-journal
mutation, no scheduler/TaskIQ/Prefect/cron activation, no production DB write/backfill/delete,
no raw market/event data committed, no secrets printed, no LLM/news-summary treated as an
authoritative event timestamp, and no reopening of the Binance/crypto ROB-362 line.**

This audit decides whether auto_trader has enough point-in-time (PIT), timestamped, and
tradability-aware **equity** event data to justify opening a *separate, bounded*
event-response research/backtest issue. It does **not** look for alpha and does **not**
implement a strategy.

**Verdict (overall): `partial`.** US-earnings is the only family that is event-feasible
*today* at daily granularity (gated on operator price-backfill + a missing session calendar +
an unmeasured join); KR-DART-disclosure is the highest-value KR follow-up but is blocked on a
KR price-store engineering gap; KR-earnings is `needs_more_data`; macro-calendar and
news-clusters are `not_feasible` as per-equity event sources. **Do not open a backtest issue
yet** — open a narrow US-earnings event+price *data-builder/coverage* issue first.

---

## Why this issue exists (context)

- The short-horizon Binance USD-M crypto strategy search is **closed on evidence**
  (ROB-316/320/324/339/342/351/353). ROB-360/**362**'s funding+OI gross-triage surfaced a
  headline `fade` edge that falsification (listing-seasoning + cohort + direction-consistency
  controls) proved to be a **new-listing / survivorship microstructure artifact** (the
  profitable sign flipped after seasoning). **No crypto backtest issue is opened from that
  line, and this audit does not reopen it.**
- The next strategy-research card is deliberately **orthogonal to crypto microstructure**:
  event-driven **equities**.
- **ROB-128** (market-events ingestion foundation) and **ROB-208** (rolling scheduler /
  calendar coverage cleanup) already built the *ingestion / product-readiness* layer. This
  issue is **not** a duplicate of that work — it is the **strategy-research feasibility audit**
  that should run *before* any event-response study. The ROB-362 survivorship lesson is the
  governing prior: a dense, skew-prone panel can manufacture spurious edge, so this audit
  fail-closes on lookahead / survivorship / date-only-as-intraday traps.

## Method & reproducibility

- **Inspection**: read-only Read/grep over `app/services/market_events/`,
  `app/services/daily_candles/`, `app/models/`, `alembic/versions/`, `app/tasks/`,
  `app/routers/`, `app/services/research_reports/`, `app/services/invest_view_model/`,
  and the corresponding tests. Branch `rob-367` @ `727d84e3` (== `origin/main`).
- **Probe** (RUN `2026-05-30`): one read-only SQLAlchemy `engine.connect()` (not `begin()`)
  issuing only `SELECT count(*)` / `GROUP BY` / `FILTER` / `to_regclass()` against
  `market_events`, `market_event_values`, `market_quote_snapshots`, `crypto_candles_*`. Engine
  disposed in `finally`; throwaway script deleted; `DATABASE_URL` value never printed
  (password stripped). **The probe hit a populated local/dev instance — counts are local
  ground truth; prod parity is `확인 불가`.** Code-level facts below are repo-proven independent
  of the DB.
- A 25-agent fan-out (9 source readers → PIT-matrix / price-join / probe → per-family draft
  verdict → adversarial falsification) produced and cross-checked every load-bearing claim;
  each verdict survived an adversarial pass whose explicit mandate was to refute optimism and
  downgrade any unmeasured "feasible."

---

## 1. Source inventory (cited)

### Event layer — `market_events` (ROB-128 foundation), all present
- **Normalizers** `app/services/market_events/normalizers.py` — five pure functions, each
  returning `(event_dict, [value_dict, ...])`:
  - `normalize_finnhub_earnings_row` (US earnings; eps/revenue actual+estimate; BMO/AMC hint
    via `_FINNHUB_HOUR_TO_TIME_HINT` lines 13–18).
  - `normalize_dart_disclosure_row` (KR disclosure; `rcept_no`/`rcept_dt`/`report_nm`;
    `classify_dart_category` keyword map lines 107–128).
  - `normalize_wisefn_earnings_row` (KR earnings *schedule*; deterministic `source_event_id`).
  - `normalize_forexfactory_event_row`, `normalize_tradingview_event_row` (macro/economic).
- **Model** `app/models/market_events.py` — `MarketEvent` (`event_date` DATE,
  `release_time_utc`/`release_time_local` TIMESTAMP, `source_timezone`, `time_hint`, `status`,
  `fetched_at`), `MarketEventValue` (`released_at`, `surprise`/`surprise_pct`),
  `MarketEventIngestionPartition` (per-day ingest state). Two partial unique indexes:
  `uq_market_events_source_event_id` and `uq_market_events_natural_key`.
- **Repository** `app/services/market_events/repository.py` (all writes; upsert lines 32–151).
  **Read** `query_service.py`, `freshness_service.py`, `prioritization.py`.
- **DART** `app/services/market_events/dart_helpers.py` (`OpenDartReader.list_date`).
  **Earnings** `finnhub_helpers.py`, `wisefn_helpers.py`. **Macro** `forexfactory_helpers.py`,
  `tradingview_helpers.py`, `app/services/external/{economic_calendar,forexfactory_calendar}.py`.
- **Ingest CLI** `scripts/ingest_market_events.py` (`--source finnhub|dart … --from-date
  --to-date`, no upper bound). **Router** `app/routers/market_events.py` (GET today/range).
- **Session/holiday calendar** `app/services/market_events/expected_sources.py:11–14` — only
  Sat/Sun is modeled; *"We do not yet model KRX/NYSE observed holidays"* (a follow-up).

### Price layer — equity daily candles **DO exist** (corrects an earlier mis-scout)
The equity OHLCV store is **not** in `app/models/` (only `crypto_candles.py` +
`market_quote_snapshot.py` live there); it is a TimescaleDB hypertable accessed via raw SQL in
`app/services/daily_candles/repository.py`, which is why a naive `app/models` grep misses it:
- **`us_candles_1d`** — `alembic/versions/142b01f2eba0_add_us_candles_1d.py` (`adj_close
  NUMERIC` **nullable**, line 67; exchanges NASD/NYSE/AMEX), hypertable + retention policy
  (`e7a5b7c9d1f2`, `f8b6c4d2e1a3`).
- **`kr_candles_1d`** — `alembic/versions/bad6e17e4115_add_kr_candles_1d.py` (**no `adj_close`
  column**; venue KRX/NTX), hypertable (`87541fdbc954`) + retention policy (`d31f0a2b4c6d`).
- **Service** `app/services/daily_candles/sync_service.py`: `_sync_us` (lines 96–145) tries KIS
  then **Yahoo fallback** on empty rows; `_sync_kr` (lines 87–94) is **KIS-only, no fallback**.
  `kis_daily_fetcher.py` / `yahoo_us_fallback.py` / `constants.py` (`*_BACKFILL_BARS`=400 CLI
  knob, not a vendor cap). **Backfill CLI** `scripts/backfill_daily_candles.py` exposes
  `--market/--symbols/--horizon-bars/--partition/--dry-run` — **no `--end-date`**.
- **TaskIQ crons exist** (static fact, *not* activated here): `app/tasks/us_candles_tasks.py`,
  `kr_candles_tasks.py`, `daily_candles_tasks.py` — incremental, forward-only, universe-gated
  (~10-bar). They do **not** backfill historical event windows.
- **`market_quote_snapshots`** (`app/models/market_quote_snapshot.py`) is a **forward-only**
  PIT snapshot (unique on market/symbol/source/`snapshot_at`) — it cannot reconstruct a *past*
  window.

### Downstream consumers (verified read-only, not label sources)
`app/routers/investment_reports.py`, `app/services/investment_reports/query_service.py`,
`app/services/action_report/snapshot_backed/collectors/market.py`,
`app/services/invest_view_model/{screener_service,calendar_service}.py` — all consume events as
**display/context only**, after report generation; no event→label mapping; no in-process LLM
(ROB-287 boundary). Using them as event labels would be lookahead/leakage and is out of scope.

### News / research-reports (event-timestamp quality)
`app/models/news.py` (`article_published_at` nullable/heuristic line 93–95; `scraped_at` =
crawler-arrival line 96–98; `NewsAnalysisResult.created_at` = LLM time, line 278),
`news_entity_matcher.py` (heuristic alias matching), `app/models/research_reports.py` +
`app/schemas/research_reports.py` (publisher `published_at`, but copyright-gated to
summary≤1000 / excerpt≤500; full-text rejected lines 129–139; first-seen `dedup_key` upsert
= corrections never re-ingested).

---

## 2. PIT / known-after semantics (per source)

| Source (family / market) | Event ts field | ts kind | TZ | Known-after rule | Revision-safe |
|---|---|---|---|---|---|
| **Finnhub** (earnings / US) | `event_date` only; `release_time_utc` **hardcoded NULL** (`normalizers.py:62`) | **date-only** + BMO/AMC `time_hint` | `America/New_York` | `event_date` + `time_hint`: BMO→next-open, AMC→next-close, unknown→whole-day uncertainty | **No** — estimates revised pre-event; **no historical forecast snapshot** kept (in-place upsert) |
| **DART** (disclosure / KR) | `event_date` from `rcept_dt`; `release_time_utc` NULL (`:165`); `time_hint` **'unknown'** (`:168`) | **date-only** (FSS *receipt* date, not announcement) | `Asia/Seoul` | filing-date only; earliest clean tradable point = **D+1 session** (잠정실적 often post-close) | **Yes** — `rcept_no` immutable, metadata fixed at filing |
| **WiseFn** (earnings / KR) | `event_date`; `release_time_utc` NULL (`:363`) | **date-only** (schedule) | `Asia/Seoul` | date + `time_hint`; **feature-gated OFF** (`config.py:321`) | Low risk; **schedule only — emits empty values list** (`:376`) |
| **ForexFactory** (macro / global) | `release_time_utc` **populated** (ET→UTC) | **release-time** | `UTC` | true intraday moment | Moderate — live scrape, in-place upsert, no version history |
| **TradingView** (macro / global) | `release_time_utc = date_utc` (`:431`) | **release-time** | `UTC` | true intraday moment | Moderate — same as FF |
| **News / research** | `article_published_at` nullable / `published_at` publisher-supplied, unverified | **mixed / collection-time** | source TZ often lost (`to_kst_naive`) | **no authoritative tradable timestamp** | **No** — corrections never re-ingested |

**Decisive fact:** every *equity-linkable* source (Finnhub, DART, WiseFn) is **100% date-only**
— the probe measured **0/6357 US and 0/201 KR** equity events with a non-null `release_time_utc`.
Only the **global macro** sources (FF/TV) carry a true release moment, and those have **no
equity-symbol linkage**. There is **no derived `known_after_ts_utc` column**; it must be
computed at query time from (`time_hint`, `source_timezone`, a session calendar) — and the
session/holiday calendar **does not exist** (`expected_sources.py:11–14`). Treating a date-only
event as intraday-known-before is therefore forbidden; the earliest lookahead-safe decision
point is **next session, bucketed by BMO/AMC (US) or D+1 (KR)**.

---

## 3. Event normalization & dedupe (spec, already largely satisfied)

A normalized event table is **producible today** from `market_events` for the deterministic
families, with one gap (derived `known_after_ts_utc`):

- `event_id` / deterministic key: **yes** — `source_event_id` when present (DART `rcept_no`;
  TV UUID; FF/WiseFn deterministic strings), else COALESCE natural key
  `(source, category, market, symbol, event_date, fiscal_year, fiscal_quarter)`. Both enforced
  by partial unique indexes → **idempotent re-ingest** (`repository.py:32–114`).
- `symbol`, `market`, `event_family`(=category), `event_type`: **yes** (DART `symbol` is
  `stock_code` only when 6-digit numeric, else NULL — filter `symbol IS NOT NULL`).
- `event_ts_utc`: **only `event_date` (date)** for equities; `release_time_utc` for macro only.
- `known_after_ts_utc`: **must be derived** (no column) — **blocked on the missing session
  calendar**; until then, label with `time_hint` + an explicit day-granularity boundary.
- `source`, `source_url/id`: **yes** (e.g. DART `dart.fss.or.kr/...rcpNo={rcept_no}`).
- Duplicate grouping: **handled** by the two unique indexes; values upserted by
  `(event_id, metric_name, period)`.
- Confidence/quality flags vs silent drops: `status` (scheduled/released/revised) + ingestion
  partition state; **no silent drops** (rows written unless explicit exception). **Finnhub** is
  the one collision risk (no stable ID → natural-key assumes symbol stability).

---

## 4. Price-reaction window join feasibility (the binding constraint)

The gating constraint is the **equity price-reaction join, not the event side.** No intraday
equity bars are stored (US or KR), so even the US "decision-day" reaction is observable only at
**daily-close** granularity. `market_events` is **forward-accumulating** (no bulk historical
event backfill *except* DART's `list_date`), so realized sample-N depends on how long ingestion
has run, not on an archive.

| Family / market | Price source | Hist. depth | PIT reconstructable | Windows (-5..-1 / decision / +1+3+5+20) | Tradability | Benchmark | Sample N |
|---|---|---|---|---|---|---|---|
| **US-earnings** | `us_candles_1d` + **Yahoo fallback** (`adj_close` present) | **deep / recoverable** on-demand | **yes** (daily) | pre/post **yes**; decision = **date-only, BMO/AMC bucket only** | volume present (ADV computable); **no halt/ADV field**; survivorship controllable via `us_symbol_universe.is_active`/`is_common_stock` (but **delisted-bar recoverability via Yahoo = `확인 불가`**) | SPY/sector ETFs queryable as plain symbols; **no pre-stored index/sector table** | `확인 불가` — event-side gated; local DB us=6357 rows but range **2026-05-01..07-28** (mostly *future-dated* vs today) and **0 materialized candle rows** locally |
| **KR-earnings** | `kr_candles_1d` (**KIS-only, no Yahoo, no `adj_close`**) | shallow / today-anchored | **partial** | bounded; **no realized eps/revenue at all** (WiseFn empty + gated OFF; DART quarterly-actual scrape unimplemented) | weak: no adj_close (no split/div), no halt/ADV | **missing** (no KOSPI/KOSDAQ OHLCV) | `확인 불가`; realized-actual N ≈ **0 by construction** |
| **KR-DART-disclosure** | same KR path | **event deep / price shallow** | **partial** | event window backfillable (`list_date` arbitrary past, immutable `rcept_no`); **price window today-anchored** (`_sync_kr` never threads `end_date`; CLI has no `--end-date`) | weak (same KR path) | missing | `확인 불가`; DART rows = **0** locally |
| **macro-calendar** | N/A per-symbol (`market='global'`, `symbol=None`) | shallow (FF ~14d rolling; TV day-by-day) | **partial** (regime only) | **no per-stock window** — no (country/currency/title)→equity mapping anywhere | N/A | event *is* the regime signal; US proxy ok, **KR proxy missing** | equity (kr/us) macro rows = **measured 0** |
| **news-clusters** | N/A (timestamp authority too weak) | weak | **no** | timestamp + entity gate fails upstream of any price join | N/A | N/A | out-of-scope |

**Overall bottleneck:** (1) no intraday equity bars → daily-close granularity only; (2)
equity events 100% date-only → earliest decision = next session by BMO/AMC; (3) price stores +
`market_quote_snapshots` are forward-accumulating (snapshots cannot reconstruct the past); (4)
no NYSE/KRX session calendar; secondary: no halt/ADV fields, no pre-stored index/sector OHLCV
(KOSPI/KOSDAQ absent).

**Probe counts (local dev, read-only, `2026-05-30`; prod parity `확인 불가`):**
`market_events` total **10521** — finnhub/earnings/us **6357** (5024 distinct symbols, range
2026-05-01..07-28), wisefn/earnings/kr **201**, tradingview/economic/global **3848**,
forexfactory/economic/global **115**, **dart = 0**. `market_event_values` 12419;
events_with_symbol 9376; **events with `release_time_utc` over all kr/us equity = 0**.
Price side: `market_quote_snapshots` total **20** (all KR; US=0); equity daily candles
materialized locally ≈ **0**. → **No measured event+price join coverage exists today**, which is
exactly why no family can be `feasible`.

---

## 5. Coverage / readiness thresholds (deterministic, for the next issue's gate)

A future event-response study may proceed **only if** an operator-run, read-only coverage probe
meets all of:

- **min events / family / market**: ≥ 500 *realized* (`status=released`, `event_date ≤ today`)
  events for the target family.
- **min joinable symbols**: ≥ 200 distinct symbols with both event rows and a materialized
  `-5..+20d` daily-bar window around the event, **join coverage ≥ 90%** of selected events.
- **max date-only / unknown-time ratio**: documented and **≤ 100%** is *accepted* for equities
  **only** if labeling is restricted to next-session/BMO-AMC buckets (intraday is forbidden).
- **max duplicate / ambiguous-mapping ratio**: ≤ 1% (unique indexes already enforce this;
  measure NULL-symbol DART ratio explicitly).
- **min tradability coverage**: ≥ 90% of joined symbols have `volume`>0 history; **survivorship
  policy stated** (include/exclude delisted via `is_active`) and delisted-bar recoverability
  measured, not assumed.
- **benchmark availability**: a benchmark symbol set (US: SPY/sector ETFs; KR: blocked) joined
  for ≥ 90% of events, else relative-strength is out of scope.
- **a fail-closed NYSE/KRX session+holiday calendar exists** before any known-after labeling.

---

## 6. Verdict table (post-adversarial; every verdict held)

| Event family | Market | Verdict | One-line basis |
|---|---|---|---|
| **Earnings** | **US** | **`partial`** | Event + price *capability* both exist (Yahoo-recoverable `adj_close` daily history, survivorship-controllable), but **date-only timing → daily granularity only**, **no session calendar**, **no historical forecast snapshot**, and **join coverage is unmeasured/0 today**. "feasible" is forbidden without measured coverage. |
| **Earnings** | **KR** | **`needs_more_data`** | **Realized eps/revenue = 0 by construction** (WiseFn empty + gated OFF; DART quarterly scrape unimplemented) **and** KR price not PIT-reconstructable (KIS-only, today-anchored, no `adj_close`). Gaps are in-repo *engineering* over already-integrated free vendors → not `needs_vendor_data`. |
| **DART disclosure** | **KR** | **`partial`** | **Strongest, most-backfillable event layer in the repo** (`list_date` arbitrary past, immutable `rcept_no`, deterministic classification) — but **blocked on the KR price store** (no `end_date` threading, no `adj_close`, no index benchmark). A path exists → not `not_feasible`; no missing vendor → not `needs_vendor_data`. |
| **Macro / calendar** | global | **`not_feasible`** (per-equity) | **By-construction `symbol=None`, `market='global'`**; no (country/currency/title)→equity mapping; measured equity-market macro rows = **0**. Usable only as US **regime context** (KR has no index proxy), explicitly outside the per-stock mandate. |
| **News / issue clusters** | KR/US | **`not_feasible`** (as event source) | No source-verified publisher timestamp (`article_published_at` nullable; `scraped_at`=arrival; LLM time ≠ event time), heuristic entity linkage, corrections never re-ingested, copyright-gated bodies. Suitable for sentiment/citation only — **never** as a causality anchor (ROB-367 hard boundary). |

---

## 7. KR vs US recommendation — **split; do US-earnings first**

**US is materially more feasible today; KR is the long pole.** US has `us_candles_1d` with
`adj_close` (split/div) and a **Yahoo fallback** that recovers deep daily history on demand
(`--horizon-bars` past the 400 default), plus `is_active`/`is_common_stock` to control
survivorship — so a `-5..+20d` window around a *past* US earnings event is reconstructable
without lookahead at daily granularity (watch survivorship + Yahoo raw-vs-adjusted).

KR has structural deficits **no single PR fixes**: KIS-only with **no Yahoo fallback**, **no
`adj_close`** (cannot split-adjust), a **today-anchored** backfill (the unused `end_date` exists
in `kis_daily_fetcher.py` but is never threaded through `_sync_kr`/the CLI), bounded KIS depth,
**no KOSPI/KOSDAQ index OHLCV** for benchmarking, no halt/ADV metadata, and **no realized KR
earnings actuals**. KR-DART-disclosure is the *highest-value KR follow-up* (its event layer is
the best in the repo) but it is gated on a **KR price-store engineering project**, not on this
audit.

---

## 8. Final recommendation — **do NOT open a backtest issue; open a US data-builder issue**

The overall verdict is **`partial`**, so per the issue's own decision rule the next card is a
**narrower data-builder / coverage issue, not a backtest**:

1. **(recommended, US first)** Open a **US-earnings event+price coverage builder** issue:
   operator-run read-only coverage probe against the §5 thresholds; materialize the event→daily
   `-5..+20d` join for `status=released` US earnings; **build the fail-closed NYSE session+holiday
   calendar** (the `expected_sources.py` follow-up); enumerate a benchmark symbol set; record a
   survivorship/delisted-recoverability policy. Only if that probe **passes §5** does a bounded
   US event-response backtest issue become warranted.
2. **(KR, deferred)** A separate **KR equity price-store** engineering issue (thread `end_date`
   through `DailyCandleSyncService.sync_one` + the backfill CLI for PIT windows; add a KR
   `adj_close`/corporate-action source; stand up a KOSPI/KOSDAQ index OHLCV table) is the
   precondition before DART-disclosure becomes a viable study. **Do not** start KR event-response
   research before that lands.
3. **Stop / defer**: KR-earnings actuals, macro-as-per-equity, and news-as-event-source — no
   action without new data sourcing; macro retained only as optional US regime context.

This keeps the program honest: the event metadata is in good shape, but **measured price-join
coverage is the unmet gate**, and the ROB-362 survivorship lesson is carried forward as an
explicit §5 threshold rather than discovered after the fact.

---

## Boundaries honored (acceptance-criteria evidence)

- ✅ Existing repo/service/data paths inspected and cited (§1, file:line throughout).
- ✅ Prior work acknowledged — ROB-128/208 (ingestion foundation, *not* duplicated), ROB-360/362
  (crypto closed as artifact, *not* reopened).
- ✅ Families classified `feasible|partial|needs_more_data|needs_vendor_data|not_feasible` (§6).
- ✅ PIT / known-after semantics documented per source (§2).
- ✅ Normalization / dedupe requirements specified (§3).
- ✅ Price-reaction join feasibility + sample counts **measured (local) or labeled `확인 불가`**
  (§4); prod parity explicitly `확인 불가`.
- ✅ KR vs US recommendation explicit — **split, US-earnings first** (§7).
- ✅ Final recommendation stated — **no backtest issue yet; US data-builder issue first** (§8).
- ✅ Safety: read-only only — **no broker/order/watch/order-intent/approval/trade-journal
  mutation, no scheduler/TaskIQ/Prefect/cron activation** (cron decorators only statically
  inspected, not invoked), **no prod DB write/backfill/delete** (probe was SELECT-only, engine
  disposed), **no secrets printed**, **no raw market/event data committed**, **no LLM/news used
  as authoritative event timestamp**, **Binance/crypto ROB-362 line untouched**.
