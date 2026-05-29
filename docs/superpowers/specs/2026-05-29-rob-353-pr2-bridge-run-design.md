# ROB-353 PR2 — campaign bridge + bounded empirical RUN + verdict report design

**Status:** approved (brainstorming, 2026-05-29)
**Issue:** ROB-353. **PR2 of 2** (stacked on PR1 branch `rob-353` / PR #995; PR2 branch `rob-353-pr2`, base `rob-353`; rebase `--onto origin/main` after PR1 squash-merges — stacked-squash gotcha noted).
**Boundary:** research/backtest only. Read-only PUBLIC data. No live, no Demo `confirm=true`, no broker/order/watch/scheduler/DB mutation, no `/invest` exposure, no raw large data committed, no credential logging. ROB-343 only RECOMMENDED, never run. Never label canonical `validated`.

## Background

PR1 landed the PIT data layer (`pit_universe` extended + `pit_universe.v1.json` manifest + `pit_klines_fetcher` + `pit_bars` + builder). PR #993 (ROB-351) landed the funnel: `campaign.run_campaign(specs, FROZEN_CONFIG)` consumes a list of family **specs** and emits the `rob351_campaign.v1` verdict table. The missing piece is the **bridge** that turns real PIT bars into those specs, plus the actual **bounded empirical RUN** that produces a durable verdict.

ROB-349 already ran the cross-sectional family (≈ family 3) on the PIT-corrected panel → reject/needs_more_data. PR2 re-confirms family 3 through the official funnel and runs the untested families 1 (breakout continuation) and 2 (time-series trend basket). The honest expectation is mostly reject/needs_more_data → recommend family 4/5; the deliverable is a durable, auditable verdict either way.

## Contracts already in the repo (consumed, NOT modified)

- `families.breakout_continuation_trades(bars: Sequence[Bar], lookback=20, hold=5, notional=1000, ref_fee_bps) -> list[Trade]` (family 1, single symbol).
- `families.ts_trend_basket_periods(closes_by_symbol: dict[str, Seq[(ts,close)]], lookback=20, notional=1000, ref_fee_bps) -> list[PortfolioPeriod]` (family 2).
- `families.xs_momentum_periods(closes_by_symbol, rebalances: Seq[int], lookback=20, top_k=1, notional=1000, ref_fee_bps, manifest, min_seasoning) -> list[PortfolioPeriod]` (family 3, PIT-aware).
- `campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)` where each spec is `{"name": str, "summary": HypothesisSummary, "kind": "trade"|"portfolio", "data": list[Trade]|list[PortfolioPeriod], "maker_conservative_net": float|None}`.
- `discovery.screen.HypothesisSummary(name, conditions, sample_count, gross_expectancy_bps, fee_adjusted_bps, oos_fee_adjusted_bps=None, oos_gross_bps=None, ...)`.
- `pit_bars.load_bars(symbol, interval, manifest, root)` and `pit_bars.load_panel(symbols, interval, manifest, root)`.
- `pit_universe.PITManifest.load(...).strict_usdt_perp()`.
- `frozen_config.FROZEN_CONFIG` (config_hash `8f02dffd…` — MUST stay unchanged; PR2 touches no funnel/gate/config code).

## Goals / non-goals

**Goals**
- A pure, tested **bridge** (`campaign_specs.py`): real PIT bars/panel → the three family specs (with `HypothesisSummary` derived from the family output, frozen params).
- A **RUN harness** (`run_rob353_campaign.py`): bounded real RUN (1d, window 2023-01..2026-04, strict_usdt_perp ∩ window-active) → `run_campaign` → controls → durable report. `--self-test` (synthetic, no network) proves wiring.
- A committed **verdict report** (markdown embedding the `rob351_campaign.v1` table + config hash + data/universe definition + controls + skipped-control disclosure + per-family verdict + branch recommendation).

**Non-goals**
- 1h interval RUN (deferred; fetcher already supports it).
- parameter-neighborhood sweep, regime split, symbol-concentration robustness (explicitly **skipped this RUN, disclosed with reason** in the report — satisfies the "thin run cannot quietly pass" AC).
- Any change to `families.py`, `campaign.py`, `validated_gate.py`, `rob343_label.py`, `frozen_config.py`, `panel.py`.
- Implementing/running ROB-343.

## Components (all `research/nautilus_scalping/`, pure stdlib + research venv)

### 1. `campaign_specs.py` (pure, tested)
Bridge real data → family specs. Frozen params match PR1 self-test / family defaults (ex-ante; recorded in the report).
- `_summary_from_trades(name, trades, oos_split_ts) -> HypothesisSummary`: `sample_count=len(trades)`; `gross_expectancy_bps` = mean per-trade gross return in bps (gross = `net_ref_pnl + commission_ref`, normalized by notional → bps); `fee_adjusted_bps` = gross minus round-trip ref-fee budget (use `cost_model`/`REF_FEE_BPS`); split by `ts_opened <= oos_split_ts` (in-sample) vs `>` (OOS) to fill `oos_gross_bps`/`oos_fee_adjusted_bps`.
- `_summary_from_periods(name, periods, oos_split_ts) -> HypothesisSummary`: analogous over `PortfolioPeriod` (gross = `gross_ref_pnl + commission_ref`), bps normalized by the period notional.
- `breakout_spec(panel, oos_split_ts, ...) -> dict`: run `breakout_continuation_trades` on EACH symbol's bars, pool the Trade lists, build summary → `{name:"family1_breakout_continuation", kind:"trade", data:pooled, summary, maker_conservative_net:None}`.
- `ts_trend_spec(panel, oos_split_ts, ...) -> dict`: `ts_trend_basket_periods(panel)` → `{name:"family2_ts_trend_basket", kind:"portfolio", ...}`.
- `xs_momentum_spec(panel, rebalances, manifest, oos_split_ts, ...) -> dict`: `xs_momentum_periods(panel, rebalances, manifest=manifest)` → `{name:"family3_xs_momentum", kind:"portfolio", ...}`.
- `OOS_SPLIT_TS` constant = epoch ms of `2025-01-01` (matches ROB-349 train/test boundary).

### 2. `run_rob353_campaign.py` (RUN harness)
- `--self-test`: build the three specs from tiny SYNTHETIC panels (no network), call `run_campaign`, print the verdict table + config_hash. Proves wiring; mirrors `run_rob351_campaign.py --self-test`.
- default (real RUN, operator/network): 
  1. `manifest = PITManifest.load("data_manifests/pit_universe.v1.json").strict_usdt_perp()`; restrict to symbols active within `[2023-01, 2026-04]` (manifest listing overlaps window).
  2. ensure 1d klines present via `pit_klines_fetcher.fetch_months` for each symbol (idempotent; writes gitignored root). Liquidity filter: drop symbols whose median daily quote-volume over the window `< MIN_MEDIAN_QUOTE_VOL` (documented constant); record dropped count.
  3. `panel = pit_bars.load_panel(kept_symbols, "1d", manifest)`; build `rebalances` = weekly (R=7d) timestamps over the window (ROB-349 cadence).
  4. specs = `[breakout_spec(panel,...), ts_trend_spec(panel,...), xs_momentum_spec(panel, rebalances, manifest,...)]`.
  5. `result = campaign.run_campaign(specs, FROZEN_CONFIG)`; assert `result["config_hash"] == FROZEN_CONFIG` hash.
  6. controls: gross vs net + turnover/trade-count (from gate report), OOS in/out split (already in summaries), BTC buy&hold + cash baselines over the window, cost-stress (breakeven taker bps already emitted per family), max drawdown of each portfolio series.
  7. write live verdict JSON → `results/rob353/rob351_campaign.v1.json` (gitignored) and print a report-ready summary; the committed report (component 3) is authored from this output.
  - flags: `--from-month`/`--to-month` (default 2023-01/2026-04), `--max-symbols` (operator bound), `--skip-fetch` (use already-downloaded data).

### 3. Committed verdict report (markdown)
`docs/runbooks/rob-353-pr2-empirical-verdict.md` — the durable artifact. Sections: data source/retrieval, window, interval (1d), universe definition (strict_usdt_perp ∩ window, active+delisted, exclusions, liquidity filter + threshold + dropped count), PIT manifest path + snapshot hash, the embedded `rob351_campaign.v1` verdict table (JSON fence), gross/net + turnover + drawdown + OOS per family, BTC b&h / cash baselines, cost-stress (breakeven) per family, **explicitly enumerated skipped controls (parameter neighborhood, regime split, symbol concentration) and why**, per-family verdict (reject/needs_more_data/promote_to_pilot/cost_binding_343_candidate), and the branch recommendation. Authored from the RUN output; if the operator RUN is not executed in-session, the report records a `data-precondition`/`run-not-executed` status with the exact command to produce it (no fabricated numbers).

### 4. Tests
- `test_campaign_specs.py`: synthetic panels → each spec has correct `kind`/`name`/`data` type; `_summary_from_*` computes gross/fee-adjusted/oos bps correctly incl. the train/test split; pooled breakout trades across symbols; empty-panel safety.
- `run_rob353_campaign.py --self-test`: synthetic, no network, prints `rob351_campaign.v1` + unchanged config_hash.
- extend `test_pit_data_layer_guard.py`: add `campaign_specs.py`, `run_rob353_campaign.py` to the no-`app.*` guard list.

## Data flow
```
pit_universe.v1.json → strict_usdt_perp ∩ [2023-01,2026-04] → pit_klines_fetcher (1d) → pit_bars.load_panel
   → campaign_specs.{breakout,ts_trend,xs_momentum}_spec → campaign.run_campaign(FROZEN_CONFIG)
   → verdict table + controls → results/rob353/rob351_campaign.v1.json (gitignored)
   → docs/runbooks/rob-353-pr2-empirical-verdict.md (committed durable report)
```

## Safety / boundaries
- No raw klines committed (gitignored). Only the markdown report (with embedded small verdict table) is committed.
- Research-local `os.environ` only; zero `app.*` (guard-tested, extended).
- Frozen config untouched; RUN asserts the config_hash is unchanged.
- Network limited to public `data.binance.vision`, read-only. No broker/order/scheduler/DB.
- Verdict labels limited to reject / needs_more_data / promote_to_pilot / cost_binding_343_candidate. ROB-343 recommended only.

## Acceptance (PR2)
- `campaign_specs.py` + tests committed; `run_rob353_campaign.py --self-test` passes under py3.13, config_hash unchanged.
- Bounded real RUN executed on 1d strict-perp∩window data (or report records an explicit blocker with the exact rerun command — no fabricated numbers).
- Committed report includes verdict table, data/universe definition, controls, **skipped-control disclosure**, per-family verdict, branch recommendation, safety confirmation.
- Branch recommendation explicit: pilot design / ROB-343 probe / family 4-5 feasibility / stop as needs_more_data.
- Guard + gitignore confirm no `app.*` and no raw-data/secret leakage. Research suite green (`--ignore=tests/test_signal_parity.py`).

## Expectations (honesty)
family 3 ≈ ROB-349 reject/needs_more_data; families 1-2 likely net-negative (ROB-316/320/324/339/342 line) → likely all reject/needs_more_data → recommend family 4/5 (funding/OI/liquidation, TODOS.md). Goal is the honest durable verdict, not a forced promote.
