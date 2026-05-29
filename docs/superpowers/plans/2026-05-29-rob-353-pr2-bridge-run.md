# ROB-353 PR2 — campaign bridge + bounded RUN Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `specs → campaign.run_campaign` bridge and run a bounded, survivorship-safe 1d empirical campaign for families 1–3 on real Binance USDⓈ-M data, producing a durable committed verdict report.

**Architecture:** Two pure, tested modules (`campaign_specs.py` = data→family-specs; `campaign_controls.py` = pure RUN analytics) feed one operator/network RUN harness (`run_rob353_campaign.py`). The harness loads the PR1 PIT manifest + klines, builds specs, calls the frozen `campaign.run_campaign`, computes controls, and writes a verdict JSON; a committed markdown report is authored from that output. No funnel/gate/config code is touched.

**Tech Stack:** Python 3.13, pure stdlib + the research `.venv`, pytest. Run with `uv run --no-project python ...` / `uv run --no-project pytest ...` from inside `research/nautilus_scalping/`. Branch `rob-353-pr2` (stacked on `rob-353`).

**Spec:** `docs/superpowers/specs/2026-05-29-rob-353-pr2-bridge-run-design.md`

**Working dir for ALL commands:** `/Users/mgh3326/work/auto_trader.rob-353/research/nautilus_scalping`. Tests import flat. ruff line-length 88 (E501 not selected); F401 (unused imports) IS enforced — import only what you use, add no spurious `# noqa`.

---

## Repo contracts this plan consumes (do NOT modify these files)

- `families.Bar(ts, high, low, close)`; `families.breakout_continuation_trades(bars, lookback=20, hold=5, notional=1000.0, ref_fee_bps)` → `list[Trade]`; `families.ts_trend_basket_periods(closes_by_symbol, lookback=20, notional=1000.0, ref_fee_bps)` → `list[PortfolioPeriod]`; `families.xs_momentum_periods(closes_by_symbol, rebalances, lookback=20, top_k=1, notional=1000.0, ref_fee_bps, manifest, min_seasoning=0)` → `list[PortfolioPeriod]`.
- `validated_gate.Trade(net_ref_pnl, commission_ref, notional, ts_opened)` — `net_ref_pnl` is ALREADY net at the 2-leg ref fee; true gross = `net_ref_pnl + commission_ref`.
- `validated_gate.PortfolioPeriod(ts, gross_ref_pnl, commission_ref)` — `gross_ref_pnl` is ALREADY net at ref fee; true gross = `gross_ref_pnl + commission_ref`.
- `discovery.screen.HypothesisSummary(name, conditions, sample_count, gross_expectancy_bps, fee_adjusted_bps, oos_fee_adjusted_bps=None, oos_gross_bps=None, ...)`.
- `campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)` → `{"schema_version","config_hash","config","families":[...],"note"}`; each spec = `{"name","summary","kind":"trade"|"portfolio","data","maker_conservative_net"}`.
- `frozen_config.FROZEN_CONFIG` (`.config_hash()` == `8f02dffd…`).
- `pit_bars.load_bars(symbol, interval, manifest, root)`, `pit_bars.load_panel(symbols, interval, manifest, root)`.
- `pit_universe.PITManifest.load(path).strict_usdt_perp()`; each `SymbolListing` has `.symbol, .listed_from, .delisted_at, .status, .kline_coverage, .confidence, .tradeable_at(ts)`.
- `cost_model.REF_FEE_BPS == 10.0`.

`NOTIONAL = 1000.0` (matches family defaults) is the bps normalization base used across the bridge.

---

## File structure

| File | Responsibility | Create/Modify |
|------|----------------|---------------|
| `campaign_specs.py` | data→family specs; `HypothesisSummary` derivation (gross/fee-adjusted/OOS bps) | Create |
| `campaign_controls.py` | pure RUN analytics: weekly rebalance grid, max drawdown, buy&hold bps, universe filter | Create |
| `run_rob353_campaign.py` | RUN harness: `--self-test` (synthetic) + bounded real RUN (network, operator) | Create |
| `tests/test_campaign_specs.py` | summary math + spec builders on synthetic panels | Create |
| `tests/test_campaign_controls.py` | rebalance grid, drawdown, buy&hold, filter | Create |
| `tests/test_pit_data_layer_guard.py` | add the 3 new modules to the no-`app.*` guard | Modify |
| `docs/runbooks/rob-353-pr2-empirical-verdict.md` | committed durable verdict report (authored from RUN) | Create |

