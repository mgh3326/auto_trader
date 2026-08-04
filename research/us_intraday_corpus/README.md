# us-intraday-corpus-v1

US intraday (1Hour full universe / 1Min top-500) research corpus builder.

> **SURVIVORSHIP_BIASED = TRUE.** The universe is a frozen snapshot of US common
> stocks that were **active** at snapshot time. Symbols delisted earlier are
> absent entirely, so every return, hit-rate, Sharpe and drawdown derived from
> this corpus is biased **optimistic**. Never cite a number from it without this
> label. `EXPLORATORY_BACKTEST_RESEARCH_ONLY`.

## Status — `BUILT_WITH_GAPS`

493,898,211 1Min rows for the top 500 US symbols, 2016-01-01 … 2026-07-31
(409,638,229 exploration / 84,259,982 holdout), 52,114 requests, 9.28 h, 10 GiB.

Read these three caveats before using it:

> ### 🔴 `PAGE_COMPLETENESS = UNVERIFIED`
> The page-chain instrument was broken during the shipped run (every chain
> recorded `complete=false`), so **per-chain pagination completeness is not
> established**. The substitute arguments do not close the gap: row
> reconciliation is internal-consistency only, the "stop-then-resume implies no
> truncation" argument is **withdrawn** (annual chains are independent, so a
> counterexample exists), and the checksum/access-log evidence is circular
> (write-time digests compared against write-time records). Proving it requires
> re-running collection with the fixed instrumentation to capture the terminal
> `next_page_token=null` per chain. See `reports/page_chain_integrity.json`.
> Row-level checks (OHLC, duplicates, monotonicity, nulls) *did* pass on a full
> exploration scan.

> ### 🔴 Coverage is 499 of 500, not 500
> `PSKY` has **zero exploration rows**; its only data lies inside the sealed
> holdout. Any "500" figure is the any-window count and includes the holdout.

> ### 🔴 Ticker identity is point-in-time
> Alpaca serves point-in-time tickers while the universe file is retroactively
> renamed, so joining on ticker across time can splice two different companies
> together (`SNOW` in 2016 is Intrawest Resorts, not Snowflake). 6 of 500
> symbols affected — see `reports/ticker_identity_caveats.csv`. This is a
> **separate axis from survivorship bias** and the label does not defend
> against it.

**Scope C** (operator decision): 1Min top-500 only. 1Hour collection is dropped
and recorded as a `DEFERRED_NOT_ABANDONED` data gap — it needs 130k–246k
requests at the measured ≤416 rows/request, against a cap of 80,000. The
top-500's hourly bars are not a gap: they are derivable locally by resampling
this corpus's 1Min data.

## Layout

| module | role |
| --- | --- |
| `config.py` | §1 literals. Nothing here is inferred at runtime. |
| `selection.py` | Deterministic 1Min top-500 by 2024 mean `close*volume`. |
| `alpaca_data.py` | GET-only client pinned to `data.alpaca.markets`. |
| `bars.py` | UTC storage + America/New_York `session_date` (ROB-1206). |
| `writer.py` | Atomic, labelled, hashed-at-write parquet output. |
| `loader.py` | The only sanctioned reader; enforces the bias label. |
| `finalize.py` | Manifest + checksums sealing. |
| `build.py` | Budget gate → 1Hour → midpoint checkpoint → 1Min. |

## The three invariants this corpus exists to get right

The daily sister corpus `us-corpus-v1` was BLOCKED on exactly these three, so
each is enforced in code and pinned by a regression test rather than documented
and hoped for.

**1. The holdout is written and never read.** Digests are computed from the
in-memory buffer at write time (`writer.write_parquet_atomic`), so a holdout
file is covered by `checksums.sha256` without ever being re-opened.
`finalize._shippable_files()` excludes `holdout/` from the post-hoc walk, and
`hashing.sha256_of_file` refuses holdout paths outright. The access log records
**READ as well as WRITE**, and `written_not_read` in the manifest is *derived*
from that log — so the claim cannot contradict the evidence.

**2. The survivorship label is enforced, not documented.** It is stamped into
every parquet's file metadata, embedded as real fields in every CSV/JSON
carrying numbers, and required by `loader.load_dataset(...)` via
`acknowledge_survivorship_bias=True`. `finalize.seal()` aborts if any shipped
artifact lacks it.

**3. Artifacts are reproducible from the shipped commit.** `checksums.sha256`
covers `reports/*` and `inputs/*`, not just parquet, and the manifest records
the exact `git rev-parse HEAD` the artifacts were generated from.

## Usage

```bash
# 1Min universe selection (no credentials needed; reads only exploration data)
uv run python -m research.us_intraday_corpus.selection

# measure page geometry + project the request budget, then stop
uv run python -m research.us_intraday_corpus.build --probe-only

# build under scope C -- 1Min top-500. This is the DEFAULT (--phase 1m).
uv run python -m research.us_intraday_corpus.build

# 1Hour is a recorded data gap: collecting it requires an explicit opt-out
uv run python -m research.us_intraday_corpus.build --phase 1h --override-scope-c

uv run pytest research/us_intraday_corpus/tests/ -q
```

Credentials come from the dedicated read-only file only
(`ENV_FILE=…/.env.alpaca-data-readonly.native`); `.env.prod`, `.env.dev` and
`.env` are refused by name.

## Reading the data

`loader.load_dataset(acknowledge_survivorship_bias=True)` is the sanctioned
reader. The holdout is write-only: `loader.load_holdout()` always raises, and
the guard resolves **inode identity** as well as canonical paths, so hardlink,
symlink, case and `..` aliases of a sealed file are all refused.

## Boundaries

Read-only market data only. `assert_data_host()` runs before every request and
denies the trading and broker hosts by name; only GET against
`/v2/stocks/bars` is implemented. No operating-DB access, no broker or account
calls, no scheduler registration. Credentials come from the environment —
`.env.prod` is never read and no new secret is created.
