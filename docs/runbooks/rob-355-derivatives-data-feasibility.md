# ROB-355 — Binance USD-M family 4/5 derivatives data feasibility audit

Research/feasibility only. Read-only PUBLIC data (`data.binance.vision` S3 listing + a few
sample files). **No strategy, no backtest, no broker/order/watch/order-intent mutation, no
Demo `confirm=true`, no live/mainnet endpoint, no scheduler/TaskIQ/Prefect/cron/daemon, no
production DB write, no raw large market data committed, no secrets.**

This audit decides whether the next research family — **funding + open-interest (OI)
crowding / deleveraging events** — can be built on point-in-time (PIT), survivorship-safe
data *before* any feature-builder or backtest issue is opened. It does **not** look for a
profitable strategy and does **not** run a parameter sweep (those are explicit non-goals).

## Why this issue exists (context)

ROB-353 ran the empirical Binance USD-M PIT campaign for ROB-351 families 1–3
(`breakout_continuation`, `ts_trend_basket`, `xs_momentum`). All three were **screened out
on gross expectancy** — the failure mode was **signal/edge weakness, not fees/slippage**.
That closes the generic price-action (trend / momentum / breakout) line. The next plausible
direction is **derivatives-native**: positioning crowding and forced-flow / deleveraging,
which generic OHLCV-only families cannot express. This issue audits the data for that line.

## Method & reproducibility

- Source: `data.binance.vision` USDⓈ-M futures (`futures/um`), public, no keys, read-only.
- Tool: `research/nautilus_scalping/audit_derivatives_feasibility.py` — an **operator-gated**
  read-only network probe that reuses the `build_pit_universe.py` S3-listing pattern. It
  measures archive coverage (path existence, first/last date, file counts, sampled
  columns/granularity) and **never persists raw market data** — only a coverage summary
  (counts / date-ranges / column names). CI exercises only the pure helpers; the network
  RUN is operator-invoked.
- Tiny PIT probe (survivorship proof, **not** a backtest): a curated mix of **live** majors
  and **delisted/dead** symbols drawn from the committed ROB-349/353 manifest
  (`data_manifests/pit_universe.v1.json`, 552 strict-USDT-perp: 524 live + 28 dead).
- Probe RUN: `2026-05-29`. Sampled date ranges below are as observed on that date.

      # operator (read-only, public, no keys):
      cd research/nautilus_scalping
      uv run --no-project python audit_derivatives_feasibility.py        # prints coverage table
      uv run --no-project python audit_derivatives_feasibility.py --out  # + writes summary JSON to gitignored results/ (not committed)

## Coverage table

`data.binance.vision/futures/um` data types actually present (probed `2026-05-29`):
`monthly/{aggTrades, bookTicker, fundingRate, indexPriceKlines, klines, markPriceKlines,
premiumIndexKlines, trades}` and `daily/{aggTrades, bookDepth, bookTicker, indexPriceKlines,
klines, metrics, markPriceKlines, premiumIndexKlines, trades}`. **There is no
`liquidationSnapshot` (or any liquidation/forceOrder) prefix** in either granularity.

| Family evidence | Archive path | Granularity | History start (sampled) | Delisted/dead coverage | Timestamp / known-after | Missingness | Source / license |
|---|---|---|---|---|---|---|---|
| **Funding rate** | `monthly/fundingRate/<sym>/` | 8h (3/day, 00/08/16 UTC) | 2020-01 (BTC/ETH/XRP/EOS); per-symbol from listing | **Yes** — MATIC/RNDR/EOS/GAL all present through active life | `calc_time` (epoch ms) = settlement instant; realized `last_funding_rate` known **at** `calc_time` | None seen on majors; per-symbol funding starts at listing | Binance public; redistribution-limited — do not commit raw |
| **Open interest + positioning** | `daily/metrics/<sym>/` | intraday (sampled BTC 2020-09-01 = 576 rows ≈ 2.5 min; Binance documents 5 min — early granularity may differ) | 2020-09 (BTC); per-symbol later (XRP/MATIC/EOS 2021-12, RNDR 2023-02, GAL 2022-05) | **Yes** — delisted symbols carry metrics through (and past) delisting | `create_time` (`YYYY-MM-DD HH:MM:SS`, UTC) snapshot instant; OI is a state reading, known at `create_time` | Per-symbol start **later** than funding/klines; post-delist frozen tail present | Binance public; redistribution-limited — do not commit raw |
| **Liquidation / forced-flow** | **(absent)** | — | — | — | — | **No archive** | Binance no longer publishes a historical liquidation dump; live `forceOrder` WS is throttled (≤1/s/symbol → severe undercount) |
| **Trade flow (taker)** | `monthly/aggTrades/<sym>/`, `monthly/trades/<sym>/` | per-trade | 2020-01 (BTC) | Expected yes (same archive family as klines) | trade `time` (epoch ms); `is_buyer_maker` → taker side | None seen on majors | Binance public; redistribution-limited — do not commit raw |
| **Depth / order book** | `daily/bookDepth/<sym>/`, `daily/bookTicker/<sym>/` | bookDepth ≈ sub-minute snapshots of **percentage-band** depth (`±1..5%`), NOT L2; bookTicker = top-of-book updates | bookDepth 2023-01; bookTicker later than klines | partial (newer streams) | snapshot `timestamp` (UTC) | **No tick-level L2 book** in archive; banded depth only, and only from 2023 | Binance public; redistribution-limited — do not commit raw |

