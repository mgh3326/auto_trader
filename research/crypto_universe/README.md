# crypto-universe-v1 (ROB-1432)

Survivorship-free crypto pair universe and daily candles. It replaces the
survivor-only enumeration of `crypto-corpus-v1`, which listed only the pairs an
exchange still lists today, so its delisted share was 0%.

Research only: public, unauthenticated market data; no `app/` import, database,
broker, account, credential, scheduler or LLM. `public_http.check_url` refuses
every URL outside the allowlisted public data endpoints before a socket opens.

## Commands (manual; there is no scheduler)

```bash
ROOT=~/work/herdr-artifacts/crypto-universe-v1   # raw data stays outside git

# Today's exchange-info snapshot. Append-only, idempotent per UTC day.
uv run python -m research.crypto_universe.cli snapshot --root "$ROOT"

# Rebuild candles + PIT universe from the archives (reads the latest snapshot,
# which must be dated after coverage_end). The build runs in .build-staging/;
# with --replace the old candles/ and universe/ are swapped out only after it
# succeeds. cache/ and snapshots/ are kept.
uv run python -m research.crypto_universe.cli build --root "$ROOT" --replace

# Pairs tradeable on a UTC day (refuses days after coverage_end and a table
# whose sha256 differs from the manifest).
uv run python -m research.crypto_universe.cli pit --root "$ROOT" \
    --venue binance_usdt_spot --date 2021-06-01
```

## Sources and coverage

| venue | candles | delisted pairs | live/delisted label |
|---|---|---|---|
| `binance_usdt_spot` | data.binance.vision spot monthly 1d zips, SHA-256 checked against the published `.CHECKSUM` | **yes** — the archive keeps every pair that ever traded | exchangeInfo snapshot: `TRADING` = live, `BREAK` or absent = delisted, other states = `not_trading_other` |
| `upbit_krw` | quotation API `candles/days`, paged back to the first candle | **only where a probe returns candles.** Upbit has no public archive and answers HTTP 404 `Code not found` for delisted markets. The build probes the known delisted markets plus `KRW-<base>` for every Binance base asset not listed on Upbit today; a probe that returns candles is ingested as a delisted pair, and every probe's status and body hash is recorded. A 404 cannot tell "delisted" from "never listed" | `market/all` snapshot: listed = live, absent = delisted |

* Coverage window per venue: from the earliest first trade to `coverage_end`,
  the last day of the newest published monthly archive. Upbit uses the same
  `coverage_end`.
* First/last trade = first/last UTC day with a bar that has `trades > 0`
  (Binance) or a candle at all (Upbit, which emits no candle for a day without
  trades).
* `last_trade_censored = true` means the last trade is on `coverage_end`: the
  pair may have traded later.
* Pairs whose archive has a checksum mismatch, a failed download or a
  malformed row are excluded and listed under `fetch_errors`, never truncated.
  A cached zip that no longer verifies is discarded and fetched again.
* The exchangeInfo snapshot must be dated after `coverage_end`; an older one
  would label pairs listed after it as delisted, so the build refuses it.
* exchangeInfo pairs with no monthly archive yet (listed after `coverage_end`,
  or `PRE_TRADING`) are counted in the report and are not in the table.
* Candles are split at `2025-01-01` (the crypto-corpus-v1 holdout start) into
  `exploration/` and `holdout/`; nothing in this package reads holdout files.

The measured numbers of the latest build are in `data/coverage.md`.

## Point-in-time rule

`PitUniverse.universe_as_of(t)` (all dates UTC, inclusive):

* a live pair is in the universe from its first trade onward;
* any other pair only on days inside one of its listing episodes;
* a day after `coverage_end` raises `OutsideCoverageError` (fail closed).

Listing episodes split the traded days wherever more than 7 consecutive days
have no trade. That separates a delisting followed by a relisting, and a symbol
reused for another asset (Binance `LUNAUSDT`: Terra classic until 2022-05-13,
LUNA 2.0 from 2022-05-31). The pair is not in the universe between episodes.

A live pair stays in the universe on days without a trade after its last
episode began: the exchange still lists it after `coverage_end`, so it was
listed on those days too. Membership means "listed", not "had volume that day".

Flags for analysis, not filters: `is_leveraged_token` (Binance UP/DOWN/BULL/BEAR
tokens) and `is_stable_base` (stablecoin or fiat base asset).

## Files

`data/` in this folder holds the committed, small outputs of the latest build:
the PIT tables, `coverage.md`/`coverage.json`, and `manifest.json` (input
snapshot hashes, table hashes, the hash of the candle-file index). Candles and
the snapshot files stay under the artifact root.

Snapshot layout: `snapshots/<YYYY-MM-DD>/<source>.json.gz` (gzip, mtime 0,
read-only, published by `os.link` so the name never holds a partial file and is
never replaced) and `snapshots/manifest.jsonl` (one appended line per file;
`sha256` of the raw body, `stored_sha256` of the `.gz`). Runs hold an exclusive
`flock`. A file without a manifest line, left by a crash, gets its line on the
next run (on any later day) after its bytes pass validation; a line torn by a crash is closed and
skipped. A run whose fetch crosses UTC midnight writes nothing.
