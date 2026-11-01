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

- **Event dates are brackets.** `listed_from` is the first capture showing the
  symbol. For an inactive interval, `listed_to` is the last capture showing it
  and `delisted_by` is the first capture without it. The true date lies inside
  that range. Early years have one or two captures per year, so a bracket can
  span more than a year.
- **Left censoring.** `listed_from_censored=true` marks intervals that start at
  the first capture of their series. The symbol may have listed earlier.
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
| `listed_from_censored` | interval starts at the series' first observation |
| `series`, `exchange`, `security_name`, `etf`, `likely_common_stock`, `observations` | provenance and classification |

`research.us_universe.pit.universe_as_of(rows, t)` returns the symbols with
`listed_from <= t` and (`listed_to` empty or `t <= listed_to`). A symbol first
seen after `t` is excluded, which prevents look-ahead. A symbol that delisted
after `t` is included, which prevents survivorship bias.

## Commands (manual only, no scheduler)

```bash
ROOT=~/work/herdr-artifacts/us-universe-v1

# Record today's universe (America/New_York date). Idempotent per day:
# a second run the same day fetches and writes nothing.
uv run python -m research.us_universe.snapshot --root "$ROOT"

# Fetch any new archive captures into the write-once cache, then build.
uv run python -m research.us_universe.rebuild --root "$ROOT" --out <new-dir>

# Reproducibility check: rebuild from the cache only; the outputs must be
# byte-identical to the committed ones (same code commit).
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

The committed `data_manifests/` holds the PIT table, `manifest.json` (every
input with its sha256 and capture URL, per-series coverage, inactive shares,
the output sha256 and the generating commit) and `coverage.md`.
