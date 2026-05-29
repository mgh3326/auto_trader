# ROB-353 PR2 — empirical 1d verdict (families 1–3)

**Status:** RUN executed 2026-05-29. Universe filter 539 strict-perp; effective panel **37 USDT perps** (≥30 daily bars in window). Research/backtest only.

> Reproduce: stage 1d klines under `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT/data/klines/1d/<SYM>/` then
> `uv run --no-project python run_rob353_campaign.py --from-month 2023-01 --to-month 2026-04 --skip-fetch`
> (or omit `--skip-fetch` to fetch the full 539-symbol universe from data.binance.vision).

## Verdict (all families reject)

| family | screen | gross expectancy | gate | label_343 |
|---|---|---|---|---|
| family1_breakout_continuation | **screened_out** | OOS gross **−70.99 bps** ≤ 0 (in-sample edge does not hold OOS) | — | — |
| family2_ts_trend_basket | **screened_out** | gross **−27.53 bps** ≤ 0.50 bps floor | — | — |
| family3_xs_momentum | **screened_out** | gross **−39.38 bps** ≤ 0.50 bps floor | — | — |

All three families are **screened out on GROSS expectancy** — they have no economically meaningful edge *before* fees, so none reaches the conservative gate or a ROB-343 cost-binding label. This is a stronger negative than a cost-bound result: fees are not the bottleneck; there is no gross signal to begin with.

Per-family verdict (ROB-351/343 label space): **reject** for all three (`screen=screened_out`, no gate verdict, no `cost_binding_343_candidate`). Nothing reaches `promote_to_pilot` or `cost_binding_343_candidate`.

## Data source & retrieval
- Source: `data.binance.vision` USDⓈ-M futures (`futures/um`), public 1d klines, read-only, no keys.
- This RUN reused the already-downloaded ROB-349 scope-4 1d panel (39 symbols staged: 23 continuous-since-2023 survivors + 16 in-window delisted perps) — real public klines, the survivorship-corrected panel ROB-349 used. The harness fetcher (`pit_klines_fetcher`) can fetch the full universe; here `--skip-fetch` used the staged data.

## Window & interval
- Window: **2023-01 .. 2026-04** (matches ROB-349/342 so results are comparable). Interval: **1d**.

## Universe definition
- Authority: `data_manifests/pit_universe.v1.json` (843 symbols; metadata only) → `strict_usdt_perp()` = 552 → `campaign_controls.filter_universe` (active-in-window ∩ kline_coverage ≥ 0.8 ∩ confidence ∈ {high, medium}) = **539 symbols**.
- Effective panel = the staged 37 symbols with ≥30 in-window daily bars. **2 staged symbols dropped** (`BTSUSDT`, `SRMUSDT` — early-delisted, <30 bars in window).
- Active **+ delisted** symbols included (survivorship fix). Exclusions (via `strict_usdt_perp`): settling, BUSD/USDC-quoted, dated/quarterly (`_`), `*SETTLED`.
- PIT membership: each symbol contributes only over `[listed_from, delisted_at)` (epoch ms, exclusive); post-delist freeze tail trimmed (`pit_bars`).
- PIT manifest: `data_manifests/pit_universe.v1.json`, snapshot_hash `e715de33042e9b897ecc43e016ad4b651bbbf58c522fa8e14f1c762593a16cf6`.

## Frozen config (ex-ante)
- config_hash **`8f02dffd51dc5bedf5ab4c1521edb2185f4768304b5b60fa7dd0836ef8872adf`** (unchanged — the RUN asserts it). taker_bps 4.0; economic_triviality_floor_bps 0.5; fee_grid [10,7.5,5,2,0].
- Family params frozen to ROB-351 defaults: breakout lookback 20 / hold 5; ts-trend lookback 20; xs-momentum lookback 20 / top_k 1 / weekly (7d) rebalance; notional 1000. (Note: these differ from ROB-349's hand-tuned quartile L/S L=60 — this is the funnel's ex-ante config, not a re-tune. The negative result is even cleaner here: family 3 has no gross edge under the frozen params, vs ROB-349's gross edge that collapsed under survivorship correction.)

## Controls
- **Gross vs net:** all families screened out on gross — net is moot. Sample counts: breakout 1366 trades, ts-trend 58 periods, xs-momentum 172 periods.
- **OOS:** train ≤ 2025-01-01 / test > 2025-01-01 split in the summaries; family 1's OOS gross (−70.99 bps) is what screens it out (in-sample did not hold).
- **Baselines:** BTC buy&hold over the window **+35,938 bps (+359%)**; cash 0 bps. Every family is far below both — a passive long-BTC or cash stance dominates all three.
- **Drawdown (net equity, bps):** family2_ts_trend_basket **−2,881 bps**; family3_xs_momentum **−15,811 bps** (family1 is a pooled-trade series, not a portfolio equity curve).
- **Cost-stress:** moot — no family cleared the gross screen, so breakeven taker fee is not computed.

### Skipped controls (disclosed — a thin run must not pass quietly)
- Dollar-volume liquidity filter (used manifest coverage/confidence quality gate instead).
- Parameter-neighborhood sweep.
- BTC regime/period split.
- Symbol-concentration analysis.
- 1h interval (deferred — fetcher supports it).
- **Universe coverage:** ran the bounded 37-symbol ROB-349 panel, not the full 539-symbol strict universe. A full-universe RUN is a follow-up; given all families fail on gross even on this vetted panel, full-universe is unlikely to reverse the sign (consistent with ROB-349's larger-panel finding).

## Branch recommendation
All three families are **reject** (no gross edge). Per the ROB-353/351 branch policy:

→ **Move to family 4/5 feasibility: funding-rate / open-interest / liquidation crypto-native edges** (parked in `TODOS.md`), gated first on confirming usable historical data quality (OI history ~30d from the API; liquidation history scarce — needs a durable archival source before any strategy claim).

This closes the generic trend/momentum/reversal line for short-horizon crypto (joins ROB-316 / 320 / 324 / 339 / 342 / 349). Do **not** open a pilot-design issue (nothing reached `promote_to_pilot`); do **not** open a ROB-343 probe (nothing reached `cost_binding_343_candidate` — fees are not the bottleneck); do **not** enable any automation.

## Safety boundary confirmation
Research/backtest only. Read-only public data. No live, no Demo `confirm=true`, no broker/order/watch/order-intent mutation, no scheduler/TaskIQ/Prefect/cron/daemon, no production DB/env/secret mutation, no `/invest` exposure. No raw market data committed (the verdict JSON lives at gitignored `results/rob353/rob351_campaign.v1.json`; only this report is committed). Canonical `validated` is NOT used. ROB-343 is recommended-not-run; it was not implemented or run here.
