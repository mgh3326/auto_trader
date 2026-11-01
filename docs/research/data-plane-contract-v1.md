# Data contract v1 — point-in-time inputs for U7/U8 (ROB-1443)

The boosting baseline (U7) and the TFT challenger (U8) take the same inputs for
KR, US and crypto. This contract fixes what those inputs look like on disk, when
each row became knowable, and how a missing or stale day is detected. Code:
`research/data_plane/` (commands in its `README.md`). Collector inventory and
keep/fix/rebuild/retire decisions: `data-plane-collector-inventory-2026-11-01.md`.
Broker market-data sources, their limits and the research budget (ROB-1448):
`data-plane-broker-sources-inventory-2026-11-01.md`.

Datasets: `universe`, `bars_1d` (raw), `bars_1d_adjusted` (KR/US only),
`corporate_actions`.

## The PIT rule

Every row has `available_at`. A consumer joins a row only when
`available_at <= decision_time`. Partitions make that cheap: **partition
`date=P` holds exactly the rows whose `available_at` falls on the local day `P`**
(market time zone below), so the partitions dated `<= D` never hold anything that
was unknown on day `D`. The writer refuses (exit code 3, nothing written):

* a row whose `available_at` is **later** than its partition day;
* a row whose `available_at` is earlier (it belongs to an earlier partition);
* a row observed before it was available (`observed_at < available_at`: a
  forming intraday bar or a skewed clock);
* a row without a UTC offset on any timestamp, a duplicate row key, or a
  `content_hash` that does not match the row.

## Common fields (every dataset, every market)

| field | meaning |
|---|---|
| `instrument` | DB symbol format: KR 6-char code; US dot format (`BRK.B`, via `app.core.symbol`); crypto venue pair (`BTCUSDT`, `KRW-BTC`) |
| `venue` | `KOSPI`/`KOSDAQ`; `NASDAQ`/`NYSE`/`NYSE American`/`NYSE Arca`/`Cboe BZX`/`IEX`; `binance_usdt_spot`/`upbit_krw` |
| `event_time` | when the fact happened (bar close, dividend ex-date open, listing observation) |
| `available_at` | earliest instant a decision process could know the row (rules per dataset below) |
| `observed_at` | when the collector read it; `observed_at − available_at` is the observation lag |
| `source` / `source_version` | e.g. `kis_daily_chart`/`kr-FHKST03010100/us-HHDFS76240000`, `toss_candles_1d`/`api-v1`, `yahoo_chart`/`v8`, `binance_klines`/`api-v3`, `upbit_candles_days`/`v1`, `kis_master`/`v1`, `nasdaqtrader_symdir`/`v1`, `exchange_listing`/`v1`, `universe_diff`/`v1` |
| `content_hash` | sha256 of the canonical JSON row without `observed_at`/`content_hash`; the same fact observed twice hashes the same |

All timestamps are ISO-8601 with an explicit offset (written in UTC). Prices are
exact decimal text as the source sent them (Yahoo sends float32-rounded values
such as `346.7200012207031`; they are kept, not rounded).

## Storage

```
<root>/<dataset>/<market>/date=YYYY-MM-DD/rows.jsonl.gz   write-once (os.link)
<root>/<dataset>/<market>/manifest.jsonl                  append-only, one record per partition
```

* Bytes are deterministic (sorted canonical lines, gzip mtime 0).
* A manifest record carries the file sha256, row count, `observed_at` range,
  code commit, the universe partitions (date + sha256) the rows were selected
  from, `instruments_requested`/`instruments_missing`/`coverage`, and the
  missing instruments with a reason (capped at 500). KR/US bar partitions also
  carry `vintage`, `served_by` (the source that served the whole partition),
  `source_order`, `source_attempts` (every source tried, with `served`,
  `unavailable` + detail, or `coverage_below_floor` + coverage and reason
  counts), `adjustment_basis` and `research_budget`.
* Each partition directory also holds a write-once `meta-<sha256>.json`
  sidecar with the run metadata, published before the rows, so a crash between
  the rows and the manifest append is repaired with the same coverage and
  selection metadata; an unrecorded partition without its sidecar, or one that
  is not readable, is refused rather than recorded.
* A partition is written once. A rerun of the same day fetches nothing and
  writes nothing — after checking the published file still matches its
  manifest sha256 (a corrupted or replaced file fails the run). One dataset failing does not stop the others (corporate
  actions can only be observed on their own day). A crash between publish and manifest append is repaired by the
  next run from the on-disk bytes.
* A partition with an empty row list is valid (for example no new corporate
  action that day) and still counts as present.
* The root lives outside git (for example `~/work/herdr-artifacts/data-plane-v1`).

## Per market

