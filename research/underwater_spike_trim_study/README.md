# underwater_spike_trim_study

Offline historical backtest of the §128차 `underwater_spike_trim` shadow, run
across three markets (Upbit KRW crypto, KR equities, US equities) under one
pre-registered specification.

**Observation only.** Nothing in this package imports `app`, opens a socket,
or touches a database. `tests/test_offline_boundary.py` proves that as a
property of the import graph rather than as a claim.

## The question

An underwater position prints a big up-day with no resistance overhead. Is it
better to

1. keep holding,
2. trim 10% into the spike, or
3. trim 10% and rebid that 10% at support?

## Pre-registration

`spec.py` **is** the pre-registration and was frozen before any P&L was
computed. Event definition, the three payoff formulas, the horizons, the
cost-basis grid and the control design are all constants there. Changing one
after seeing a result is a new round and must be labelled as such.

Two proxy arms were added after observing *event counts only* (never P&L), and
both are declared in `spec.py`:

* `resistance_rule` — `named` (headline) vs `any` (literal). On a bar that
  closes at a new window high, the production clustering reports exactly one
  "resistance": `fib_0`, which is that same bar's own intraday high. The
  literal rule therefore fires on ~1% of spike days, so the headline arm
  ignores single-source `weak` clusters.
* `rebid_strength` — `strong` (as written) vs `moderate_plus`. `strong`
  requires three independent sources inside one 2% band and exists below the
  price on only ~7% of crypto events, which would leave option (3) mostly
  "unavailable". Both are reported; neither replaces the other.

## Running

```bash
# scan one market's frozen corpus -> observations.jsonl + scan-summary.json
uv run python -m research.underwater_spike_trim_study.run --market crypto
uv run python -m research.underwater_spike_trim_study.run --market kr
uv run python -m research.underwater_spike_trim_study.run --market us

# sensitivity: the production get_support_resistance tool fetches 60 bars,
# the pre-registered primary is 120
uv run python -m research.underwater_spike_trim_study.run --market crypto --level-window 60

# aggregate (never re-reads a corpus)
uv run python -m research.underwater_spike_trim_study.report \
  --observations <out>/crypto/observations.jsonl --out <out>/crypto/report.json

# the one real underwater lot
uv run python -m research.underwater_spike_trim_study.case_study \
  --observations <out>/crypto/observations.jsonl --out <out>/crypto/xrp-case.json
```

Tests live under `tests/` and are **not** collected by CI (`testpaths =
["tests"]` in `pyproject.toml`). Run them explicitly:

```bash
uv run pytest research/underwater_spike_trim_study/tests/ -q
```

## Data

| Market | Corpus | Exploration span | Universe | Sealed |
|---|---|---|---|---|
| crypto | `crypto-corpus-v1` `dataset-labeled/venue=upbit_krw` | 2017-10-24 → 2024-12-31 | 139 KRW markets, `SURVIVORSHIP_BIASED` | 2025-01-01 → 2026-08-01 |
| kr | `kr-corpus-v1` run `20260803-1001` | 2015-01-01 → 2024-12-31 | KOSPI + KOSDAQ, `BUILT_WITH_GAPS` | 2025-01-01 → 2026-07-31 |
| us | `us-corpus-v1` `dataset/market=us` | 2016-01-01 → 2024-12-31 | 5,355 frozen active common stocks, `SURVIVORSHIP_BIASED=TRUE` | 2025-01-01 → 2026-07-31 |

Every sealed holdout is refused through that corpus's own guard
(`crypto_corpus.loader`, `kr_corpus.backtest.holdout_guard`,
`us_corpus.holdout_gate`). No result here covers 2025 or later.

## Market-specific honesty rules

* **KR ceiling locks.** A zero-range bar whose close-to-close move sits at the
  statutory daily cap (±30% from 2015-06-15, ±15% before) is treated as
  price-unreachable: the trim is recorded as non-executable and the bar is
  removed from the rebid fill window. `fill_used_locked_bar` records whether
  that removal actually changed a fill verdict, so the exclusion's cost is
  measured rather than assumed.
* **Gaps.** Every observation is valued twice — `event_close` (trim at the
  event bar's close) and `next_open` (trim at the following bar's open, exit
  at the open `H` sessions later). `gap_next_open` is reported per market.
* **Missing sessions.** A close-to-close return that silently bridges a
  dropped session is not a 24h return. Bars whose predecessor is not the
  immediately preceding calendar session are excluded from the event and
  control pools entirely.

## Files

| File | Role |
|---|---|
| `spec.py` | the frozen pre-registration |
| `levels.py` | pure port of the production S/R + RSI arithmetic |
| `corpora.py` | the three corpus readers, normalised to one bar schema |
| `events.py` | event detection and seeded control sampling |
| `simulate.py` | the three options' payoffs (no data access) |
| `report.py` | aggregation into arms |
| `case_study.py` | the real Upbit KRW-XRP lot |
