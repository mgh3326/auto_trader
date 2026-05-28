# ROB-351 — Cost-blind funnel for surfacing a ROB-343-worthy crypto candidate

Status: **funnel code + tests landed; empirical RUN operator-gated (no market data committed).**
Branch: `rob-351`. Design + eng-review: `~/.gstack/projects/mgh3326-auto_trader/mgh3326-rob-351-design-*.md`.

## Goal (reframed)

Campaign success = produce **≥1 candidate legitimately worth running through ROB-343**
(the deferred execution-realism harness), i.e. a candidate with **positive gross edge**
whose net viability **hinges on closable execution cost** — not "force a profitable result."

## What this PR ships (the funnel CODE)

A pure-stdlib, test-driven funnel in `research/nautilus_scalping/`:

| Stage | Module | Role |
|------|--------|------|
| shared | `cost_model.py` | one analytic `net_at_fee` primitive (3→1 dedup of fee_sweep/validated_gate/compare_strategies) |
| 0 | `pit_universe.py` | PIT manifest (symbol→listed/delisted); single survivorship authority; as-of-each-rebalance |
| 0 | `panel.py` | lazy PIT-aware cross-section generator (memory-bounded; no full time×symbol matrix) |
| 1 | `discovery/screen.py` `classify(cost_blind=…)` | fees=0 gross screen + economic-triviality floor + `cost_binding` flag |
| 1/2 | `families.py` | family 1 breakout-continuation (Trade) / 2 TS-trend-basket / 3 XS-momentum (PortfolioPeriod) |
| 2 | `validated_gate.evaluate_gate_portfolio` | **period-return** drawdown/Sharpe for basket/XS (Issue 1 fix) |
| 2 | `validated_gate` block bootstrap / BH-FDR / effect-size sample gate / turnover-matched baseline | Codex statistical hardening |
| 3 | `rob343_label.py` | `promote_to_pilot` / `cost_binding_343_candidate` / `needs_more_data` / `reject` |
| ex-ante | `frozen_config.py` | thresholds + achievable-execution envelope, hash recorded in the run |
| driver | `campaign.py` + `run_rob351_campaign.py` | wires Stage 1→2→3 into a verdict table |

**Verify the wiring (no data/secrets):** `python run_rob351_campaign.py --self-test` prints a
verdict table demonstrating all three terminal outcomes, including a synthesized
`cost_binding_343_candidate`. 126 pure tests pass (`uv run --no-project --python 3.13 --with pytest -- pytest`).

## Key engineering decisions (from plan-eng-review + Codex outside-voice)

- **~80% of the "Stage 2 gauntlet" already existed** → Stage 2 is **wiring**, not a rebuild.
- **Issue 1 (P1):** `validated_gate`'s per-trade serial equity sum understates a basket's
  drawdown (concurrent positions). `PortfolioPeriod` + `evaluate_gate_portfolio` compute
  drawdown on the **period-return** equity curve — the honest number `promote_to_pilot` /
  the 343 criterion gate on.
- **Issue 3 (P1):** maker closability is decided in the **pure** path (`maker_fill` queue-loss
  /adverse-selection scenario), never via `fee_sweep`'s taker-only linear rescale.
- **Issue 4 (P2):** single `net_at_fee` primitive (3→1), with a REGRESSION test.
- **Codex hardening (absorbed):** block/time bootstrap (not iid), Benjamini-Hochberg FDR
  across all shots, **effect-size/FDR-aware** sample gate (replaces cargo-culted `n≥263`),
  economic-triviality floor (not just sign>0), turnover-matched random baseline,
  as-of-each-rebalance PIT, and a **realistic-path stop-rule** — `cost_binding_343_candidate`
  requires the maker-conservative scenario to be net-positive, so "cost is the blocker"
  alone is not enough.

## Verdict structure (what the operator RUN fills)

The `campaign.run_campaign` artifact is a per-family table:
`screen` (screened_out/needs_more_data/promote_to_full_validation) · `cost_binding_screen` ·
`gate_verdict` · `label_343` · `breakeven_taker_bps` · `config_hash`. `label_343 ∈
{promote_to_pilot, cost_binding_343_candidate, needs_more_data, reject}`. Canonical
`validated` is NOT produced here (owned by the conservative gate).

## NOT done here / blockers

- **Empirical RUN is operator-gated.** No Binance USDⓈ-M market data is committed, so no real
  verdict table exists yet. To run: set `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` to a data root
  (bar OHLCV + a `PITManifest`), build family specs from it, call `campaign.run_campaign`.
- **Families 4–5 (liquidity-sweep, funding/OI/liquidation) are out of scope** (parked).
  Codex argued these are the more crypto-native / execution-coupled families; recorded as the
  leading follow-up in `TODOS.md`. Family 5 is additionally data-blocked (OI ~30d history).
- **ROB-343 stays deferred** — the verdict only *recommends* it (with a quantified execution
  target) when a `cost_binding_343_candidate` survives; it is not implemented here.

## Safety boundary (unchanged)

Research/backtest only. No live, no Demo `confirm=true`, no broker/order/watch/order-intent
mutation, no scheduler/TaskIQ/Prefect/launchd/daemon, no prod DB/env/secret, no `/invest`
exposure, no raw large data committed, no credential logging.
