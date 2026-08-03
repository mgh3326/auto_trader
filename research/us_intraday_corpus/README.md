# us-intraday-corpus-v1

US intraday (1Hour full universe / 1Min top-500) research corpus builder.

> **SURVIVORSHIP_BIASED = TRUE.** The universe is a frozen snapshot of US common
> stocks that were **active** at snapshot time. Symbols delisted earlier are
> absent entirely, so every return, hit-rate, Sharpe and drawdown derived from
> this corpus is biased **optimistic**. Never cite a number from it without this
> label. `EXPLORATORY_BACKTEST_RESEARCH_ONLY`.

## Status

**BLOCKED_PRECONDITION** — no bar data has been fetched. Alpaca market-data
credentials are not reachable under the brief's constraints (see
`events/worker-final.md`). Everything that does not require them is complete:
the deterministic 1Min top-500 selection, and the full builder with its
invariants under test.

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

# measure the multi-symbol form + project the request budget, then stop
uv run python -m research.us_intraday_corpus.build --probe-only

# full build (1Hour, then 1Min)
uv run python -m research.us_intraday_corpus.build

# 1Hour only, sealing at the midpoint boundary
uv run python -m research.us_intraday_corpus.build --skip-1m

uv run pytest research/us_intraday_corpus/tests/ -q
```

## Boundaries

Read-only market data only. `assert_data_host()` runs before every request and
denies the trading and broker hosts by name; only GET against
`/v2/stocks/bars` is implemented. No operating-DB access, no broker or account
calls, no scheduler registration. Credentials come from the environment —
`.env.prod` is never read and no new secret is created.
