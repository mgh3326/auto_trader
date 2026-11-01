# us-universe-v1 — US point-in-time listing universe (ROB-1431)

`research/us_corpus` is labelled `SURVIVORSHIP_BIASED=TRUE` because its
universe is a frozen list of symbols that were active at snapshot time. Its
README says no free source of delisted US symbols was found. This package
supplies that history. It rebuilds which US exchange-listed symbols were
listed on any date since 2008, delisted ones included. It also adds a manual
command that records today's universe, so the history keeps growing from now on.

`PURPOSE = EXPLORATORY_BACKTEST_RESEARCH_ONLY`. The package uses no broker
API, credentials, database or scheduler. It imports only `app.core.symbol`
from `app/`, and a test enforces that.

## Sources, coverage and gaps

| Source | Used | What it gives | Limits |
|---|---|---|---|
| NASDAQ Trader symbol directory (`nasdaqlisted.txt`, `otherlisted.txt`), fetched live | yes — daily snapshot command | Complete listed set for Nasdaq, NYSE, NYSE American, NYSE Arca, Cboe BZX and IEX, dated by the file's own `File Creation Time` | Shows today only; gives no history |
| Internet Archive (Wayback Machine) captures of the same two files | yes — `rebuild` | The same complete listed sets on the dates the archive captured them, 2008 → 2026 | Captures are sparse and uneven (see `data_manifests/coverage.md`). An event between two captures is known only to lie between them. |
| Alpaca paper `GET /v2/assets?status=inactive` (the paper broker's asset list) | **no** | Symbols Alpaca marks inactive | Needs paper API credentials, which this unit may not read. The asset schema (`id, symbol, name, status, tradable, class`) has **no listing or delisting dates**, so it could only add symbols, not intervals. Can be added later as an optional input, run by the operator. |
| SEC `company_tickers_exchange.json` | **no** | Current ticker → CIK → exchange | Current only, and formerly-listed tickers are not kept, so it adds nothing for delisted symbols |
| KIS master files (`us_symbol_universe`) | **no** | Current KIS-tradable set | Current only. `is_active=false` means "missing from a later file", with no date. Rows live in the production DB, which this unit must not touch. |
| NASDAQ Trader Daily List / symbol change history | **no** | Exchange-announced additions and deletions | Served through JavaScript pages, not as files; the symbol change history URL returns 404 |

Known gaps, stated in the output rather than papered over:

- **A stale series is refused.** `rebuild` stops if one file's newest
  accepted observation lags the other's by more than 7 days, because every
  name delisted from it since would read as active. Run `snapshot` first.
- **Event dates are brackets.** `listed_from` is the first capture showing the
  symbol. For an inactive interval, `listed_to` is the last capture showing it
  and `delisted_by` is the first capture without it. The true date lies inside
  that range. Early years have one or two captures per year, so a bracket can
  span more than a year.
- **Listing dates are brackets too.** `otherlisted` has no capture between
  2013-10-23 and 2017-06-07. A NYSE name that listed in that gap (TWTR, for
  example, listed 2013-11) has `listed_after=2013-10-23`,
  `listed_from=2017-06-07`, and is absent from the universe until 2017.
  Excluding it is the no-look-ahead choice. A consumer who prefers coverage
  can widen with `listed_after`.
- **Left censoring.** `listed_from_censored=true` marks intervals that start at
  the first capture of their series. The symbol may have listed earlier.
- **One capture is unusable at the source.** The archive's copy of
  `nasdaqlisted` from 2008-11-20 is truncated (`IncompleteRead` on every
  fetch), so it is not in the cache. The cache holds 86 `nasdaqlisted` and 45
  `otherlisted` captures, plus today's snapshot.
- **Short-lived listings are missed.** A symbol that listed and delisted
  between two captures never appears.
- **Ticker reuse.** Each run of consecutive captures is a separate interval,
  so a ticker that disappears and comes back gets two rows. A reuse with no
  gap between captures merges into one interval. `security_name` is the name
  at the interval's last capture.
- **Symbol-level status.** A symbol counts as inactive only if none of its
  intervals is active. A transfer between Nasdaq and NYSE therefore stays
  active.
- **Common-stock subset** (`likely_common_stock`) uses the screener's existing
  name and flag heuristic (`app/services/us_common_stock_classifier.py`); a
  test checks the two stay identical. Old `nasdaqlisted` files have no `ETF`
  column (`etf` is empty), so only name markers exclude funds there.

## PIT table (`us_pit_universe.csv`)

One row per listing interval:

| column | meaning |
|---|---|
| `symbol` | DB dot format (`BRK.B`, via `app.core.symbol.to_db_symbol`) |
| `listed_from` | first observation date in the interval (inclusive) |
| `listed_to` | last observation date (inclusive); empty while active |
| `status` | `active` (in the latest observation of its series) or `inactive` |
| `status_source` | `<series>@<date>`: the observation that decides the status (the latest one for active, the first one missing the symbol for inactive) |
| `delisted_by` | first observation without the symbol; the delisting is in `(listed_to, delisted_by]` |
| `listed_after` | last observation before the interval; the listing is in `(listed_after, listed_from]`; empty when censored |
| `listed_from_censored` | interval starts at the series' first observation |
| `series`, `exchange`, `security_name`, `etf`, `likely_common_stock`, `observations` | provenance and classification |

`research.us_universe.pit.universe_as_of(rows, t)` returns the symbols with
`listed_from <= t` and (`delisted_by` empty or `t < delisted_by`):

- A symbol first seen after `t` is excluded, which prevents look-ahead.
- A symbol that delisted after `t` is included, which prevents survivorship
  bias.
- Inside the bracket `(listed_to, delisted_by)`, a symbol about to delist
  looks exactly like one that survives: both were last seen listed. Dropping
  it there would use the later capture and remove exactly the names about to
  delist, so the default keeps it.

`end="listed_to"` selects the narrower evidence-only rule, which has the bias
just described.

## Commands (manual only, no scheduler)

```bash
ROOT=~/work/herdr-artifacts/us-universe-v1

# Record today's universe (America/New_York date). Idempotent per day:
# a second run the same day fetches and writes nothing.
uv run python -m research.us_universe.snapshot --root "$ROOT"

# Fetch any new archive captures into the write-once cache, then build.
uv run python -m research.us_universe.rebuild --root "$ROOT" --out <new-dir>

# Reproducibility check: rebuild from the cache only. us_pit_universe.csv,
# manifest.json and coverage.md must be byte-identical to the committed ones.
# The generating commit is kept apart in build_info.json.
uv run python -m research.us_universe.rebuild --root "$ROOT" --out <new-dir> --offline
```

Layout under `$ROOT`:

```
snapshots/<YYYY-MM-DD>/nasdaqlisted.txt   raw bytes, write-once
snapshots/<YYYY-MM-DD>/otherlisted.txt
snapshots/manifest.jsonl                  one line per day, append-only
sources/wayback/<series>/<capture-ts>.bin raw archive captures, write-once
```

Every file is written once: staged as `.partial`, fsynced, then hard-linked
into place, which fails if the target already exists. Nothing is overwritten
or deleted. A body is accepted as an observation only if it has a known
header, the `File Creation Time` footer and at least 1,000 rows. A truncated
capture or error page is listed as a rejected input. It is never read as "these
symbols delisted".

The committed `data_manifests/` holds:

- the PIT table;
- `manifest.json`: every input with its sha256 and capture URL, per-series
  coverage, inactive shares and the output sha256;
- `coverage.md`;
- `build_info.json`: the generating commit and the manifest sha256.
