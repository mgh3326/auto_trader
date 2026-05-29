# ROB-356 — Binance USD-M funding + OI PIT feature builder

Research/feature-construction only. Read-only PUBLIC data (`data.binance.vision`
`futures/um` monthly `fundingRate` + daily `metrics`). **No strategy, no backtest, no
parameter sweep, no runtime strategy params, no `/invest` exposure, no
broker/order/watch/order-intent mutation, no Binance Demo `confirm=true`, no
live/mainnet endpoint, no scheduler/TaskIQ/Prefect/cron/daemon, no production DB write,
no raw large market-data committed, no secrets, no OHLCV/volume proxy for OI.**

This issue builds the deterministic, point-in-time (PIT), survivorship-safe funding+OI
**feature artifact** that a later bounded crowding/deleveraging event study would consume.
It is the step after ROB-355/PR #1000, which audited the data and found funding-only
`feasible` (baseline/control) and **funding + open-interest `feasible` and the primary next
line**. It does **not** open or run that backtest — [ROB-343](https://linear.app/mgh3326/issue/ROB-343)
remains deferred.

## Why this issue exists (context)

ROB-353 screened out the generic price-action families (trend / momentum / breakout) on
**gross** expectancy — the failure was edge, not fees. ROB-355 then audited derivatives-native
data and ranked **funding + OI crowding/deleveraging** as the feasible primary line
(liquidation `needs_vendor_data` — no archive; sweep `partial`). This issue proves the
funding+OI features can be constructed cleanly and PIT-safely so the crowding study, *if*
opened, starts from trustworthy inputs.

## What this builds

| Component | Path | Role |
|---|---|---|
| Archive parsers (pure) | `research/nautilus_scalping/funding_oi_archive.py` | zip-CSV text → normalized `FundingRow` / `MetricRow`; dedup; UTC epoch-ms |
| Feature core (pure) | `research/nautilus_scalping/funding_oi_features.py` | PIT join, delist trim, OI-start bounding, OI features, backward as-of funding |
| `oi_coverage` field | `research/nautilus_scalping/pit_universe.py` | additive `SymbolListing` / `_META_FIELDS` coverage field |
| Builder CLI (operator-gated) | `research/nautilus_scalping/build_funding_oi_features.py` | read-only network RUN + coverage summary + deterministic verdict |
| Tests | `research/nautilus_scalping/tests/test_funding_oi_{archive,features,readiness}.py` + `test_pit_universe.py` | PIT semantics + verdict |

Generated feature tables and the coverage summary are written **only** under the gitignored
`results/` artifact root (`AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` if set). No raw market data is
committed.

## Confirmed archive schemas (read-only probe, `2026-05-29`)

- **funding** `futures/um/monthly/fundingRate/<sym>/<sym>-fundingRate-YYYY-MM.zip`
  CSV `calc_time, funding_interval_hours, last_funding_rate`. `calc_time` is epoch **ms UTC**;
  ~3 rows/day (8h). `funding_interval_hours` is **per-row** — 8h→4h interval changes live in
  the data and are carried as a feature, never assumed constant.
- **OI** `futures/um/daily/metrics/<sym>/<sym>-metrics-YYYY-MM-DD.zip`
  CSV `create_time, symbol, sum_open_interest, sum_open_interest_value,
  count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio,
  sum_taker_long_short_vol_ratio`. `create_time` is a **string UTC** datetime, 5-min grid.
  **Duplicate rows occur** (same `(symbol, create_time)` repeated) → deduped (first wins);
  genuinely-distinct timestamps are preserved.

## PIT semantics (the point of the artifact)

1. **Canonical grid = OI metrics timestamps** (5-min, UTC, deduped) — the dense
   crowding/deleveraging axis. Funding (monthly, ~3/day) is sparse context, not the grid.
2. **Backward as-of funding join, no future leakage.** For an OI row at `t`, attach the most
   recent funding row with `calc_time <= t`. Realized `last_funding_rate` and per-row
   `funding_interval_hours` reach an OI row only **at/after** the funding `calc_time`
   (known-after). OI rows before the first funding `calc_time` carry `None` funding fields.
3. **`delisted_at` is EXCLUSIVE.** Rows at/after `delisted_at` are dropped from both series
   before join — no post-delist frozen-tail leakage.
4. **Per-symbol OI-start bounding** is implicit: the grid starts at the first OI row, so
   funding observations earlier than OI simply have no row to attach to. OI archives start
   later than funding/klines for most symbols (ROB-355), so this bound is real and reported.
5. **All timestamps normalized to epoch-ms UTC** (`create_time` parsed as UTC).
6. **OI from actual open interest only.** `sum_open_interest` / `sum_open_interest_value`
   come straight from the metrics archive; **no OHLCV/volume/wick proxy** is used for OI.
   Rolling z-scores use population std and are bounded to `0.0` on a zero-variance window
   (never NaN/inf).

## Feature schema (`FeatureRow`)

`ts` (epoch ms UTC), `symbol`; OI: `sum_open_interest`, `sum_open_interest_value`,
`oi_delta`, `oi_pct_change`, `oi_zscore`; positioning passthrough:
`count_toptrader_long_short_ratio`, `sum_toptrader_long_short_ratio`,
`count_long_short_ratio`, `sum_taker_long_short_vol_ratio`; funding (backward as-of):
`funding_calc_time`, `last_funding_rate`, `funding_interval_hours`, `funding_rate_zscore`.

Funding-only carry remains a **baseline/control** and is not promoted here. An OI-to-ADV
crowding ratio (real volume denominator, not an OI proxy) is a deliberate **additive
extension** left out of this artifact so the build stays within the two declared archives
(`fundingRate` + `metrics`) and carries zero OI-proxy risk; the OI-native crowding signal
(`oi_zscore` / OI vs. its trailing level) is provided instead.

## `oi_coverage` manifest extension

`oi_coverage` is added to `SymbolListing` and `_META_FIELDS` **additively** alongside
`kline_coverage` / `funding_coverage`. `to_records()` omits it when `None`, so existing
manifests serialize byte-for-byte unchanged and the committed `pit_universe.v1.json`
`snapshot_hash` is preserved (proven by `test_oi_coverage_absent_is_not_emitted_so_committed_hash_is_stable`
and the existing `test_committed_manifest_loads_and_hash_matches_meta`).

## Deterministic readiness verdict

`classify_feature_readiness(ReadinessInputs, ReadinessThresholds)` → `("ready", [])` or
`("needs_more_data", [reasons])`, mirroring ROB-355's `classify_verdict`. Default thresholds:

| Threshold | Default | Rationale |
|---|---|---|
| `min_usable_symbols` | 20 | enough cross-section for a crowding panel |
| `min_delisted_usable` | 3 | survivorship must be exercised, not just claimed |
| `min_oi_window_rows` | 500 | per-symbol OI history long enough to be meaningful |
| `max_missingness` | 0.05 | worst per-symbol day-level OI gap fraction (`1 − oi_coverage`) |

Plus a hard gate: **every** attempted delisted symbol must be `survivorship_ok` (its metrics
archive reaches its last active day, `delisted_at − 1`). Below any threshold → the builder
prints `needs_more_data` with reasons and **does not** recommend opening a backtest issue.

## Operator RUN

```bash
cd research/nautilus_scalping
# dry (no network): prints what would run
uv run --no-project python build_funding_oi_features.py --limit 40
# probe only (read-only network; prints the verdict, writes NOTHING):
uv run --no-project python build_funding_oi_features.py --run --limit 40
# probe + persist artifacts (read-only network; writes only to gitignored results/):
uv run --no-project python build_funding_oi_features.py --run --limit 40 --out
```

`--out` gates **all** disk writes. `--run` alone performs the read-only network RUN and prints
the readiness verdict but writes nothing. Adding `--out` additionally writes per-symbol feature
CSVs under `results/discovery/rob356/features/` and the coverage summary
`results/discovery/rob356/funding_oi_coverage.v1.json` (coverage + verdict). All gitignored —
the RUN downloads each symbol's full monthly `fundingRate` + daily `metrics` history (a daily
archive per OI day — thousands of small files for long-lived symbols) but never commits raw data.