**Unit contracts introduced:**
- `campaign_specs.OOS_SPLIT_TS: int`, `NOTIONAL: float`
- `campaign_specs._summary_from_trades(name, trades, oos_split_ts) -> HypothesisSummary`
- `campaign_specs._summary_from_periods(name, periods, oos_split_ts, notional=NOTIONAL) -> HypothesisSummary`
- `campaign_specs.breakout_spec(panel, oos_split_ts=OOS_SPLIT_TS) -> dict`
- `campaign_specs.ts_trend_spec(panel, oos_split_ts=OOS_SPLIT_TS) -> dict`
- `campaign_specs.xs_momentum_spec(panel, rebalances, manifest, oos_split_ts=OOS_SPLIT_TS) -> dict`
- `campaign_controls.weekly_rebalances(lo_ts, hi_ts, step_days=7) -> list[int]`
- `campaign_controls.max_drawdown_bps(period_net_pnls, notional=NOTIONAL) -> float`
- `campaign_controls.buy_hold_bps(close_series) -> float`
- `campaign_controls.filter_universe(manifest, lo_ts, hi_ts, min_coverage=0.8, confidences=("high","medium")) -> list[str]`

---

## Task 0: Baseline

- [ ] **Step 1: Confirm branch + green baseline**

Run: `git branch --show-current` (expect `rob-353-pr2`) and `uv run --no-project pytest -q --ignore=tests/test_signal_parity.py`
Expected: branch `rob-353-pr2`; tests `165 passed, 1 skipped` (PR1 baseline; `test_signal_parity` is a pre-existing nautilus_trader collection error — always `--ignore` it).

---

## Task 1: `campaign_specs` summary derivation

**Files:** Create `campaign_specs.py`; Create `tests/test_campaign_specs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_campaign_specs.py
import campaign_specs
import families
from validated_gate import PortfolioPeriod

DAY = 86_400_000


def test_summary_from_trades_gross_net_and_oos_split():
    # 2 in-sample trades (ts < split) +1% and +0.5% gross; 1 OOS trade (ts > split) -0.2% gross.
    split = 10 * DAY
    trades = [
        families.make_taker_trade(0.01 * 1000.0, 5 * DAY, 1000.0),   # gross +100 bps
        families.make_taker_trade(0.005 * 1000.0, 6 * DAY, 1000.0),  # gross +50 bps
        families.make_taker_trade(-0.002 * 1000.0, 20 * DAY, 1000.0),  # OOS gross -20 bps
    ]
    s = campaign_specs._summary_from_trades("f1", trades, split)
    assert s.sample_count == 3
    assert round(s.gross_expectancy_bps, 6) == round((100 + 50 - 20) / 3, 6)
    # fee_adjusted = mean net bps; each trade net = gross - 2*REF_FEE_BPS (=20bps round trip)
    assert round(s.fee_adjusted_bps, 6) == round(((100 - 20) + (50 - 20) + (-20 - 20)) / 3, 6)
    # OOS uses only the ts>split trade
    assert round(s.oos_gross_bps, 6) == -20.0
    assert round(s.oos_fee_adjusted_bps, 6) == -40.0


def test_summary_from_periods_uses_notional_bps():
    split = 10 * DAY
    # PortfolioPeriod.gross_ref_pnl is NET; true gross = gross_ref_pnl + commission_ref
    periods = [
        PortfolioPeriod(ts=5 * DAY, gross_ref_pnl=8.0, commission_ref=2.0),    # gross 10 -> 100bps, net 80bps
        PortfolioPeriod(ts=20 * DAY, gross_ref_pnl=-4.0, commission_ref=1.0),  # OOS gross -3 -> -30bps, net -40bps
    ]
    s = campaign_specs._summary_from_periods("f2", periods, split, notional=1000.0)
    assert s.sample_count == 2
    assert round(s.gross_expectancy_bps, 6) == round((100 + (-30)) / 2, 6)
    assert round(s.fee_adjusted_bps, 6) == round((80 + (-40)) / 2, 6)
    assert round(s.oos_gross_bps, 6) == -30.0
    assert round(s.oos_fee_adjusted_bps, 6) == -40.0


def test_summary_empty_is_safe():
    s = campaign_specs._summary_from_trades("f1", [], 0)
    assert s.sample_count == 0
    assert s.gross_expectancy_bps == 0.0 and s.oos_gross_bps is None
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest tests/test_campaign_specs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'campaign_specs'`.

- [ ] **Step 3: Implement summary helpers**