| | KR | US | crypto |
|---|---|---|---|
| local day / time zone | Asia/Seoul | America/New_York | UTC |
| partition days | XKRX sessions (exchange_calendars) | XNYS sessions (early closes included) | every UTC day |
| bar `event_time` | session close (15:30 KST; 16:30 on CSAT days, `calendars.KRX_LATE_CLOSE_DAYS`) | session close (16:00 ET; 13:00 on early-close days) | candle close, `P` 00:00 UTC |
| bar `available_at` | close + 30 min | close + 30 min | `P` 00:05 UTC |
| bar in partition `P` | session `P` | session `P` | **the candle of UTC day `P−1`** (`bar_date = P−1`) |
| bar source | KIS daily chart → Toss `candles` 1d → Yahoo chart v8 (`<code>.KS`/`.KQ`) | KIS overseas daily → Toss `candles` 1d → Yahoo chart v8 | Binance `api/v3/klines` (data-api.binance.vision), Upbit `candles/days` |
| universe source | KIS stock master zips (KOSPI, KOSDAQ) — the `kr_symbol_universe` source, parsed by U2 | NASDAQ Trader `nasdaqlisted`/`otherlisted` — parsed by U3 | Binance exchangeInfo (USDT pairs, every status) + Upbit market/all (KRW) — U4's snapshot sources |
| instruments that get bars/actions | every universe row | universe rows that are likely common stock **or** ETF (preferreds, rights, warrants, units have no Yahoo chart: 7/120 random universe rows missing on 2026-11-01, all of those kinds; 150/150 eligible rows present) | `TRADING` Binance pairs + every listed Upbit KRW market |
| corporate actions | Yahoo dividends + splits; listing changes | Yahoo dividends + splits; listing changes | listing changes (no dividends/splits) |
| delisting outcomes (U2) | `research/kr_delist_repair` export `delist_resolution.v1.csv`, keyed by symbol, with its own `available_at` (disclosure receipt time or delist date 00:00 KST) | U3 PIT intervals: listed until somewhere in `(listed_to, delisted_by]` — no terminal outcome source | U4 PIT table: last trade day, `last_trade_censored` — no terminal outcome source |

### universe

One row per listed instrument as observed on day `P`. `available_at =
event_time = observed_at` (a listing snapshot has no earlier provable time).
Fields: `listing_status` (`listed`; Binance raw status such as `TRADING`/`BREAK`),
`tradeable`, plus source attributes (`name`, `etf`, `likely_common_stock`,
`source_file_created_at`, `base_asset`, `warning`). **Can only be collected on
its own day**: a run for an earlier `P` is refused before any fetch. A
snapshot in which a venue lost more than 10% (and more than 5) of its
instruments since the previous universe partition is treated as a truncated
source and refused (`--max-universe-drop` widens it after an operator has
checked a genuine mass delisting); otherwise one bad body would turn every
omitted instrument into a `listing_removed` and back.

Bars and actions for day `P` select instruments from the **latest two universe
partitions dated `<= P`** — never a later universe (survivorship), and two so
that an instrument dropped from today's listing still gets its last bar. A bar
backfill for a day before the first universe partition is refused.

### bars_1d (raw vintage)

`bar_date`, `open`, `high`, `low`, `close`, `volume`, `currency`, `price_basis =
raw` (crypto: `as_observed`), `adjustment_basis` (KR/US; for example `kis
FID_ORG_ADJ_PRC=1 (unadjusted, as traded)`), plus `adj_close` when Yahoo served
the partition and `quote_volume`, `trades` for crypto. One row per instrument:
the bar of session `P` as traded.

**Sources and fallback (KR/US).** The whole partition comes from the first
source in `--bar-sources` (default `kis,toss,yahoo`) whose coverage reaches the
floor; a source that is not configured, cannot build its client, fails 25
instruments in a row before serving any, or covers too little is recorded in
`source_attempts` and the next one is tried. A source is stopped as soon as the
floor is out of reach (`instruments_tried` records where), so a failing source
does not spend its budget on the rest of the universe. A partition never mixes sources.
Broker calls are read-only daily charts (see the broker inventory) and take a
research budget slot each.

**A bar backfilled later is the source's view at observation time**: Yahoo's
`close` is split-adjusted retroactively, so for backfilled rows check
`observed_at − available_at` and apply the `corporate_actions` ledger, never the
source's `adj_close`, when building a training series.

### bars_1d_adjusted (adjusted vintage, KR/US)

The adjusted bars of the last `--adjusted-window` sessions (default 20) ending at
`P`, as the source publishes them on `P`: same bar fields plus `price_basis =
adjusted`, `adjustment_basis` and `vintage_date = P`. **Collectable only on its
own day** (like the universe): read later, the source's adjustment would carry
events announced after `P` under `P`'s `available_at`, so a run for an earlier
`P` is refused (exit 3). Raw `bars_1d` can still be backfilled. Every row of the
partition has `available_at = bar_available_at(P)` whatever its `bar_date` (an
adjusted value is known only once the adjustment is), and `event_time` = that
bar's session close. Raw and adjusted are separate datasets with separate
partitions and manifests, so a raw series is never overwritten by an adjusted
one. A split or dividend shows up as changed values for the overlapping
`bar_date`s of two consecutive vintages, which makes the adjustment history
itself point-in-time. Basis per source: KIS `FID_ORG_ADJ_PRC=0` (KR) / `MODP=1`
(US); Toss `adjusted=true`; Yahoo OHLC × `adjclose/close` (derived, marked
`(derived)`). Crypto has no adjusted vintage.

