# Data contract v1 — point-in-time inputs for U7/U8 (ROB-1443)

The boosting baseline (U7) and the TFT challenger (U8) take the same inputs for
KR, US and crypto. This contract fixes what those inputs look like on disk, when
each row became knowable, and how a missing or stale day is detected. Code:
`research/data_plane/` (commands in its `README.md`). Collector inventory and
keep/fix/rebuild/retire decisions: `data-plane-collector-inventory-2026-11-01.md`.

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
| `source` / `source_version` | e.g. `yahoo_chart`/`v8`, `binance_klines`/`api-v3`, `upbit_candles_days`/`v1`, `kis_master`/`v1`, `nasdaqtrader_symdir`/`v1`, `exchange_listing`/`v1`, `universe_diff`/`v1` |
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
  missing instruments with a reason (capped at 500).
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
| bar source | Yahoo chart v8 (`<code>.KS`/`.KQ`) | Yahoo chart v8 | Binance `api/v3/klines` (data-api.binance.vision), Upbit `candles/days` |
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
its own day**: a run for an earlier `P` is refused before any fetch.

Bars and actions for day `P` select instruments from the **latest two universe
partitions dated `<= P`** — never a later universe (survivorship), and two so
that an instrument dropped from today's listing still gets its last bar. A bar
backfill for a day before the first universe partition is refused.

### bars_1d

`bar_date`, `open`, `high`, `low`, `close`, `volume` (+ `adj_close`,
`currency` for Yahoo; `quote_volume`, `trades` for crypto), `price_basis =
as_observed`. A bar observed on its own day is the raw (unadjusted) bar. **A bar
backfilled later is the source's view at observation time**: Yahoo's `close` is
split-adjusted retroactively, so for backfilled rows check
`observed_at − available_at` and apply the `corporate_actions` ledger, never the
source's `adj_close`, when building a training series.

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
  changes become available on that later day) instead of being lost.
* **First-observed semantics**: `available_at` is when this collector first saw
  the fact (the sources carry no announcement time). Each row has `fact_hash`
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
URL is never printed.

`collect` refuses to publish a bars/actions partition whose coverage is below
`--min-coverage` (default 0.95), so a partial source outage leaves the day
missing (and visible to the health check) instead of writing a thin partition
that a rerun could no longer fill.

## Known limits

* Yahoo is an unofficial public endpoint (no SLA); KR coverage measured 120/120
  and US eligible coverage 150/150 on random samples, 2026-11-01. Per-instrument
  rate limits are unknown; the default is 0.3 s between requests, so a full US
  run (~10,900 instruments) takes about an hour, KR (~4,000) about 20 min.
* KR bars from Yahoo are KRX-only; NXT after-hours trading is not included (UNVERIFIED for Yahoo's KR feed).
* Corporate actions only cover what Yahoo reports; rights issues, mergers and
  KR capital reductions are not covered. Delisting outcomes come from U2/U3/U4,
  not from this daily feed.
* Bars for an earlier session can be collected with `--date P --datasets
  bars_1d` only when a universe partition dated `<= P` exists, so the daily feed
  starts at its first universe day. History before that comes from the frozen
  corpora (kr-corpus-v1, U3/U4 rebuilds) under their own manifests.