```python
"""ROB-353 (PR2) — bridge real PIT bars/panel into ROB-351 funnel family specs (pure).

Turns the data the PR1 layer produces (``pit_bars.load_bars`` / ``load_panel``) into the
``{name, summary, kind, data, maker_conservative_net}`` specs ``campaign.run_campaign``
consumes. Family params are FROZEN to the ROB-351 defaults (ex-ante; recorded in the
report). No market data is read here — the harness passes already-loaded bars/panels in.
"""
from __future__ import annotations

from collections.abc import Sequence

import cost_model
import families
from discovery.screen import HypothesisSummary
from validated_gate import PortfolioPeriod, Trade

NOTIONAL = 1000.0
OOS_SPLIT_TS = 1_735_689_600_000  # 2025-01-01T00:00:00Z in epoch ms (ROB-349 train/test boundary)
_ROUND_TRIP_FEE_BPS = 2.0 * cost_model.REF_FEE_BPS  # documentation; net is already in the data


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _summary_from_trades(name: str, trades: Sequence[Trade], oos_split_ts: int) -> HypothesisSummary:
    gross = [(t.net_ref_pnl + t.commission_ref) / t.notional * 1e4 for t in trades]
    net = [t.net_ref_pnl / t.notional * 1e4 for t in trades]
    oos_g = [g for g, t in zip(gross, trades, strict=True) if t.ts_opened > oos_split_ts]
    oos_n = [n for n, t in zip(net, trades, strict=True) if t.ts_opened > oos_split_ts]
    return HypothesisSummary(
        name=name, conditions=f"frozen ROB-351 family params; OOS split {oos_split_ts}",
        sample_count=len(trades),
        gross_expectancy_bps=_mean(gross), fee_adjusted_bps=_mean(net),
        oos_gross_bps=(_mean(oos_g) if oos_g else None),
        oos_fee_adjusted_bps=(_mean(oos_n) if oos_n else None),
    )


def _summary_from_periods(name: str, periods: Sequence[PortfolioPeriod], oos_split_ts: int,
                          notional: float = NOTIONAL) -> HypothesisSummary:
    gross = [(p.gross_ref_pnl + p.commission_ref) / notional * 1e4 for p in periods]
    net = [p.gross_ref_pnl / notional * 1e4 for p in periods]
    oos_g = [g for g, p in zip(gross, periods, strict=True) if p.ts > oos_split_ts]
    oos_n = [n for n, p in zip(net, periods, strict=True) if p.ts > oos_split_ts]
    return HypothesisSummary(
        name=name, conditions=f"frozen ROB-351 family params; OOS split {oos_split_ts}",
        sample_count=len(periods),
        gross_expectancy_bps=_mean(gross), fee_adjusted_bps=_mean(net),
        oos_gross_bps=(_mean(oos_g) if oos_g else None),
        oos_fee_adjusted_bps=(_mean(oos_n) if oos_n else None),
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest tests/test_campaign_specs.py -q` → PASS (3). Then ruff: `cd /Users/mgh3326/work/auto_trader.rob-353 && uv run ruff check research/nautilus_scalping/campaign_specs.py research/nautilus_scalping/tests/test_campaign_specs.py` → clean.

- [ ] **Step 5: Commit**

```bash
git add campaign_specs.py tests/test_campaign_specs.py
git commit -m "feat(ROB-353): campaign_specs summary derivation (gross/fee-adjusted/OOS bps)"
```

---

## Task 2: `campaign_specs` spec builders (families 1–3)

**Files:** Modify `campaign_specs.py`; Modify `tests/test_campaign_specs.py`

- [ ] **Step 1: Write the failing test**

```python
def _ramp(start_ts, n, base=100.0, step=1.0):
    return [(start_ts + i * DAY, base + i * step) for i in range(n)]


def test_breakout_spec_pools_trades_across_symbols():
    panel = {"AUSDT": _ramp(0, 40), "BUSDT": _ramp(0, 40, base=50.0, step=0.5)}
    # breakout needs Bar objects per symbol; the builder derives them from the panel closes
    spec = campaign_specs.breakout_spec(panel, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family1_breakout_continuation"
    assert spec["kind"] == "trade"
    assert all(hasattr(t, "net_ref_pnl") for t in spec["data"])
    assert spec["summary"].sample_count == len(spec["data"])
    assert spec["maker_conservative_net"] is None


def test_ts_trend_spec_is_portfolio():
    panel = {"AUSDT": _ramp(0, 40), "BUSDT": _ramp(0, 40, base=50.0, step=-0.3)}
    spec = campaign_specs.ts_trend_spec(panel, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family2_ts_trend_basket"
    assert spec["kind"] == "portfolio"
    assert all(isinstance(p, PortfolioPeriod) for p in spec["data"])


def test_xs_momentum_spec_is_portfolio_pit_aware():
    panel = {s: _ramp(0, 40, base=b) for s, b in [("AUSDT", 100), ("BUSDT", 50), ("CUSDT", 75)]}
    import pit_universe
    m = pit_universe.PITManifest.from_records([{"symbol": s, "listed_from": 0} for s in panel])
    rebals = [10 * DAY, 17 * DAY, 24 * DAY, 31 * DAY]
    spec = campaign_specs.xs_momentum_spec(panel, rebals, m, oos_split_ts=campaign_specs.OOS_SPLIT_TS)
    assert spec["name"] == "family3_xs_momentum"
    assert spec["kind"] == "portfolio"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest tests/test_campaign_specs.py -q -k spec`
