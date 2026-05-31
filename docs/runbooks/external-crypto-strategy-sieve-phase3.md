# External Crypto Strategy Sieve Phase 3 (ROB-383)

Counts-only validation verdicts for the clean-room Phase 3 shortlist. This report
uses the frozen Phase 3 runner output at
`research/nautilus_scalping/results/discovery/rob383/phase3_validation.json`
(gitignored; no raw klines committed).

## Methodology

- Candidates: 5 clean-room OHLCV-bar signals from the frozen shortlist.
- Frozen params: `rob383.phase3.v1`
- Params hash:
  `6f32e3d5284a4451e2529033fa454323778f138d50b8f4ad7fd8eb3000a25290`
- Symbol panel: BTCUSDT, ETHUSDT, XRPUSDT, SOLUSDT, DOGEUSDT.
- Window: 2023-01 through 2024-12.
- Native intervals: 1h for Supertrend, Squeeze, Range Filter, Chandelier; 5m for
  BBRSI.
- Costs: Binance USD-M Demo taker fee, 4.0 bps per leg.
- Fee grid: 10.0, 7.5, 5.0, 2.0, 0.0 bps per leg.
- Gate: `validated_gate.evaluate_gate`, 50/25/25 chronological walk-forward.
- Baselines: canonical micro-breakout and seeded turnover-matched random entry.
- Selection discipline: one frozen param set per candidate, no sweep, no
  validation-best parameter search.

## Verdicts

| candidate | class | verdict | trades | gross net | net @ 4 bps | OOS net @ 4 bps | fee-sweep net PnL (10/7.5/5/2/0 bps) | reason |
|---|---|---:|---:|---:|---:|---:|---|---|
| `freqtrade_supertrend` | reject | not_validated | 1,828 | 799.15 | -663.25 | 2,851.62 | -2,856.85 / -1,942.85 / -1,028.85 / 67.95 / 799.15 | Net-negative at demo taker; edge appears in only one fold. |
| `freqtrade_bbrsi_naive` | shadow_candidate | validated | 6,903 | 7,123.35 | 1,600.95 | 949.50 | -6,682.65 / -3,231.15 / 220.35 / 4,362.15 / 7,123.35 | OOS positive and beats baselines, but validation fold is negative, so not demo-ready. |
| `tv_squeeze_momentum` | research_candidate | not_validated | 2,230 | 3,033.74 | 1,249.74 | 2,268.93 | -1,426.26 / -311.26 / 803.74 / 2,141.74 / 3,033.74 | Gross-positive but failed stability: edge appears in only one fold. Clean-room momentum is simplified from LazyBear linreg to close-SMA. |
| `tv_range_filter` | reject | not_validated | 10,981 | -6,061.61 | -14,846.41 | -5,792.58 | -28,023.61 / -22,533.11 / -17,042.61 / -10,454.01 / -6,061.61 | Gross and net negative; OOS net <= 0; OOS profit factor 0.77; does not beat both baselines. |
| `tv_chandelier_exit` | shadow_candidate | validated | 3,914 | 4,116.30 | 985.10 | 1,645.53 | -3,711.70 / -1,754.70 / 202.30 / 2,550.70 / 4,116.30 | OOS positive and beats baselines, but train fold is negative, so not demo-ready. |

## Phase 4 Strategy-Pack v0 Recommendation

Recommended Binance Demo strategy-pack v0 contains:

- Demo-ready candidates: none.
- Shadow candidates: `freqtrade_bbrsi_naive`, `tv_chandelier_exit`.
- Research-only candidate: `tv_squeeze_momentum`.
- Reject candidates: `freqtrade_supertrend`, `tv_range_filter`.

`freqtrade_bbrsi_naive` and `tv_chandelier_exit` are suitable only for
signal-only / dry-run shadow observation. Neither should place Demo orders or
create order intents from this report. Any future Demo activation needs a
separate operator-approved issue with position limits, kill switches, and daily
review criteria.

`tv_squeeze_momentum` should remain research-only: the net result is positive at
4 bps, but the gate rejected it for single-fold edge concentration, and the
clean-room implementation is explicitly simplified.

Daily retrospective fields required if a shadow/demo candidate is later
activated:

- date, operator, candidate_id, params_version, params_hash
- symbols observed and data coverage/missing-bar summary
- signal counts by symbol and side
- hypothetical entries/exits, blocked signals, and reason codes
- notional/exposure assumptions and fee/slippage assumptions
- realized Demo fills/PnL only if a later approved Demo issue activates trading
- net PnL, expectancy, drawdown, win rate, and fee drag
- baseline comparison for the same day/window
- anomalies, outages, manual overrides, and kill-switch events
- next-day decision: continue shadow, pause, demote to research, or request
  separate Demo approval

## Safety Boundary

This report is not an activation. No live orders, Binance Demo orders, broker
calls, order intents, trade-journal mutations, scheduler/TaskIQ jobs, cron jobs,
prod DB writes, env changes, or secrets are touched. Public Binance kline data is
cached under gitignored research paths. No raw klines or raw dumps are committed.