Sampled columns (for schema design, below):

- `fundingRate`: `calc_time, funding_interval_hours, last_funding_rate` — **no `mark_price`** in
  the funding file (join `markPriceKlines` if needed).
- `metrics`: `create_time, symbol, sum_open_interest, sum_open_interest_value,
  count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio,
  sum_taker_long_short_vol_ratio` — i.e. OI value **and** ready-made crowding/positioning
  metrics in the same file.
- `bookDepth`: `timestamp, percentage, depth, notional` — depth at fixed percentage offsets
  from mid, not a raw order book.

## PIT-join feasibility (vs ROB-349/353 universe)

- Both funding (`calc_time`) and OI (`create_time`) are timestamped instants → epoch-ms join
  against `PITManifest.universe_as_of(ts)` and the per-symbol `[listed_from, delisted_at)`
  window, exactly like `pit_bars` does for klines.
- **Survivorship is safe**: the delisted-symbol probe (MATIC delisted 2024-09 → OI to
  2025-01, funding to 2026-04; RNDR delisted 2024-07 → OI to 2025-06; EOS, GAL similar)
  confirms funding **and** OI exist for dead symbols across their active life. A
  funding+OI panel built on the ROB-349 manifest will **not** be biased to survivors.
- **Required cleaning (lookahead/cleanliness):** the OI `metrics` and `fundingRate` archives
  keep emitting **stale/frozen rows after a symbol delists** (same freeze-tail as klines).
  These MUST be trimmed at `delisted_at` (exclusive) via the manifest, reusing the
  `pit_bars` zero/flat-tail trim approach. Untrimmed tails would inject post-delisting
  phantom state.
- **Panel bound:** per-symbol OI starts **later** than funding/klines (e.g. BTC OI 2020-09 vs
  funding 2020-01; many alts OI from 2021-12). The funding+OI panel is therefore bounded by
  the OI start per symbol — a modest reduction, not a blocker.

## Lookahead risk — explicit

- **Funding known-after (primary risk):** the archived `last_funding_rate` keyed by
  `calc_time` is the **realized** rate applied at that 8h settlement. A decision at time `t`
  may consume only settlements with `calc_time <= t`. Do **not** confuse it with the
  continuously-updating **predicted/current** funding (premium index, `premiumIndexKlines`),
  which is forward-looking and a different series. Funding cashflow can be reconstructed
  without lookahead by summing realized settlements strictly before the decision instant.
- **OI is a state reading**, known at `create_time`; safe if consumed only for `ts <= create_time`.
- **Post-delist frozen tails** (above) are the other concrete lookahead trap — trim them.

## Anti-proxy guardrails honored

- **OI is NOT proxied** from OHLCV / volume / wicks. The verdict rests on the actual
  `daily/metrics` `sum_open_interest[_value]` archive; if that archive were absent the
  verdict would have stopped at `needs_vendor_data`, not fallen back to a price proxy.
- **Liquidation heatmaps/models are NOT treated as raw evidence.** No raw exchange
  liquidation dump exists in the archive; vendor aggregates (e.g. Coinglass) are estimated /
  aggregated and are labeled exploratory-only, never as backtest-grade evidence.
- **Liquidity sweep is NOT validated from OHLC wicks alone.** The `partial` verdict is granted
  only because `aggTrades`/`trades` (taker flow) and banded `bookDepth` exist as
  trade/depth evidence; wick-only validation remains out of scope.

## Final verdict — per future family