Expected: FAIL — `AttributeError: module 'campaign_specs' has no attribute 'breakout_spec'`.

- [ ] **Step 3: Implement spec builders**

Append to `campaign_specs.py`:

```python
def _panel_to_bars(series: Sequence[tuple[int, float]]) -> list[families.Bar]:
    """Build single-symbol Bars from a (ts, close) series (high=low=close; OHLC-from-close)."""
    return [families.Bar(ts=ts, high=c, low=c, close=c) for ts, c in series]


def breakout_spec(panel: dict[str, Sequence[tuple[int, float]]], oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    pooled: list[Trade] = []
    for symbol in sorted(panel):
        pooled.extend(families.breakout_continuation_trades(_panel_to_bars(panel[symbol]), notional=NOTIONAL))
    pooled.sort(key=lambda t: t.ts_opened)
    return {"name": "family1_breakout_continuation",
            "summary": _summary_from_trades("family1_breakout_continuation", pooled, oos_split_ts),
            "kind": "trade", "data": pooled, "maker_conservative_net": None}


def ts_trend_spec(panel: dict[str, Sequence[tuple[int, float]]], oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    periods = families.ts_trend_basket_periods(panel, notional=NOTIONAL)
    return {"name": "family2_ts_trend_basket",
            "summary": _summary_from_periods("family2_ts_trend_basket", periods, oos_split_ts),
            "kind": "portfolio", "data": periods, "maker_conservative_net": None}


def xs_momentum_spec(panel, rebalances, manifest, oos_split_ts: int = OOS_SPLIT_TS) -> dict:
    periods = families.xs_momentum_periods(panel, rebalances, notional=NOTIONAL, manifest=manifest)
    return {"name": "family3_xs_momentum",
            "summary": _summary_from_periods("family3_xs_momentum", periods, oos_split_ts),
            "kind": "portfolio", "data": periods, "maker_conservative_net": None}
```

NOTE: `breakout_continuation_trades` reads only `bars[i].high` and `bars[i].close`; building `high=low=close=close` means the breakout triggers on close-vs-prior-close-high — an honest daily-close approximation (documented in the report). Do not invent intrabar highs.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest tests/test_campaign_specs.py -q` → PASS (6). ruff clean on both files.

- [ ] **Step 5: Commit**

```bash
git add campaign_specs.py tests/test_campaign_specs.py
git commit -m "feat(ROB-353): family 1-3 spec builders (pool breakout, ts-trend, PIT xs-momentum)"
```

---

## Task 3: `campaign_controls` — pure RUN analytics

**Files:** Create `campaign_controls.py`; Create `tests/test_campaign_controls.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_campaign_controls.py
import campaign_controls
import pit_universe

DAY = 86_400_000


def test_weekly_rebalances_inclusive_step():
    r = campaign_controls.weekly_rebalances(0, 21 * DAY, step_days=7)
    assert r == [0, 7 * DAY, 14 * DAY, 21 * DAY]


def test_buy_hold_bps_close_to_close():
    series = [(0, 100.0), (DAY, 110.0)]  # +10% = 1000 bps
    assert round(campaign_controls.buy_hold_bps(series), 6) == 1000.0
    assert campaign_controls.buy_hold_bps([]) == 0.0


def test_max_drawdown_bps_on_cumulative_pnl():
    # net pnls per period on notional 1000: +50, -120, +10 -> equity 1050, 930, 940
    # peak 1050 then trough 930 -> drawdown 120/1050 in bps
    dd = campaign_controls.max_drawdown_bps([50.0, -120.0, 10.0], notional=1000.0)
    assert dd < 0 and round(dd, 2) == round(-120.0 / 1050.0 * 1e4, 2)