## Validation evidence (bounded real-data smoke, `2026-05-29`)

A bounded smoke (read-only, nothing persisted) on the **delisted** symbol `EOSUSDT`:

- funding `2024-01`: 93 rows parsed; `calc_time` epoch-ms, `funding_interval_hours=8`,
  realized `last_funding_rate` present.
- metrics `2024-01-15`: **576 raw rows → 288 after dedup** (clean 5-min grid; confirms the
  duplicate-row observation and the `(symbol, create_time)` dedup).
- `build_features` produced 288 rows; `oi_delta` computed; funding as-of attached
  (`last_funding_rate`, `funding_interval_hours`).
- **future-leakage rows = 0** (no attached funding `calc_time` exceeded its OI `ts`) — the
  known-after rule holds on real data.

This proves construction is clean and reusable. The **at-scale** coverage numbers and the
final `ready` / `needs_more_data` verdict require the operator RUN above (the full
multi-thousand-file download is operator-scale and intentionally not run in CI).

## Recommendation (open a bounded funding-OI event backtest issue next?)

**Conditional — gated on the operator RUN's deterministic verdict.**

- Construction is proven clean, PIT-safe, and survivorship-aware; ROB-355 already ruled the
  funding+OI data `feasible` with delisted coverage. The expected RUN outcome is `ready`.
- **Open a bounded funding-OI crowding/deleveraging event backtest issue iff** the full RUN
  returns `verdict == "ready"`. That issue would still be cost-blind-first and must clear a
  gross-edge screen before any cost/OOS gauntlet — consistent with ROB-351/353.
- **If the RUN returns `needs_more_data`**, stop: do not open the backtest issue; address the
  named coverage gaps (symbols / OI window / survivorship / missingness) first.
- [ROB-343](https://linear.app/mgh3326/issue/ROB-343) stays deferred regardless — it is only
  justified once a real candidate shows positive gross edge and is materially cost-sensitive.

## Tests

```bash
cd research/nautilus_scalping
uv run --no-project --with pytest python -m pytest \
  tests/test_funding_oi_archive.py tests/test_funding_oi_features.py \
  tests/test_funding_oi_readiness.py tests/test_pit_universe.py -q
```

Covers: funding/metrics parsing incl. interval-change and dup-dedup; delist trim (exclusive);
OI-start bounding; backward as-of join with no future leakage; per-row interval carry;
known-after; OI delta/pct/z (zero-variance bounded); additive `oi_coverage` round-trip
(committed hash preserved); and the deterministic readiness verdict.

> **Note on `ruff`:** all ROB-356 files pass both `ruff check` and `ruff format --check`
> (project-pinned ruff 0.15.9). Note that `research/nautilus_scalping/` as a whole is outside
> the CI ruff scope — `make lint` gates `app/` + root `tests/` only — so most sibling research
> files are not `ruff format`-clean; the ROB-356 files are formatted regardless to satisfy the
> acceptance criteria.
