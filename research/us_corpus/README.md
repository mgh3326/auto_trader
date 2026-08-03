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
| Holdout | 2025-01-01 → 2026-07-31 — **write-only; sealed artifact never reopened** |
| Forward OOS | starts 2026-08-03 |

## Layout

```
/Users/mgh3326/work/herdr-artifacts/us-corpus-v1/
  inputs/common_stock_universe.csv         # pinned input, read-only
  crosscheck/kis_db_frozen_sample_v2.csv   # pinned input (active)
  crosscheck/kis_db_frozen_sample.csv      # v1, superseded — provenance only
  dataset/market=us/year=YYYY/             # exploration 2016–2024, labelled
  holdout/market=us/year=YYYY/             # 🔴 sealed 2025–2026, write-only
  reports/                                 # coverage, gaps, empties, crosscheck
  probe/alpaca_lookback.json               # intraday feasibility measurement
  manifest.json                            # records the generating commit SHA
  checksums.sha256                         # dataset + reports + manifest
  holdout-write-registry.sha256            # sealed digests, NOT in checksums
  holdout-access.log                       # WRITE and READ-refusal ledger
```

## Running

```bash
uv run python -m research.us_corpus.build       # fetch (resumable, ~1h)
uv run python -m research.us_corpus.finalize    # partition + validate + manifest
uv run python -m research.us_corpus.alpaca_probe  # bounded intraday probe
uv run python -m research.us_corpus.verify_gates  # offline boundary proofs
```

🔴 `finalize` stamps the generating commit SHA into the manifest, so run it
**after** committing the code. If the tree is dirty the SHA is suffixed
`-dirty`, which makes non-reproducible artifacts visible rather than silent.

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
- **Holdout is write-only, structurally.** No digest in this package is computed
  by reopening a file — every hash comes from the write buffer. There is
  therefore no artifact-root sweep to exclude the holdout from, which is how R1
  read both sealed partitions while reporting `written_not_read: true`. Sealed
  writes go through `holdout_gate`, which offers no read function; `guard_read`
  logs a `READ` line and raises. An AST check refuses any reintroduced
  `ARTIFACT_ROOT.rglob(...)` and any read call taking a holdout path.
- **Survivorship label is enforced, not just documented.** Every Parquet
  partition carries `SURVIVORSHIP_BIASED=TRUE` in its schema metadata, every
  numeric artifact carries a label field or column, and `read_labeled_parquet`
  raises `UnlabeledCorpusError` on an unlabelled file rather than filtering or
  returning an empty frame.

## What the label does and does not stop

The label is stamped **in place**, into the same tree consumers read. That was
chosen over publishing a separate labelled tree because BLOCKER-3 required
regenerating every artifact at the final commit anyway — the integrity seal is
created *after* labelling, in the same operation, so nothing pre-existing is
broken. A separate tree would have left an unlabelled original in place forever.

🔴 It is still not a complete block, and should not be described as one:

- `pd.read_parquet(path)` and `pq.read_table(path)` return rows without
  surfacing schema metadata, so a consumer who ignores the supported loader
  never sees the label,
- converting to CSV/JSON, or copying the file elsewhere, drops the metadata,
- `duckdb`/`polars` scanning `dataset/` ignore it as well.

So: `CONSUMER_CAN_READ_UNAWARE = NO` through the supported loader, `YES` in
absolute terms. The label is present in every file; nothing forces a reader to
look at it.

## Crosscheck — and a boundary note

`CROSSCHECK_MODE=FROZEN_DB_SAMPLE` is **diagnostic only**. The frozen KIS sample
never overwrites a Yahoo value; disagreements are reported and nothing else.

The pinned sample is **v2** (`kis_db_frozen_sample_v2.csv`). v1 was superseded:
its export was timezone-shifted, labelling every row one calendar day early. The
five value columns are identical across v1 and v2 for all 1,414 rows — only the
dates moved. Against v2 at lag 0 there are **zero** price mismatches over 1%.
🔴 v1 is retained unmodified as correction provenance and is never read by the
build; the manifest records both digests and why v1 was replaced.

⚠️ **All 1,414 rows fall inside the holdout window** (v2: 2025-05-21 →
2026-07-31). The crosscheck therefore runs against `_staging/` before the split,
restricted to exactly the `(symbol, session_date)` pairs already present in the
authorised digest-pinned input, and emits only aggregate statistics. The sealed
`HOLDOUT_DIR` is never opened. See the job report for the precise wording — the
claim is "the sealed artifact was not opened", not "the holdout was never seen".

The crosscheck keeps the **date-alignment probe** that originally exposed the v1
shift. It reports match rates at two tolerances because the residual after
alignment is a piecewise-constant dividend factor (adjusted vs raw), which makes
a single exact-match rate look like a partial effect.

## Alpaca probe scope

`alpaca_probe.py` measures how far back Alpaca serves intraday bars, to inform a
possible v2 intraday corpus. It asserts `host == data.alpaca.markets`
immediately before every send, refuses trading hosts and account/order/position
paths outright, caps itself at 30 calls, and **collects no intraday data** —
only the oldest reachable timestamp. Collecting intraday bars is out of scope
and requires separate approval.