def test_filter_universe_uses_membership_and_quality():
    m = pit_universe.PITManifest.from_records([
        {"symbol": "GOOD", "listed_from": 0, "delisted_at": None, "status": "live",
         "kline_coverage": 1.0, "confidence": "high"},
        {"symbol": "LOWCOV", "listed_from": 0, "delisted_at": None, "status": "live",
         "kline_coverage": 0.5, "confidence": "low"},
        {"symbol": "OUTWINDOW", "listed_from": 100 * DAY, "delisted_at": None, "status": "live",
         "kline_coverage": 1.0, "confidence": "high"},
    ])
    kept = campaign_controls.filter_universe(m, lo_ts=0, hi_ts=10 * DAY)
    assert kept == ["GOOD"]
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run --no-project pytest tests/test_campaign_controls.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'campaign_controls'`.

- [ ] **Step 3: Implement**

```python
"""ROB-353 (PR2) — pure analytics for the empirical RUN (no I/O, no network).

Baselines and robustness numbers the verdict report cites: weekly rebalance grid,
max drawdown, buy&hold return, and the survivorship-/quality-aware universe filter
(membership overlap + manifest coverage/confidence). Dollar-volume liquidity
filtering is intentionally NOT done here (disclosed as a skipped control).
"""
from __future__ import annotations

from collections.abc import Sequence

from pit_universe import PITManifest

_DAY_MS = 86_400_000


def weekly_rebalances(lo_ts: int, hi_ts: int, step_days: int = 7) -> list[int]:
    step = step_days * _DAY_MS
    return list(range(lo_ts, hi_ts + 1, step))


def buy_hold_bps(close_series: Sequence[tuple[int, float]]) -> float:
    if len(close_series) < 2:
        return 0.0
    first, last = close_series[0][1], close_series[-1][1]
    return (last - first) / first * 1e4 if first else 0.0


def max_drawdown_bps(period_net_pnls: Sequence[float], notional: float = 1000.0) -> float:
    equity = notional
    peak = notional
    worst = 0.0
    for pnl in period_net_pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak * 1e4)
    return worst


def filter_universe(manifest: PITManifest, lo_ts: int, hi_ts: int,
                    min_coverage: float = 0.8, confidences=("high", "medium")) -> list[str]:
    """Symbols whose listing overlaps [lo_ts, hi_ts] with adequate data quality."""
    kept = []
    for x in manifest.listings:
        overlaps = x.listed_from <= hi_ts and (x.delisted_at is None or x.delisted_at > lo_ts)
        cov_ok = (x.kline_coverage or 0.0) >= min_coverage
        conf_ok = x.confidence in confidences
        if overlaps and cov_ok and conf_ok:
            kept.append(x.symbol)
    return sorted(kept)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-project pytest tests/test_campaign_controls.py -q` → PASS (4). ruff clean.

- [ ] **Step 5: Commit**

```bash
git add campaign_controls.py tests/test_campaign_controls.py
git commit -m "feat(ROB-353): pure RUN analytics (rebalance grid, drawdown, buy&hold, universe filter)"
```

---

## Task 4: `run_rob353_campaign.py` — `--self-test` (synthetic, no network)

**Files:** Create `run_rob353_campaign.py`

- [ ] **Step 1: Write the failing test (run the self-test as the test)**

There is no unit test file; the acceptance is the CLI self-test. First create the failing expectation:

Run: `uv run --no-project python run_rob353_campaign.py --self-test`
Expected (before implementation): FAIL — `python: can't open file ... run_rob353_campaign.py`.

- [ ] **Step 2: Implement the harness with `--self-test` first**

Create `run_rob353_campaign.py`:

