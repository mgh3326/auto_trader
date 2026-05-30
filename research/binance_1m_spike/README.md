# Binance USD-M 1m gross-edge spike

A **bounded, reproducible falsification probe**, not a strategy and not a
validator. It answers one cheap question before any heavier backtest work:

> On a 1-minute timeframe, does a canonical z-score mean-reversion *fade* have any
> **GROSS** per-trade edge (before fees) above the economic-triviality floor, on a
> tiny bounded sample of Binance USD-M futures data?

## Why this shape

The repo has already concluded that trend / momentum / reversal scalping is
**net-negative, and mostly gross-negative**, on this venue:

- `docs/runbooks/rob-353-pr2-empirical-verdict.md` — families 1/2/3 all
  `screened_out` on **gross** (−70.99 / −27.53 / −39.38 bps), before fees.
- `docs/runbooks/binance-demo-scalping.md` — ROB-316 trend micro-breakout
  net-negative at realistic fees and gross-negative out-of-sample.

So the honest job here is **falsification on the cheapest possible path**, not a
search for profit. The full validator (walk-forward, bootstrap CI, MC
permutation, maker-fill model) already exists in `research/nautilus_scalping/`
and requires a NautilusTrader build; this spike intentionally avoids that and
runs under a bare `python3`.

## What it does

1. `binance_1m_data.py` — read-only download of one `SYMBOL-1m-YYYY-MM.zip` from
   the public `data.binance.vision` USD-M archive → close prices. (The existing
   `nautilus_scalping/pit_klines_fetcher.py` only supports 1d/1h; this adds the
   1m path in isolation.)
2. `meanrev_probe.py` — rolling z-score; **non-overlapping** fade trades (open,
   hold N bars, close, skip past exit) → independent gross per-trade returns.
3. `fees.py` — frozen Binance USD-M Demo envelope (taker 4.0 / maker 2.0 bps per
   leg; gross floor 0.5 bps), mirroring `nautilus_scalping/frozen_config.py`.
4. `run.py` — fetch → probe → emit a **counts-only** JSON artifact.

## Run

```bash
cd research/binance_1m_spike
python3 run.py --out          # default: 2026-04, XRPUSDT + BTCUSDT, one param set
python3 test_spike.py         # pure unit tests, no network
```

`data/` and `results/` are gitignored — no raw bars or result dumps are committed.

## Verdict space (mirrors the funnel labels)

- `needs_more_data` — fewer than 30 trades; sample can't support a read.
- `screened_out_gross` — mean gross ≤ 0.5 bps floor; no edge even before fees → reject.
- `gross_edge_present_needs_full_validation` — gross edge above the floor; this
  spike does **not** validate it (net + statistics are the Nautilus gate's job).

## Hard non-goals / stop conditions

- No parameter sweep (one fixed param set).
- No profitability claim from a tiny sample.
- No live execution; no order mutation; read-only public data only.
- `net_*` columns subtract a flat round-trip fee for **context only** — a real
  net verdict requires the full gate.
- BTCUSDT is **data-only**: it is excluded from the futures-demo execution
  allowlist (`app/services/brokers/binance/futures_demo/sizing.py`), so its
  result does not map to anything the demo loop could trade. XRPUSDT is the demo
  default.
