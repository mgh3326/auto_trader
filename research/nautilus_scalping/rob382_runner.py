"""ROB-382 — per-candidate orchestration: load → simulate → baseline-aware OOS gate → verdict.

A ported candidate is a module exposing:
    NATIVE_INTERVAL: str            # "5m" | "1m" (the strategy's own timeframe — faithful)
    NEEDS_INFORMATIVE_1H: bool
    EXIT_MODEL: rob382_backtest.ExitModel
    HOLD_SEMANTICS: str             # human note: how exit/hold was preserved or changed
    def signals(bars, bars_1h=None) -> tuple[list[bool], list[bool]]   # (entry, exit), causal

Pipeline (honors ROB-382 §3 AC):
  1. pool the candidate's trades across symbols (entry @ close, SL-first, non-overlapping);
  2. cost-blind GROSS screen (triviality floor 0.5 bps + OOS-gross sign) — discovery.screen;
  3. if it clears the screen, the cost/OOS GATE vs REAL micro-breakout + random baselines
     (validated_gate.evaluate_gate at the frozen taker fee) — beats-baselines + walk-forward
     OOS + profit-factor + param-stability + bootstrap CI;
  4. emit a counts-only verdict row + the contrast fields + a strict decisive-survivor flag.

net_* are reported at the FROZEN taker (4.0 bps/leg = 8 bps round trip; the gate's cost) AND
at the harsher retail REF fee (10 bps/leg) so the cost-sensitivity is explicit. No market data
is committed; raw bars stay on disk.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import cost_model
import rob382_backtest as bt
import rob382_bars as rb
import rob382_baselines as bl
import validated_gate as vg
from discovery.screen import HypothesisSummary, classify
from frozen_config import FROZEN_CONFIG
from validated_gate import Trade

DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT")
OOS_SPLIT_TS = 1_735_689_600_000  # 2025-01-01T00:00:00Z (ROB-349 train/test boundary)
REF_FEE_BPS = cost_model.REF_FEE_BPS  # retail taker reference (10 bps/leg)


def _t_stat_bps(values: Sequence[float]) -> float:
    """One-sample t-stat of per-trade returns (bps) vs 0. 0.0 if <2 or zero-variance."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var / n)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _summary(name: str, trades: Sequence[Trade]) -> HypothesisSummary:
    gross = [(t.net_ref_pnl + t.commission_ref) / t.notional * 1e4 for t in trades]
    net_ref = [t.net_ref_pnl / t.notional * 1e4 for t in trades]
    oos_g = [g for g, t in zip(gross, trades, strict=True) if t.ts_opened > OOS_SPLIT_TS]
    oos_n = [n for n, t in zip(net_ref, trades, strict=True) if t.ts_opened > OOS_SPLIT_TS]
    return HypothesisSummary(
        name=name, conditions=f"ROB-382 faithful port; OOS split {OOS_SPLIT_TS}",
        sample_count=len(trades),
        gross_expectancy_bps=_mean(gross), fee_adjusted_bps=_mean(net_ref),
        oos_gross_bps=(_mean(oos_g) if oos_g else None),
        oos_fee_adjusted_bps=(_mean(oos_n) if oos_n else None),
    )


def simulate_candidate(module, symbols: Sequence[str] = DEFAULT_SYMBOLS):
    """Run a candidate module across symbols; return (pooled trades, per-symbol data, diag)."""
    pooled: list[Trade] = []
    per_symbol: dict[str, dict] = {}
    bars_1h_cache: dict[str, list] = {}
    for sym in symbols:
        bars = rb.load_ohlcv(sym, module.NATIVE_INTERVAL)
        if not bars:
            per_symbol[sym] = {"trades": 0}
            continue
        bars_1h = None
        if getattr(module, "NEEDS_INFORMATIVE_1H", False):
            bars_1h = bars_1h_cache.get(sym) or rb.load_ohlcv(sym, "1h")
            bars_1h_cache[sym] = bars_1h
        entry, exit_sig = module.signals(bars, bars_1h)
        trades = bt.simulate(bars, entry, exit_sig, module.EXIT_MODEL)
        per_symbol[sym] = {"bars": bars, "exit_sig": exit_sig, "trades": len(trades), "trade_list": trades}
        pooled.extend(trades)
    pooled.sort(key=lambda t: t.ts_opened)
    diag = {
        "symbols": list(symbols),
        "native_interval": module.NATIVE_INTERVAL,
        "needs_informative_1h": bool(getattr(module, "NEEDS_INFORMATIVE_1H", False)),
        "per_symbol_trade_counts": {s: d["trades"] for s, d in per_symbol.items()},
        "exit_model": {
            "type": module.EXIT_MODEL.type, "hard_sl_pct": module.EXIT_MODEL.hard_sl_pct,
            "roi_pct": module.EXIT_MODEL.roi_pct, "max_hold_bars": module.EXIT_MODEL.max_hold_bars,
        },
        "hold_semantics": getattr(module, "HOLD_SEMANTICS", "unspecified"),
    }
    return pooled, per_symbol, diag


def _build_baselines(module, per_symbol: dict) -> tuple[list[Trade], list[Trade]]:
    breakout: list[Trade] = []
    rnd: list[Trade] = []
    for idx, (sym, d) in enumerate(sorted(per_symbol.items())):
        bars = d.get("bars")
        if not bars:
            continue
        breakout.extend(bl.breakout_baseline(bars))
        rnd.extend(bl.random_baseline(bars, d["exit_sig"], module.EXIT_MODEL,
                                      n_entries=d["trades"], seed=1_000 + idx))
    breakout.sort(key=lambda t: t.ts_opened)
    rnd.sort(key=lambda t: t.ts_opened)
    return breakout, rnd