```python
#!/usr/bin/env python3
"""ROB-353 (PR2) — bounded empirical RUN harness for the ROB-351 funnel (research only).

Two modes:
  --self-test   Build the three family specs from tiny SYNTHETIC panels (no network,
                no data, no secrets) and print the rob351_campaign.v1 verdict table.
                Proves the bridge wiring + that the frozen config_hash is unchanged.
  (default)     Bounded real RUN: load the PR1 PIT manifest, fetch/cache 1d klines for
                strict_usdt_perp ∩ window, build specs, call campaign.run_campaign, and
                write the verdict JSON + controls under results/rob353/ (gitignored).
                Network/operator-gated. The committed report is authored from this output.

Safety: research/backtest only. No live, no Demo confirm, no broker/order/scheduler/DB,
no /invest. ROB-343 is RECOMMENDED by the verdict, never run here. No raw data committed.
"""
from __future__ import annotations

import argparse
import json
import sys


def _self_test() -> dict:
    import campaign
    import campaign_specs as cs
    import pit_universe
    from frozen_config import FROZEN_CONFIG

    DAY = 86_400_000
    panel = {
        "AUSDT": [(i * DAY, 100.0 + i) for i in range(40)],
        "BUSDT": [(i * DAY, 50.0 - 0.2 * i) for i in range(40)],
        "CUSDT": [(i * DAY, 75.0 + (i % 5)) for i in range(40)],
    }
    manifest = pit_universe.PITManifest.from_records(
        [{"symbol": s, "listed_from": 0} for s in panel]
    )
    rebals = [10 * DAY, 17 * DAY, 24 * DAY, 31 * DAY]
    specs = [
        cs.breakout_spec(panel),
        cs.ts_trend_spec(panel),
        cs.xs_momentum_spec(panel, rebals, manifest),
    ]
    return campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ROB-353 bounded empirical RUN (research only)")
    ap.add_argument("--self-test", action="store_true",
                    help="synthetic wiring proof (no network); prints the verdict table")
    ap.add_argument("--from-month", default="2023-01")
    ap.add_argument("--to-month", default="2026-04")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="operator bound on universe size (default: all qualifying)")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="use already-downloaded klines; do not hit the network")
    args = ap.parse_args(argv)

    if args.self_test:
        result = _self_test()
        print(json.dumps(result, indent=2))
        from frozen_config import FROZEN_CONFIG
        assert result["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"
        return 0

    return _real_run(args)


def _real_run(args) -> int:  # pragma: no cover - network/operator-gated
    raise SystemExit(
        "Real RUN is operator-gated and implemented in Task 5. "
        "Use --self-test to verify wiring without network/data."
    )


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run the self-test**

Run: `uv run --no-project python run_rob353_campaign.py --self-test`
Expected: prints a JSON object with `"schema_version": "rob351_campaign.v1"`, `"config_hash": "8f02dffd51dc5bedf5ab4c1521edb2185f4768304b5b60fa7dd0836ef8872adf"`, and 3 `families` rows (family1/2/3). Exit 0 (the assert confirms no config drift).

- [ ] **Step 4: ruff + commit**

ruff: `cd /Users/mgh3326/work/auto_trader.rob-353 && uv run ruff check research/nautilus_scalping/run_rob353_campaign.py` → clean (the `_real_run` stub uses `# pragma: no cover`; keep `args` referenced).
```bash
git add run_rob353_campaign.py
git commit -m "feat(ROB-353): RUN harness --self-test (synthetic wiring, frozen-config assert)"
```

---

## Task 5: `run_rob353_campaign.py` — real RUN body (`_real_run`)

**Files:** Modify `run_rob353_campaign.py`

The pure helpers (Tasks 1–3) are tested; `_real_run` is thin orchestration over them + the network fetch, so it is not unit-tested (operator-gated). It must stay import-safe (no top-level execution).

- [ ] **Step 1: Implement `_real_run`**

Replace the `_real_run` stub with:

```python
def _real_run(args) -> int:  # pragma: no cover - network/operator-gated
    import campaign
    import campaign_controls as cc
    import campaign_specs as cs
    import pit_bars
    import pit_klines_fetcher
    import pit_universe
    from frozen_config import FROZEN_CONFIG

    manifest = pit_universe.PITManifest.load("data_manifests/pit_universe.v1.json").strict_usdt_perp()
    lo = pit_universe._date_to_epoch_ms(f"{args.from_month}-01")
    hi = pit_universe._date_to_epoch_ms(f"{args.to_month}-28")
    symbols = cc.filter_universe(manifest, lo, hi)
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    print(f"universe: {len(symbols)} strict-perp symbols (membership+quality filtered)")

    if not args.skip_fetch:
        for i, sym in enumerate(symbols, 1):
            summary = pit_klines_fetcher.fetch_months(sym, "1d", args.from_month, args.to_month)
            if i % 25 == 0:
                print(f"  fetched {i}/{len(symbols)} (last {sym}: {summary['downloaded']} dl)")

    panel = pit_bars.load_panel(symbols, "1d", manifest)
    panel = {s: v for s, v in panel.items() if len(v) >= 30}  # need enough bars to be meaningful
    print(f"panel: {len(panel)} symbols with >=30 daily bars")
    rebals = cc.weekly_rebalances(lo, hi)

    specs = [
        cs.breakout_spec(panel),
        cs.ts_trend_spec(panel),
        cs.xs_momentum_spec(panel, rebals, manifest),
    ]
    result = campaign.run_campaign(specs, config=FROZEN_CONFIG, min_trades=5)
    assert result["config_hash"] == FROZEN_CONFIG.config_hash(), "frozen config drift!"

    # controls
    btc = panel.get("BTCUSDT")
    controls = {
        "universe_size": len(panel),
        "window": f"{args.from_month}..{args.to_month}",
        "interval": "1d",
        "btc_buy_hold_bps": (cc.buy_hold_bps(btc) if btc else None),
        "family_drawdown_bps": {},
        "skipped_controls": [
            "dollar-volume liquidity filter (used manifest coverage/confidence instead)",
            "parameter-neighborhood sweep", "BTC regime split", "symbol-concentration analysis",
            "1h interval (deferred)",
        ],
    }
    for spec in specs:
        if spec["kind"] == "portfolio":
            controls["family_drawdown_bps"][spec["name"]] = cc.max_drawdown_bps(
                [p.gross_ref_pnl for p in spec["data"]], notional=cs.NOTIONAL)

    import os
    os.makedirs("results/rob353", exist_ok=True)
    out = {"verdict_table": result, "controls": controls,
           "spec_sample_counts": {s["name"]: s["summary"].sample_count for s in specs}}
    with open("results/rob353/rob351_campaign.v1.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print("\nwrote results/rob353/rob351_campaign.v1.json (gitignored). "
          "Author docs/runbooks/rob-353-pr2-empirical-verdict.md from this output.")
    return 0
```