### corporate_actions

* KR/US Yahoo events: `action_type` `cash_dividend` (`cash_amount`, `currency`)
  or `split` (`numerator`, `denominator`), `ex_date`, `event_time` = ex-date
  open. Requested with a 60-day lookback, extended back to 7 days before the
  last recorded actions partition after an outage (so a missed period is
  recovered, first observed on the day it is finally read); events with
  `ex_date > P` are not recorded.
* Listing changes (all markets), from the universe diff of `P` against the
  previous universe partition: `listing_added`, `listing_removed`,
  `listing_status_changed` with `status_before`/`status_after`. The first
  universe partition is a baseline (no rows, `listing_baseline: true`). The
  diff reference is the universe the latest earlier actions partition used, so
  a day whose actions failed is folded into the next successful diff (its
  changes become available on that later day) instead of being lost. If that
  universe record is missing, the reference is the latest universe on or
  before that actions day (never the first universe, which would re-emit
  changes already recorded).
* **First-observed semantics**: `available_at` is when this collector first saw
  the fact (the sources carry no announcement time). Rows are stamped after
  every source read of the run has finished, never before the read that
  revealed them; listing rows keep the universe observation time as
  `event_time`. Each row has `fact_hash`
  (identity without observation time); a fact already present in an earlier
  partition is not written again, and a revised amount appears as a new fact.
  Collection, like the universe, is only possible on its own day.

## Freshness SLO and gap rule

A partition day `d` is **due** at `bar_available_at(d) + grace` (default grace
3 h: KR 19:00 KST, US 19:30 ET, crypto 03:05 UTC; the Dagu proposal tightens it
to 1 h / 1 h / 6 min so the run day is already due when the DAG starts, in both
summer and winter time, and the health step right after collection judges the
same day). `research.data_plane.cli health` fails (exit 1) on:

| kind | rule |
|---|---|
| `gap` | a due day inside the lookback window (default 14 days, never before the first partition) is missing while a later day exists |
| `stale` | the newest due day is missing — the dataset stopped advancing |
| `corrupt` | a recorded partition's file is missing or its sha256 differs from the manifest |
| `low_coverage` | a partition's manifest coverage is below the floor (default 0.95) |
| `subset_partition` | a partition was written with `--symbols` (final for that day, so never healthy) |
| `repaired_unverified` | a bars/actions manifest record rebuilt after a crash, without its coverage metadata |
| `empty` | no recorded partition |

Non-session days (weekends, exchange holidays) are never gaps. The universe and
actions datasets use the same due days as bars; extra partitions on non-session
days are allowed. `--ping-url-env NAME` pings the https URL in that variable
(`<url>` on success, `<url>/fail` on failure; healthchecks.io convention); the
URL is never printed, and a redirect from the ping endpoint is never followed
(it could forward the token to another host or over http). `--min-coverage`
must be a finite number in [0, 1]; `--grace-hours` must be finite and
non-negative (a negative grace makes days due before their bars exist, and NaN
would make every comparison false). `collect --datasets` must name at least one
known dataset.

`collect` refuses to publish a bars/actions partition whose coverage is below
`--min-coverage` (default 0.95), so a partial source outage leaves the day
missing (and visible to the health check) instead of writing a thin partition
that a rerun could no longer fill.

## Known limits

* Universe refusal: an empty source file (a KIS master, a NASDAQ Trader
  series, the Binance USDT or Upbit KRW list) is refused on every run,
  including the first, whose partition would otherwise become an empty
  write-once baseline.
* Broker bars are the default for KR/US; at the default research budget a KR
  run takes about 70 minutes and a US run about two hours (two calls per
  instrument). Without broker credentials on the host the brokers are recorded
  as unavailable and Yahoo serves.
* Yahoo is an unofficial public endpoint (no SLA); KR coverage measured 120/120
  and US eligible coverage 150/150 on random samples, 2026-11-01. Per-instrument
  rate limits are unknown; the default is 0.3 s between requests, so a full US
  run (~10,900 instruments) takes about an hour, KR (~4,000) about 20 min.
* KR bars are KRX-only: KIS is read with market code `J` (KRX); NXT after-hours
  trading is not included (UNVERIFIED for Yahoo's and Toss's KR feeds).
* Corporate actions only cover what Yahoo reports (no broker client implements
  dividends or splits); rights issues, mergers and
  KR capital reductions are not covered. Delisting outcomes come from U2/U3/U4,
  not from this daily feed.
* Bars for an earlier session can be collected with `--date P --datasets
  bars_1d` only when a universe partition dated `<= P` exists, so the daily feed
  starts at its first universe day. History before that comes from the frozen
  corpora (kr-corpus-v1, U3/U4 rebuilds) under their own manifests.