def _funnel_verdict(screen_reco: str, gate_verdict: str | None) -> str:
    if screen_reco == "needs_more_data":
        return "needs_more_data"
    if screen_reco == "screened_out":
        return "screened_out"
    if gate_verdict == "validated":
        return "gross_edge_present_AND_oos_validated"
    if gate_verdict == "insufficient_data":
        return "gross_edge_present_but_underpowered"  # too few trades for the walk-forward gate
    return "gross_edge_present_needs_full_validation"  # tested vs baselines/OOS, not validated


def run_candidate(module, *, name: str, contrast: dict, symbols: Sequence[str] = DEFAULT_SYMBOLS,
                  min_trades: int = 100) -> dict:
    """Full per-candidate pipeline → one counts-only verdict row + contrast fields.

    ``contrast`` carries strat.ninja numbers (recorded FOR CONTRAST ONLY, never as evidence).
    """
    cfg = FROZEN_CONFIG
    taker_rt_bps = 2.0 * cfg.taker_bps      # frozen taker round-trip cost (8 bps)
    ref_rt_bps = 2.0 * REF_FEE_BPS          # retail REF round-trip cost (20 bps)

    trades, per_symbol, diag = simulate_candidate(module, symbols)
    summary = _summary(name, trades)

    # Stage 1 — cost-blind gross screen (triviality floor + OOS-gross sign).
    screened = classify(
        summary, cost_blind=True,
        min_samples=min(summary.sample_count, min_trades) if min_trades else 1,
        min_gross_bps=cfg.economic_triviality_floor_bps,
    )

    # Stage 2 — baseline-aware cost/OOS gate (only if it cleared the gross screen).
    gate_verdict = None
    gate_reasons: list[str] = []
    beats_breakout = beats_random = None
    if screened.recommendation == "promote_to_full_validation":
        breakout, rnd = _build_baselines(module, per_symbol)
        rep = vg.evaluate_gate(
            candidate_runs={"p": trades}, baseline_breakout=breakout, baseline_random=rnd,
            fee_bps=cfg.taker_bps, min_trades=min_trades, candidate_name=name,
            symbols=list(symbols), window={"oos_split_ts": OOS_SPLIT_TS},
        )
        gate_verdict = rep.verdict
        gate_reasons = rep.verdict_reasons
        oos_net = rep.results.get("net_after_cost", {})
        bo = rep.baselines.get("micro_breakout", {})
        rd = rep.baselines.get("random_entry", {})
        beats_breakout = oos_net.get("net_pnl", 0.0) > bo.get("net_after_cost", 0.0)
        beats_random = oos_net.get("net_pnl", 0.0) > rd.get("net_after_cost", 0.0)

    gross_bps = [(t.net_ref_pnl + t.commission_ref) / t.notional * 1e4 for t in trades]
    oos_gross = [g for g, t in zip(gross_bps, trades, strict=True) if t.ts_opened > OOS_SPLIT_TS]

    g_full = summary.gross_expectancy_bps
    g_oos = summary.oos_gross_bps
    t_oos_gross = _t_stat_bps(oos_gross)
    oos_net_taker = (g_oos - taker_rt_bps) if g_oos is not None else None

    # Decisive-survivor bar (ROB-382: gross + t>2 + OOS, beats baselines, net-positive at our cost).
    meets_survivor = bool(
        gate_verdict == "validated"
        and t_oos_gross >= cfg.target_t
        and oos_net_taker is not None and oos_net_taker > 0
        and beats_breakout and beats_random
    )

    return {
        "name": name,
        "contrast": contrast,
        "native_timeframe": diag["native_interval"],
        "needs_informative_1h": diag["needs_informative_1h"],
        "hold_semantics": diag["hold_semantics"],
        "exit_model": diag["exit_model"],
        "per_symbol_trade_counts": diag["per_symbol_trade_counts"],
        "trade_count": len(trades),
        "oos_trade_count": len(oos_gross),
        # gross (the first gate; faithful native-timeframe edge before fees)
        "our_gross_bps": round(g_full, 4),
        "our_oos_gross_bps": (round(g_oos, 4) if g_oos is not None else None),
        # net at OUR frozen taker (8 bps RT) vs retail REF (20 bps RT) — cost sensitivity explicit
        "our_net_bps_frozen_taker": round(g_full - taker_rt_bps, 4),
        "our_oos_net_bps_frozen_taker": (round(g_oos - taker_rt_bps, 4) if g_oos is not None else None),
        "our_net_bps_retail_ref": round(summary.fee_adjusted_bps, 4),
        "our_oos_net_bps_retail_ref": (
            round(summary.oos_fee_adjusted_bps, 4) if summary.oos_fee_adjusted_bps is not None else None
        ),
        "frozen_taker_rt_bps": taker_rt_bps,
        "retail_ref_rt_bps": ref_rt_bps,
        "our_t_stat_gross": round(_t_stat_bps(gross_bps), 3),
        "our_t_stat_oos_gross": round(t_oos_gross, 3),
        "target_t": cfg.target_t,
        # screen + gate
        "screen": screened.recommendation,
        "screen_reason": screened.reason,
        "gate_verdict": gate_verdict,
        "gate_reasons": gate_reasons,
        "beats_micro_breakout_baseline": beats_breakout,
        "beats_random_baseline": beats_random,
        "our_verdict": _funnel_verdict(screened.recommendation, gate_verdict),
        "meets_decisive_survivor_bar": meets_survivor,
        "config_hash": cfg.config_hash(),
    }