- [ ] **Step 2: Verify import-safety + self-test still green**

Run: `uv run --no-project python -c "import run_rob353_campaign"` (no network, no error) and `uv run --no-project python run_rob353_campaign.py --self-test | grep config_hash` → unchanged hash.
ruff: `uv run ruff check research/nautilus_scalping/run_rob353_campaign.py` → clean.

- [ ] **Step 3: Commit**

```bash
git add run_rob353_campaign.py
git commit -m "feat(ROB-353): real RUN body (manifest->fetch->panel->specs->run_campaign->controls)"
```

---

## Task 6: Guard extension + suite green

**Files:** Modify `tests/test_pit_data_layer_guard.py`

- [ ] **Step 1: Add the new modules to the guard list**

In `tests/test_pit_data_layer_guard.py`, extend `_MODULES`:
```python
_MODULES = ["pit_klines_fetcher.py", "pit_bars.py", "build_pit_universe.py", "pit_universe.py",
            "campaign_specs.py", "campaign_controls.py", "run_rob353_campaign.py"]
```

- [ ] **Step 2: Run guard + full suite**

Run: `uv run --no-project pytest tests/test_pit_data_layer_guard.py -q` → PASS (the new modules import only `families`/`cost_model`/`discovery`/`pit_*`/`validated_gate`/`campaign`/stdlib — no `app.*`).
Run: `uv run --no-project pytest -q --ignore=tests/test_signal_parity.py` → all green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_pit_data_layer_guard.py
git commit -m "test(ROB-353): extend data-layer import guard to PR2 modules"
```

---

## Task 7: Execute the bounded RUN + author the committed report

**Files:** Create `docs/runbooks/rob-353-pr2-empirical-verdict.md`

This task performs the real network RUN and writes the durable report from REAL output. **No fabricated numbers.** If the RUN cannot complete (network/disk), the report records a `run-not-executed` status with the exact rerun command and stops — it must not invent a verdict.

- [ ] **Step 1: Run the bounded campaign**

Run (network; expect minutes): `uv run --no-project python run_rob353_campaign.py --from-month 2023-01 --to-month 2026-04 2>&1 | tee /tmp/rob353_run.log`
Expected: prints universe size, panel size, the `rob351_campaign.v1` verdict table (3 families with screen/gate/label_343/breakeven), controls (btc_buy_hold_bps, per-family drawdown, skipped_controls), and writes `results/rob353/rob351_campaign.v1.json`. Capture the exact JSON.

If fetch is slow, you may bound with `--max-symbols 120` (still hundreds of perps) and record the bound in the report as a coverage caveat.

- [ ] **Step 2: Author the report from the REAL output**

Create `docs/runbooks/rob-353-pr2-empirical-verdict.md` containing, filled from `results/rob353/rob351_campaign.v1.json`:
- Status line (RUN executed: date, universe size, panel size, or `run-not-executed` + rerun cmd).
- Data source/retrieval (`data.binance.vision/futures/um`, public), window (2023-01..2026-04), interval 1d.
- Universe definition: `strict_usdt_perp` ∩ window-active ∩ (coverage≥0.8, confidence∈{high,medium}); active+delisted included; exclusions (settling/BUSD/USDC/dated/SETTLED); PIT manifest path + `snapshot_hash` from `pit_universe.v1.meta.json`.
- The verdict table as a JSON fence (the `verdict_table.families` array verbatim) + the `config_hash`.
- Controls: per-family gross vs net + sample_count (from the run), turnover/breakeven (`breakeven_taker_bps` per family), `btc_buy_hold_bps` baseline + cash(0bps) baseline, per-family `family_drawdown_bps`, OOS handling note (train≤2025-01 split in summaries).
- **Skipped controls** (verbatim from `controls.skipped_controls`) with the one-line reason each — satisfies the "thin run cannot quietly pass" AC.
- Per-family verdict (reject / needs_more_data / promote_to_pilot / cost_binding_343_candidate — read from `label_343`/`screen`/`gate_verdict`).
- Branch recommendation (explicit): if any `promote_to_pilot` → propose bounded pilot design issue; if any `cost_binding_343_candidate` → propose ROB-343 Demo fill-provenance probe; else (all reject/needs_more_data) → recommend family 4/5 feasibility (funding/OI/liquidation, TODOS.md). Do NOT enable automation; do NOT implement ROB-343.
- Safety boundary confirmation (research-only; no live/broker/scheduler/DB; no raw data committed; canonical `validated` not used).

- [ ] **Step 3: Confirm only the report (no raw data / no verdict-json) is staged**

Run: `git status --porcelain` then `git check-ignore results/rob353/rob351_campaign.v1.json` (must be ignored) and confirm no `.csv`/`data/`/`results/` staged.

- [ ] **Step 4: Commit**

```bash
git add docs/runbooks/rob-353-pr2-empirical-verdict.md
git commit -m "docs(ROB-353): empirical 1d verdict report for families 1-3 (bounded RUN)"
```

---

## Task 8: Final verification + PR (base `rob-353`)

**Files:** none (verification + PR)

- [ ] **Step 1: Full suite + self-test + lint**

Run: `uv run --no-project pytest -q --ignore=tests/test_signal_parity.py` (green); `uv run --no-project python run_rob353_campaign.py --self-test | grep config_hash` (unchanged `8f02dffd…`); `cd /Users/mgh3326/work/auto_trader.rob-353 && uv run ruff check research/nautilus_scalping/` (clean).

- [ ] **Step 2: Confirm no raw data / secrets in the branch diff**

Run: `git diff --name-only rob-353...HEAD` and `git diff --name-only rob-353...HEAD | grep -iE "\.csv$|\.parquet$|/data/|results/"` → second command empty. Only new code, tests, and the report markdown.

- [ ] **Step 3: Push + open stacked PR (base `rob-353`)**

```bash
git push -u origin rob-353-pr2
gh pr create --base rob-353 --title "feat(ROB-353): campaign bridge + bounded empirical verdict (PR2/2)" \
  --body "Stacked on #995. Bridges PR1 PIT data into the ROB-351 funnel (campaign_specs), runs a bounded 1d survivorship-safe campaign for families 1-3 (run_rob353_campaign), and commits the durable verdict report. Frozen config untouched; research-only; ROB-343 recommended-not-run. Rebase --onto origin/main after #995 squash-merges. Spec: docs/superpowers/specs/2026-05-29-rob-353-pr2-bridge-run-design.md"