| Future family | Verdict | Basis |
|---|---|---|
| **Funding-only baseline** | `feasible` *(baseline/control only)* | Monthly funding archive, 2020-01+, delisted covered, lookahead-safe. **Not** promoted to a candidate on its own — funding-only carry is a well-arbitraged, small effect and serves as the baseline/control. |
| **Funding + OI crowding / deleveraging** *(primary hypothesis)* | **`feasible`** | `daily/metrics` provides `sum_open_interest`, `sum_open_interest_value`, and built-in long/short & taker ratios at intraday granularity, with delisted-symbol coverage → PIT- and survivorship-safe. Supports crowding (funding z-score/percentile + OI level/Δ + OI-to-ADV) and deleveraging-event (OI drop on adverse shock) feature construction at 1h/4h. Bounded by per-symbol OI start; requires freeze-tail trim. |
| **Liquidation / forced-flow event** | `needs_vendor_data` | No raw liquidation archive on `data.binance.vision`; live `forceOrder` stream is throttled (undercount). Raw/complete historical liquidation prints would require vendor data of unverified completeness. **Stop here** — do not substitute estimated heatmaps. |
| **Liquidity sweep (trade/depth evidence)** | `partial` | Trade-flow (`aggTrades`/`trades`, taker side via `is_buyer_maker`) is feasible with long history; depth is **banded** `bookDepth` from 2023-01 only (no tick-level L2). A later sweep family could be probed at coarse depth fidelity but not full order-book reconstruction. |

## Recommendation

1. **Open a follow-up `funding-OI crowding feature builder` issue** (PIT-safe feature artifact
   on the ROB-349 manifest: funding z-score/percentile, realized funding cashflow, OI level,
   OI Δ, OI-to-ADV crowding, top-trader/taker long-short ratios; with freeze-tail trim and
   per-symbol OI-start bounding).
2. **Then a `bounded funding-OI deleveraging event backtest`** (fixed hypotheses, 1h/4h,
   train/validation/OOS, cost stress, with **funding-only carry as the baseline/control**),
   gated by the existing ROB-351/353 cost-blind funnel + ROB-328 `validated_gate` stats.
3. **Liquidation family: do NOT open a backtest issue yet** — park at `needs_vendor_data`
   pending a vetted, completeness-characterized raw/aggregate vendor source.
4. **Liquidity sweep: optional, low priority** — only a `partial` trade/depth probe is
   justified now; defer until the funding+OI line is resolved.
5. **ROB-343 (execution-realism harness) stays deferred** — fees were not the ROB-353
   bottleneck, so a cost-realism probe is premature until a candidate shows positive gross
   edge that is materially cost-sensitive.

## Proposed minimal schemas (for the feasible/partial datasets)

Field names mirror the archived columns so the feature builder can ingest without rename
guesswork. **Not implemented in this issue** (data-feasibility only).

    funding:       symbol, funding_time(=calc_time, epoch ms), funding_rate(=last_funding_rate),
                   funding_interval_hours, [mark_price via markPriceKlines join], known_after(=funding_time),
                   source, coverage_flag
    open_interest: symbol, timestamp(=create_time), sum_open_interest, sum_open_interest_value,
                   long_short_ratio fields (optional, present in metrics), source, coverage_flag
    liquidation:   (deferred — needs_vendor_data) symbol, timestamp, side, long_liq_notional,
                   short_liq_notional, price?, source, is_snapshot_or_aggregate, coverage_flag
    metadata:      source, retrieval_method, manifest_pointer/checksum, date_range, missingness,
                   timezone(UTC), known_after_assumptions, delist/freeze-trim rule, license/redistribution note

### Manifest / schema extension path (additive, not built here)

The committed PIT manifest already carries a per-symbol coverage slot:
`SymbolListing.funding_coverage` (see `pit_universe.py`, `_META_FIELDS`). Adding
`oi_coverage` (and later `liquidation_coverage`) is a **purely additive** change — append the
field(s) to the `SymbolListing` dataclass and to `_META_FIELDS`; `to_records` only emits
non-`None` fields, so existing committed records and the pinned `snapshot_hash` are
unaffected until the manifest is regenerated with the new metric. **This is the recommended
path for the feature-builder issue; it is intentionally NOT implemented here** to keep this
PR data-feasibility-only.

## Boundaries honored (acceptance criteria)

- [x] No strategy implementation / no broad backtest / no runtime strategy params.
- [x] No OHLCV/volume/wick proxy for OI or liquidation.
- [x] No OHLC-wick-only liquidity-sweep validation.
- [x] Liquidation heatmap/model not treated as raw evidence.
- [x] No raw large market data committed (probe persists coverage summary only, to gitignored
      `results/`); no secrets printed/committed.
- [x] No broker/order/watch/order-intent mutation; no Demo `confirm=true`; no live/mainnet
      endpoint; no `/invest` exposure; no scheduler/TaskIQ/Prefect/cron/daemon; no production
      DB write.
