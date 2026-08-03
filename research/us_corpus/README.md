# us-corpus-v1 — US daily equity corpus builder

> ## 🔴 SURVIVORSHIP_BIASED = TRUE
>
> The universe is a **frozen snapshot of currently-active US common stocks**
> (5,355 symbols). Symbols that delisted before the snapshot are **absent
> entirely** — there are no rows for them at any date. Every return, hit-rate,
> Sharpe or drawdown computed from this corpus is therefore biased
> **optimistic**.
>
> **🔴 Do not cite any number derived from this corpus without this label.**
>
> The KR corpus resolved this by pulling delisting history from `pykrx`. The US
> corpus could not — no equivalent free delisted-symbol history was available.
> When the two corpora are used side by side, that asymmetry must be stated:
> KR results are survivorship-corrected, US results are not, so they are **not
> directly comparable** on any performance metric.

`PURPOSE = EXPLORATORY_BACKTEST_RESEARCH_ONLY`

## What this is

Deterministic build of adjusted daily OHLCV bars from Yahoo Finance for the
frozen US universe, split into an exploration set and a sealed holdout.

| | |
|---|---|
| Source | Yahoo Finance (`/v8/finance/chart`), **no fallback source** |
| Window | 2016-01-01 → 2026-07-31, `1d`, adjusted |
| Calendar | XNYS (`exchange_calendars`), America/New_York |
| Train | 2016-01-01 → 2022-12-31 |
| Validation | 2023-01-01 → 2024-12-31 |
| Holdout | 2025-01-01 → 2026-07-31 — **written, never read** |
| Forward OOS | starts 2026-08-03 |

## Layout

```
/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/
  inputs/common_stock_universe.csv      # pinned input, read-only
  crosscheck/kis_db_frozen_sample.csv   # pinned input, read-only
  dataset/market=us/year=YYYY/          # exploration 2016–2024
  holdout/market=us/year=YYYY/          # 🔴 holdout 2025–2026, do not read
  reports/                              # coverage, gaps, empties, crosscheck
  probe/alpaca_lookback.json            # intraday feasibility measurement
  manifest.json · checksums.sha256 · holdout-access.log
```

## Running

```bash
uv run python -m research.us_corpus.build       # fetch (resumable, ~1h)
uv run python -m research.us_corpus.finalize    # partition + validate + manifest
uv run python -m research.us_corpus.alpaca_probe  # bounded intraday probe
```

`build` is checkpointed per symbol (`_staging/checkpoint.jsonl`) and heartbeats
to the job's `events/progress.md`, so a killed session resumes rather than
refetching.

## Invariants this code enforces

These are guard rails, not style preferences — none of them may be relaxed to
make a number look better.

- **Digest gate.** Both pinned inputs are re-verified against their SHA-256
  before their contents are read. Mismatch is `BLOCKED_PRECONDITION`.
- **Budget gate, before the first request.** Yahoo's chart endpoint has no
  multi-ticker form, so batching cannot reduce the count: it is one request per
  symbol (measured — 3 tickers produce 3 requests). Projection is
  `5355 + 1000 retry pool + 5 handshake = 6360` against `MAX_REQUESTS=12000`.
  Over budget blocks the run; the cap is not raisable from inside the process.
- **Rate gate slows only.** A rate-limit signal doubles the request interval and
  there is no path that speeds it back up.
- **No forward fill, no interpolation, no second source.** A session Yahoo did
  not return does not exist here; it is counted in `reports/explicit_gaps.csv`.
- **Empty ≠ error ≠ parse failure.** A symbol returning zero rows is recorded as
  `empty` — that is the measurable footprint of the survivorship bias. An
  unparseable non-empty response raises instead, so a bug can never masquerade
  as evidence that a symbol stopped trading.
- **Universe count assertion.** `keep_default_na=False` is load-bearing: the
  universe contains the ticker `NA` (Nano Labs), which pandas would otherwise
  silently drop. The count check is the backstop.
- **Atomic writes.** `.partial` → fsync → rename. Finished files are never
  overwritten in place.
- **Holdout is write-only.** Every read in `finalize` targets `_staging/`;
  `HOLDOUT_DIR` is opened for write only, and `holdout-access.log` records the
  writes.

## Crosscheck — and a boundary note

`CROSSCHECK_MODE=FROZEN_DB_SAMPLE` is **diagnostic only**. The frozen KIS sample
never overwrites a Yahoo value; disagreements are reported and nothing else.

⚠️ **All 1,414 rows of the frozen sample fall inside the holdout window**
(2025-05-20 → 2026-07-30). The crosscheck therefore runs against `_staging/`
rather than the sealed `HOLDOUT_DIR`, restricted to exactly the
`(symbol, session_date)` pairs already present in the authorised, digest-pinned
input file, and emits only aggregate agreement statistics. See the job report —
this collision is flagged for the brief author's decision.

The crosscheck includes a **date-alignment probe** because a naive same-date
comparison is misleading here: the sources agree on prices and disagree on the
date label. See `reports/crosscheck_report.json`.

## Alpaca probe scope

`alpaca_probe.py` measures how far back Alpaca serves intraday bars, to inform a
possible v2 intraday corpus. It asserts `host == data.alpaca.markets`
immediately before every send, refuses trading hosts and account/order/position
paths outright, caps itself at 30 calls, and **collects no intraday data** —
only the oldest reachable timestamp. Collecting intraday bars is out of scope
and requires separate approval.
