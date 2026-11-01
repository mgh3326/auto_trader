# Retrospective of real buys and sells (ROB-1439)

This package measures what real-money trades did over the six months of the dots
history export. It states facts for a later operator decision and recommends nothing.

It reads the de-identified export and a snapshot of public daily prices. It opens no
database or broker connection, uses no credentials and has no scheduler. It changes no
production code. From `app` it imports only `app.core.symbol`.

## Run it

Verify and analyse the export, and write both reports. This step uses no network:

```bash
uv run python -m research.trade_retro.run report \
  --export ~/work/herdr-artifacts/dots-history-2026-11-01 \
  --prices ~/work/herdr-artifacts/rob-1439-trade-retro/prices-2026-11-01 \
  --public-out docs/research/rob-1439-trade-retrospective-2026-11-01.md \
  --private-dir ~/work/herdr-artifacts/rob-1439-trade-retro
```

Add `--check` to compare both reports with a fresh computation, byte for byte, without
writing anything.

The price snapshot was made once with the command below, which uses the network. It
refuses a directory that already holds a snapshot, because a new fetch can pick up revised
prices. A new fetch means a new directory and a new report.

```bash
uv run python -m research.trade_retro.run fetch-prices \
  --export ~/work/herdr-artifacts/dots-history-2026-11-01 \
  --out <new directory>
```

| Exit code | Meaning |
| --- | --- |
| 0 | Done, or `--check` matched. |
| 1 | `--check` found a difference. |
| 2 | `--private-dir` is inside the repository. |
| 3 | The export or price snapshot does not match its manifest. Nothing was written. |

## Two reports, one computation

This repository is public and the export is private trading data, so the same numbers
are rendered twice.

| Report | Location | Holds |
| --- | --- | --- |
| Public | `docs/research/rob-1439-trade-retrospective-2026-11-01.md` | Ratios and counts, with masked symbols and masked accounts |
| Full | `report-full.md` in the private directory, with row-level CSVs | Symbols, amounts and account names |

Do not commit the full report, the CSVs, the export or the price snapshot. A test scans
the committed report for account names, grouped amounts and money columns.

## Modules

| Module | Job |
| --- | --- |
| `export.py` | Checks every file against the manifest before reading a row. |
| `prices.py` | Fetches public bars, and reads them back against their hashes. |
| `canonical.py` | Builds one order table and one fill table from the live tables. |
| `positions.py` | Matches lots first in, first out, into positions (section 2). |
| `entry.py` | Classifies entry types and computes fill rates (section 3). |
| `markouts.py` | Computes markouts and bootstrap intervals (section 3). |
| `idle.py` | Derives the idle-cash lower bound from fills (section 4). |
| `rules.py` | Computes the candidate-rule counterfactuals (section 5). |
| `report.py` | Renders both reports. No figure is typed by hand. |

The rules behind the numbers are in the report's Method and Data limits sections and in
each module's docstring.