```

Note the stacked-squash gotcha: when #995 squash-merges, rebase `rob-353-pr2` `--onto origin/main` and retarget this PR to `main`.

---

## Self-review notes (author)

- **Spec coverage:** bridge summary (T1), spec builders (T2), controls + universe filter (T3), self-test harness (T4), real RUN body (T5), guard (T6), executed RUN + committed report (T7), final/PR (T8). The spec's "median quote-vol liquidity filter" is intentionally replaced by a manifest coverage/confidence filter and the dollar-volume filter is disclosed as a skipped control (recorded in `controls.skipped_controls` and the report) — a deliberate, documented divergence to avoid re-reading raw CSVs / changing PR1's `pit_bars`.
- **Type consistency:** `NOTIONAL`/`OOS_SPLIT_TS`, `_summary_from_trades`/`_summary_from_periods`, `breakout_spec`/`ts_trend_spec`/`xs_momentum_spec`, `weekly_rebalances`/`max_drawdown_bps`/`buy_hold_bps`/`filter_universe` are referenced identically across tasks and the contract table. Spec dicts use keys `name/summary/kind/data/maker_conservative_net` matching `campaign.run_campaign`.
- **Known soft spots:** (1) breakout on daily closes uses high=low=close (honest daily-close approximation, disclosed in report). (2) T7 is the only network task; if it can't run, the report records `run-not-executed` rather than fabricating — the bridge + self-test still land as committed, testable value.
