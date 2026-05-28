"""ROB-320 — pure validated-signal gate.

Consumes chronological trade lists (no Nautilus) and produces a GateReport with
a ``validated`` / ``not_validated`` / ``insufficient_data`` verdict, walk-forward
fold metrics, gross/zero-fee/net-after-cost separation, baseline comparison, and
concrete overfit flags. Net at any fee is recomputed analytically from the
reference-fee run (same method as fee_sweep / compare_strategies).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import cost_model

REF_FEE_BPS = 10.0
Verdict = Literal["validated", "not_validated", "insufficient_data"]


@dataclass(frozen=True)
class Trade:
    net_ref_pnl: float      # realized pnl at REF_FEE_BPS
    commission_ref: float   # commission paid at REF_FEE_BPS (negative)
    notional: float
    ts_opened: int


@dataclass(frozen=True)
class PortfolioPeriod:
    """One period's PORTFOLIO-AGGREGATED PnL for a basket / cross-sectional run.

    Unlike ``Trade`` (per-position, keyed by open time), a period already nets all
    concurrent positions' mark-to-market into a single increment. Drawdown on the
    series of these is the honest portfolio drawdown (ROB-351 Issue 1).

    ``gross_ref_pnl`` = portfolio PnL in the period at REF_FEE_BPS;
    ``commission_ref`` = period commission magnitude at REF_FEE_BPS (>= 0); the
    shared ``cost_model.net_at_fee`` rescales to any taker fee.
    """

    ts: int
    gross_ref_pnl: float
    commission_ref: float = 0.0


@dataclass(frozen=True)
class FoldMetrics:
    fold: str
    trades: int
    net_pnl: float
    win_rate_pct: float
    max_drawdown: float
    profit_factor: float
    expectancy: float


@dataclass
class GateReport:
    schema_version: str = "validated_signal_gate.v1"
    candidate: str = ""
    hypothesis: str = ""
    symbols: list[str] = field(default_factory=list)
    window: dict = field(default_factory=dict)
    cost_model: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    per_fold: list[dict] = field(default_factory=list)
    baselines: dict = field(default_factory=dict)
    param_stability: dict = field(default_factory=dict)
    overfit_flags: dict = field(default_factory=dict)
    trade_count: int = 0
    verdict: Verdict = "insufficient_data"
    verdict_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _net_at_fee(t: Trade, fee_bps: float) -> float:
    # Delegates to the shared primitive (ROB-351 Issue 4, 3->1 dedup).
    return cost_model.net_at_fee(t.net_ref_pnl, t.commission_ref, fee_bps, REF_FEE_BPS)


def net_pnls_at_fee(trades: list[Trade], fee_bps: float) -> list[float]:
    """Per-trade net-after-fee PnLs (chronological) for the statistical layer."""
    return [_net_at_fee(t, fee_bps) for t in sorted(trades, key=lambda t: t.ts_opened)]


def _equity_drawdown(pnls: Sequence[float]) -> float:
    """Absolute max drawdown on the cumulative-PnL equity curve (starts at 0).

    Caller is responsible for ordering ``pnls`` chronologically. For a basket the
    ``pnls`` must be PERIOD-AGGREGATED portfolio increments (concurrent positions
    netted into each period) — see ``portfolio_metrics_at_fee``. Feeding a flat
    per-position list keyed by open-time understates drawdown (ROB-351 Issue 1).
    """
    equity = peak = mdd = 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
    return mdd


def _fold_metrics_from_nets(nets: list[float], fold: str) -> FoldMetrics:
    """Build FoldMetrics from an already-ordered net-PnL series."""
    n = len(nets)
    if n == 0:
        return FoldMetrics(fold, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0)
    return FoldMetrics(
        fold=fold, trades=n, net_pnl=sum(nets),
        win_rate_pct=100.0 * len(wins) / n,
        max_drawdown=_equity_drawdown(nets), profit_factor=pf, expectancy=sum(nets) / n,
    )


def metrics_at_fee(trades: list[Trade], fee_bps: float, fold: str = "") -> FoldMetrics:
    rows = sorted(trades, key=lambda t: t.ts_opened)
    nets = [_net_at_fee(t, fee_bps) for t in rows]
    return _fold_metrics_from_nets(nets, fold)


def portfolio_net_pnls_at_fee(
    periods: Sequence[PortfolioPeriod], fee_bps: float
) -> list[float]:
    """Per-period net portfolio PnLs (chronological) at ``fee_bps`` per leg."""
    rows = sorted(periods, key=lambda p: p.ts)
    return [cost_model.net_at_fee(p.gross_ref_pnl, p.commission_ref, fee_bps, REF_FEE_BPS)
            for p in rows]


def portfolio_metrics_at_fee(
    periods: Sequence[PortfolioPeriod], fee_bps: float, fold: str = ""
) -> FoldMetrics:
    """FoldMetrics on the PERIOD-return equity curve (honest basket drawdown).

    ``trades`` in the returned FoldMetrics counts PERIODS, not positions.
    """
    return _fold_metrics_from_nets(portfolio_net_pnls_at_fee(periods, fee_bps), fold)


def _chrono_split(rows: list, fractions: tuple[float, float, float]) -> dict[str, list]:
    n = len(rows)
    n_train = int(n * fractions[0])
    n_val = int(n * fractions[1])
    return {
        "train": rows[:n_train],
        "val": rows[n_train:n_train + n_val],
        "oos": rows[n_train + n_val:],
    }


def walk_forward_split(
    trades: list[Trade], fractions: tuple[float, float, float] = (0.5, 0.25, 0.25)
) -> dict[str, list[Trade]]:
    return _chrono_split(sorted(trades, key=lambda t: t.ts_opened), fractions)


def walk_forward_split_periods(
    periods: list[PortfolioPeriod], fractions: tuple[float, float, float] = (0.5, 0.25, 0.25)
) -> dict[str, list[PortfolioPeriod]]:
    return _chrono_split(sorted(periods, key=lambda p: p.ts), fractions)


def evaluate_gate(
    *,
    candidate_runs: dict[str, list[Trade]],   # param_label -> trades
    baseline_breakout: list[Trade],
    baseline_random: list[Trade],
    fee_bps: float,
    min_trades: int = 100,
    fractions: tuple[float, float, float] = (0.5, 0.25, 0.25),
    candidate_name: str = "",
    hypothesis: str = "",
    symbols: list[str] | None = None,
    window: dict | None = None,
) -> GateReport:
    report = GateReport(
        candidate=candidate_name, hypothesis=hypothesis, symbols=symbols or [],
        window=window or {}, cost_model={"fee_bps_per_leg": fee_bps,
                                         "fee_grid_bps": [10.0, 7.5, 5.0, 2.0, 0.0]},
    )

    # Rank params by validation-fold net; pick the val-best param.
    by_param_val: dict[str, float] = {}
    by_param_oos: dict[str, float] = {}
    folds_by_param: dict[str, dict[str, list[Trade]]] = {}

    # Handle edge case where no candidate runs are supplied (or all empty)
    if not candidate_runs:
        report.verdict = "insufficient_data"
        report.verdict_reasons = ["no candidate runs provided"]
        return report

    for label, trades in candidate_runs.items():
        folds = walk_forward_split(trades, fractions)
        folds_by_param[label] = folds
        by_param_val[label] = metrics_at_fee(folds["val"], fee_bps, "val").net_pnl
        by_param_oos[label] = metrics_at_fee(folds["oos"], fee_bps, "oos").net_pnl

    val_best = max(by_param_val, key=by_param_val.get)
    folds = folds_by_param[val_best]

    # per-fold metrics (net-after-cost) for the val-best param
    fold_metrics = {name: metrics_at_fee(folds[name], fee_bps, name)
                    for name in ("train", "val", "oos")}
    report.per_fold = [asdict(fold_metrics[n]) for n in ("train", "val", "oos")]

    # gross / zero-fee / net-after-cost over ALL candidate trades (val-best)
    all_best = candidate_runs[val_best]
    report.results = {
        "gross": asdict(metrics_at_fee(all_best, 0.0, "gross")),
        "zero_fee": asdict(metrics_at_fee(all_best, 0.0, "zero_fee")),
        "net_after_cost": asdict(metrics_at_fee(all_best, fee_bps, "net_after_cost")),
    }
    report.trade_count = len(all_best)

    # baselines (net-after-cost)
    bk = metrics_at_fee(baseline_breakout, fee_bps, "micro_breakout")
    rnd = metrics_at_fee(baseline_random, fee_bps, "random_entry")
    report.baselines = {
        "micro_breakout": {"net_after_cost": bk.net_pnl, "trades": bk.trades},
        "random_entry": {"net_after_cost": rnd.net_pnl, "trades": rnd.trades},
    }

    # overfit flags
    oos_rank = sorted(by_param_oos, key=by_param_oos.get, reverse=True).index(val_best) + 1
    half = max(1, (len(by_param_oos) + 1) // 2)
    param_island = oos_rank > half
    fold_nets = [fold_metrics[n].net_pnl for n in ("train", "val", "oos")]
    single_fold_edge = sum(1 for x in fold_nets if x > 0) == 1
    low_trades = any(fold_metrics[n].trades < min_trades for n in ("train", "val", "oos"))
    report.param_stability = {
        "grid": list(candidate_runs), "val_best_param": val_best,
        "oos_rank_of_val_best": oos_rank,
        "single_fold_edge": single_fold_edge, "param_island": param_island,
    }
    report.overfit_flags = {"low_trades": low_trades,
                            "single_fold_edge": single_fold_edge,
                            "param_island": param_island}

    # verdict
    oos = fold_metrics["oos"]
    reasons: list[str] = []
    if low_trades:
        report.verdict = "insufficient_data"
        thin = [f"{n}={fold_metrics[n].trades}" for n in ("train", "val", "oos")
                if fold_metrics[n].trades < min_trades]
        reasons.append(f"folds below min_trades={min_trades}: {', '.join(thin)}")
    else:
        beats_baselines = oos.net_pnl > bk.net_pnl and oos.net_pnl > rnd.net_pnl
        ok = (oos.net_pnl > 0 and oos.profit_factor > 1.0 and beats_baselines
              and not single_fold_edge and not param_island)
        report.verdict = "validated" if ok else "not_validated"
        if not ok:
            if oos.net_pnl <= 0:
                reasons.append(f"oos net-after-cost {oos.net_pnl:.2f} <= 0")
            if oos.profit_factor <= 1.0:
                reasons.append(f"oos profit_factor {oos.profit_factor:.2f} <= 1.0")
            if not beats_baselines:
                reasons.append("oos does not beat both baselines")
            if single_fold_edge:
                reasons.append("edge appears in only one fold")
            if param_island:
                reasons.append("val-best param is an overfit island (poor oos rank)")
        else:
            reasons.append("oos positive, beats baselines, stable across params/folds")
    report.verdict_reasons = reasons
    return report


def evaluate_gate_portfolio(
    *,
    candidate_runs: dict[str, list[PortfolioPeriod]],   # param_label -> period series
    baseline_periods: list[PortfolioPeriod],
    fee_bps: float,
    min_periods: int = 30,
    fractions: tuple[float, float, float] = (0.5, 0.25, 0.25),
    candidate_name: str = "",
    hypothesis: str = "",
    symbols: list[str] | None = None,
    window: dict | None = None,
) -> GateReport:
    """Walk-forward gate for basket / cross-sectional strategies (ROB-351 Issue 1).

    Identical contract to ``evaluate_gate`` but every metric is computed on the
    PERIOD-return equity curve (concurrent positions netted per period), so the
    drawdown that ``promote_to_pilot`` / the 343 criterion gate on is the honest
    portfolio drawdown, not a per-trade serial sum.
    """
    report = GateReport(
        candidate=candidate_name, hypothesis=hypothesis, symbols=symbols or [],
        window=window or {},
        cost_model={"fee_bps_per_leg": fee_bps, "fee_grid_bps": [10.0, 7.5, 5.0, 2.0, 0.0],
                    "unit": "portfolio_period"},
    )
    if not candidate_runs:
        report.verdict = "insufficient_data"
        report.verdict_reasons = ["no candidate runs provided"]
        return report

    by_param_val: dict[str, float] = {}
    by_param_oos: dict[str, float] = {}
    folds_by_param: dict[str, dict[str, list[PortfolioPeriod]]] = {}
    for label, periods in candidate_runs.items():
        folds = walk_forward_split_periods(periods, fractions)
        folds_by_param[label] = folds
        by_param_val[label] = portfolio_metrics_at_fee(folds["val"], fee_bps, "val").net_pnl
        by_param_oos[label] = portfolio_metrics_at_fee(folds["oos"], fee_bps, "oos").net_pnl

    val_best = max(by_param_val, key=by_param_val.get)
    folds = folds_by_param[val_best]
    fold_metrics = {name: portfolio_metrics_at_fee(folds[name], fee_bps, name)
                    for name in ("train", "val", "oos")}
    report.per_fold = [asdict(fold_metrics[n]) for n in ("train", "val", "oos")]

    all_best = candidate_runs[val_best]
    report.results = {
        "gross": asdict(portfolio_metrics_at_fee(all_best, 0.0, "gross")),
        "zero_fee": asdict(portfolio_metrics_at_fee(all_best, 0.0, "zero_fee")),
        "net_after_cost": asdict(portfolio_metrics_at_fee(all_best, fee_bps, "net_after_cost")),
    }
    report.trade_count = len(all_best)

    base = portfolio_metrics_at_fee(baseline_periods, fee_bps, "baseline")
    report.baselines = {"baseline": {"net_after_cost": base.net_pnl, "trades": base.trades}}

    oos_rank = sorted(by_param_oos, key=by_param_oos.get, reverse=True).index(val_best) + 1
    half = max(1, (len(by_param_oos) + 1) // 2)
    param_island = oos_rank > half
    fold_nets = [fold_metrics[n].net_pnl for n in ("train", "val", "oos")]
    single_fold_edge = sum(1 for x in fold_nets if x > 0) == 1
    low_periods = any(fold_metrics[n].trades < min_periods for n in ("train", "val", "oos"))
    report.param_stability = {
        "grid": list(candidate_runs), "val_best_param": val_best,
        "oos_rank_of_val_best": oos_rank,
        "single_fold_edge": single_fold_edge, "param_island": param_island,
        "unit": "portfolio_period",
    }
    report.overfit_flags = {"low_trades": low_periods, "single_fold_edge": single_fold_edge,
                            "param_island": param_island}

    oos = fold_metrics["oos"]
    reasons: list[str] = []
    if low_periods:
        report.verdict = "insufficient_data"
        thin = [f"{n}={fold_metrics[n].trades}" for n in ("train", "val", "oos")
                if fold_metrics[n].trades < min_periods]
        reasons.append(f"folds below min_periods={min_periods}: {', '.join(thin)}")
    else:
        beats_baseline = oos.net_pnl > base.net_pnl
        ok = (oos.net_pnl > 0 and oos.profit_factor > 1.0 and beats_baseline
              and not single_fold_edge and not param_island)
        report.verdict = "validated" if ok else "not_validated"
        if not ok:
            if oos.net_pnl <= 0:
                reasons.append(f"oos net-after-cost {oos.net_pnl:.2f} <= 0")
            if oos.profit_factor <= 1.0:
                reasons.append(f"oos profit_factor {oos.profit_factor:.2f} <= 1.0")
            if not beats_baseline:
                reasons.append("oos does not beat the baseline")
            if single_fold_edge:
                reasons.append("edge appears in only one fold")
            if param_island:
                reasons.append("val-best param is an overfit island (poor oos rank)")
        else:
            reasons.append("oos positive, beats baseline, stable across params/folds")
    report.verdict_reasons = reasons
    return report


# --------------------------------------------------------------------------- #
# ROB-328 (ROB-327 F1) — statistical-significance + run-card additions.
#
# Additive only; the OOS/verdict/baseline/fee machinery above is unchanged.
# Pure-stdlib (random/math/hashlib) so the gate stays importable without numpy.
# Framing: ROB-316/320 already proved the scalper is net-negative on fees; these
# tools test the *statistical robustness* of that net result, not a new edge.
# --------------------------------------------------------------------------- #

RUN_CARD_SCHEMA_VERSION = "validated_run_card.v1"

_FEE_KILL_NOTE = (
    "net-after-fee is the only verdict basis. ROB-316/320 already proved the "
    "scalper is net-negative purely on fees; the bootstrap CI and Monte-Carlo "
    "permutation here test the statistical robustness of that net result, not a "
    "new edge. This run card is audit evidence, not a pass stamp."
)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sample_std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


_MAX_SHARPE = 1e6  # bound the degenerate zero-variance case (sign still preserved)


def _sharpe(xs: Sequence[float]) -> float:
    """Unitless edge-per-unit-risk: mean / sample-std (no annualization).

    Zero variance (constant series) yields a sign-preserving bounded value rather
    than an eps-division blowup, so run cards stay readable.
    """
    if not xs:
        return 0.0
    mean = _mean(xs)
    std = _sample_std(xs)
    if std == 0.0:
        return 0.0 if mean == 0.0 else math.copysign(_MAX_SHARPE, mean)
    return max(-_MAX_SHARPE, min(_MAX_SHARPE, mean / std))


def _percentile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 1]); input must be sorted."""
    if not sorted_xs:
        return 0.0
    n = len(sorted_xs)
    if n == 1:
        return sorted_xs[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_xs[int(lo)]
    frac = pos - lo
    return sorted_xs[int(lo)] * (1.0 - frac) + sorted_xs[int(hi)] * frac


def bootstrap_sharpe_ci(
    net_pnls: Sequence[float],
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict:
    """Percentile bootstrap CI for the per-trade Sharpe of net-after-fee PnLs.

    ``ci_upper < 0`` => the net edge is statistically <= 0 at ``confidence``.
    Seeded via ``random.Random`` for exact reproducibility.
    """
    n = len(net_pnls)
    if n < 2:
        return {"error": "insufficient_data", "n": n,
                "n_bootstrap": n_bootstrap, "confidence": confidence, "seed": seed}
    rng = random.Random(seed)
    observed = _sharpe(net_pnls)
    sharpes: list[float] = []
    for _ in range(n_bootstrap):
        sample = [net_pnls[rng.randrange(n)] for _ in range(n)]
        sharpes.append(_sharpe(sample))
    sharpes.sort()
    alpha = (1.0 - confidence) / 2.0
    return {
        "observed_sharpe": observed,
        "ci_lower": _percentile(sharpes, alpha),
        "ci_upper": _percentile(sharpes, 1.0 - alpha),
        "median_sharpe": _percentile(sharpes, 0.5),
        "prob_positive": sum(1 for s in sharpes if s > 0) / n_bootstrap,
        "confidence": confidence,
        "n_bootstrap": n_bootstrap,
        "seed": seed,
    }


def _equity_path_metrics(
    net_pnls: Sequence[float], base_capital: float = 10_000.0
) -> tuple[float, float]:
    """Order-dependent path metrics: (returns-Sharpe, absolute max drawdown).

    Max drawdown is absolute on the cumulative-PnL equity curve (starts at 0),
    matching ``metrics_at_fee``. The returns-Sharpe is computed relative to the
    running capital, so both are sensitive to trade ordering (the only thing a
    permutation test can move — the PnL multiset and its sum are invariant).
    """
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    running_capital = base_capital
    returns: list[float] = []
    for x in net_pnls:
        returns.append(x / running_capital if running_capital else 0.0)
        running_capital += x
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    sharpe = _sharpe(returns) if len(returns) >= 2 else 0.0
    return sharpe, max_dd


def monte_carlo_permutation(
    net_pnls: Sequence[float],
    n_sim: int = 1000,
    seed: int = 42,
    base_capital: float = 10_000.0,
) -> dict:
    """Permutation test on trade ordering (null: order carries no information).

    Shuffles the PnL sequence ``n_sim`` times and recomputes the order-dependent
    path metrics. ``p_value_*`` = fraction of permutations whose statistic is at
    least as good as observed (one-tailed; max_dd is negative, so ``>=`` means a
    less-severe drawdown).
    """
    n = len(net_pnls)
    if n < 3:
        return {"error": "insufficient_data", "n": n, "n_sim": n_sim, "seed": seed}
    rng = random.Random(seed)
    actual_sharpe, actual_max_dd = _equity_path_metrics(net_pnls, base_capital)
    ge_sharpe = 0
    ge_maxdd = 0
    pool = list(net_pnls)
    for _ in range(n_sim):
        rng.shuffle(pool)
        s, dd = _equity_path_metrics(pool, base_capital)
        if s >= actual_sharpe:
            ge_sharpe += 1
        if dd >= actual_max_dd:
            ge_maxdd += 1
    return {
        "actual_sharpe": actual_sharpe,
        "actual_max_dd": actual_max_dd,
        "p_value_sharpe": ge_sharpe / n_sim,
        "p_value_maxdd": ge_maxdd / n_sim,
        "n_sim": n_sim,
        "seed": seed,
    }


# --------------------------------------------------------------------------- #
# ROB-351 (Codex outside-voice) — statistical-honesty hardening (pure-stdlib).
#
# iid trade bootstrap is too optimistic for time-clustered crypto trades; the
# many shots (3 families + meanrev seed + parameter grids) invite data snooping;
# and a fixed n>=263 is cargo-culted. These additive helpers address each.
# --------------------------------------------------------------------------- #
def block_bootstrap_sharpe_ci(
    net_pnls: Sequence[float],
    block_size: int = 10,
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict:
    """Moving-block bootstrap CI for per-trade Sharpe (preserves autocorrelation).

    Resamples contiguous blocks of length ``block_size`` so time-clustered edge
    is not artificially de-correlated the way an iid bootstrap would. ``ci_upper
    < 0`` => net edge statistically <= 0 at ``confidence``.
    """
    n = len(net_pnls)
    if n < 2:
        return {"error": "insufficient_data", "n": n, "block_size": block_size,
                "n_bootstrap": n_bootstrap, "confidence": confidence, "seed": seed}
    block_size = max(1, min(block_size, n))
    n_blocks = math.ceil(n / block_size)
    max_start = n - block_size
    rng = random.Random(seed)
    observed = _sharpe(net_pnls)
    sharpes: list[float] = []
    for _ in range(n_bootstrap):
        sample: list[float] = []
        for _ in range(n_blocks):
            start = rng.randint(0, max_start) if max_start > 0 else 0
            sample.extend(net_pnls[start:start + block_size])
        sharpes.append(_sharpe(sample[:n]))
    sharpes.sort()
    alpha = (1.0 - confidence) / 2.0
    return {
        "observed_sharpe": observed,
        "ci_lower": _percentile(sharpes, alpha),
        "ci_upper": _percentile(sharpes, 1.0 - alpha),
        "median_sharpe": _percentile(sharpes, 0.5),
        "prob_positive": sum(1 for s in sharpes if s > 0) / n_bootstrap,
        "confidence": confidence, "n_bootstrap": n_bootstrap, "seed": seed,
        "block_size": block_size, "method": "moving_block",
    }


def benjamini_hochberg(pvalues: Sequence[float], alpha: float = 0.05) -> dict:
    """Benjamini-Hochberg FDR control across many tested hypotheses.

    Returns the set of rejected (significant) hypothesis indices and the largest
    p-value threshold that survives FDR at ``alpha``. Use this so screening three
    broad families + seed + parameter neighborhoods does not silently snoop.
    """
    m = len(pvalues)
    if m == 0:
        return {"rejected": [], "threshold": None, "n": 0, "alpha": alpha}
    order = sorted(range(m), key=lambda i: pvalues[i])
    max_k = 0
    threshold: float | None = None
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * alpha:
            max_k = rank
            threshold = pvalues[idx]
    rejected = sorted(order[:max_k]) if max_k else []
    return {"rejected": rejected, "threshold": threshold, "n": m, "alpha": alpha}


def effect_size_aware_min_trades(
    observed_sharpe: float, n_configs_tried: int = 1, target_t: float = 2.0
) -> float:
    """Minimum trade count to clear a t-stat target, inflated for multiple tests.

    Replaces the cargo-culted fixed ``n>=263``: required ``n`` depends on the
    effect size (per-trade Sharpe) and how many configs were tried. The target
    t is inflated by a maximal-inequality term ``sqrt(2*ln m)`` so more shots
    demand more evidence. Zero effect => ``inf`` (no sample size rescues it).
    """
    s = abs(observed_sharpe)
    if s == 0.0:
        return math.inf
    target_t_adj = target_t + math.sqrt(2.0 * math.log(max(1, n_configs_tried)))
    return math.ceil((target_t_adj / s) ** 2)


def turnover_matched_random_baseline(
    pnl_pool: Sequence[float], n_trades: int, seed: int = 42
) -> list[float]:
    """Random-entry baseline with the SAME trade count as the real strategy.

    Samples ``n_trades`` PnLs (with replacement) from the realized-return pool so
    a candidate's gross edge must beat dumb turnover-matched activity, not just
    cash (guards against volatility harvesting / beta / selection noise).
    """
    if not pnl_pool or n_trades <= 0:
        return []
    rng = random.Random(seed)
    pool = list(pnl_pool)
    return [pool[rng.randrange(len(pool))] for _ in range(n_trades)]


def run_card_hashes(
    config: dict,
    strategy_path: str | Path | None = None,
    artifact_paths: Sequence[str | Path] = (),
) -> dict:
    """SHA-256 reproducibility trio: config (sorted-key JSON) / strategy / artifacts."""
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
    ).hexdigest()
    strategy_hash: str | None = None
    if strategy_path is not None:
        p = Path(strategy_path)
        if p.exists():
            strategy_hash = hashlib.sha256(p.read_bytes()).hexdigest()
    artifacts: list[dict] = []
    for ap in artifact_paths:
        p = Path(ap)
        if p.exists():
            artifacts.append(
                {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            )
    return {"config_hash": config_hash, "strategy_hash": strategy_hash,
            "artifacts": artifacts}


def apply_statistical_evidence(report: GateReport, bootstrap: dict) -> GateReport:
    """Fold bootstrap evidence into the verdict (mutates ``report``).

    A negative CI upper bound is a statistical guard: it appends a reason and
    downgrades a ``validated`` verdict to ``not_validated``. A positive CI lower
    bound only appends corroborating evidence — it never upgrades, since the full
    gate (OOS/baseline/overfit) owns the ``validated`` decision.
    """
    if not bootstrap or bootstrap.get("error") is not None:
        return report
    ci_lower = bootstrap.get("ci_lower")
    ci_upper = bootstrap.get("ci_upper")
    conf = bootstrap.get("confidence", 0.95)
    if ci_upper is not None and ci_upper < 0:
        report.verdict_reasons.append(
            f"net edge statistically <= 0 (bootstrap {conf:.0%} CI upper {ci_upper:.4f} < 0)"
        )
        if report.verdict == "validated":
            report.verdict = "not_validated"
    elif ci_lower is not None and ci_lower > 0:
        report.verdict_reasons.append(
            f"net edge statistically > 0 (bootstrap {conf:.0%} CI lower {ci_lower:.4f} > 0)"
        )
    else:
        report.verdict_reasons.append(
            f"net edge not statistically distinguishable from 0 "
            f"(bootstrap {conf:.0%} CI straddles 0)"
        )
    return report


def _render_run_card_md(card: dict) -> str:
    lines = [
        f"# Validated Signal Gate — Run Card ({card['schema_version']})",
        "",
        f"- Generated: {card['generated_at']}",
        f"- Candidate: {card['candidate']}",
        f"- Hypothesis: {card['hypothesis']}",
        f"- **Verdict: {card['verdict']}**",
        "",
        "## Framing",
        card["framing"],
        "",
        "## Net-after-cost (verdict basis)",
    ]
    net = card.get("net_after_cost") or {}
    if net:
        for key in ("trades", "net_pnl", "profit_factor", "expectancy", "max_drawdown"):
            if key in net:
                lines.append(f"- {key}: {net[key]}")
    else:
        lines.append("- (no net_after_cost recorded)")

    lines += ["", "## Verdict reasons"]
    lines += [f"- {r}" for r in card["verdict_reasons"]] or ["- (none)"]

    lines += ["", "## Reproducibility"]
    repro = card["reproducibility"]
    lines.append(f"- config_hash: `{repro['config_hash']}`")
    lines.append(f"- strategy_hash: `{repro['strategy_hash']}`")
    for art in repro.get("artifacts", []):
        lines.append(f"- artifact {art['path']}: `{art['sha256']}`")

    lines += ["", "## Statistical validation"]
    val = card.get("validation") or {}
    bs = val.get("bootstrap")
    mc = val.get("monte_carlo")
    if bs and bs.get("error") is None:
        lines.append(
            f"- bootstrap Sharpe: observed {bs['observed_sharpe']:.4f}, "
            f"{bs['confidence']:.0%} CI [{bs['ci_lower']:.4f}, {bs['ci_upper']:.4f}], "
            f"prob_positive {bs['prob_positive']:.3f} (n={bs['n_bootstrap']})"
        )
    if mc and mc.get("error") is None:
        lines.append(
            f"- Monte-Carlo permutation: p_value_sharpe {mc['p_value_sharpe']:.3f}, "
            f"p_value_maxdd {mc['p_value_maxdd']:.3f} (n={mc['n_sim']})"
        )
    if not bs and not mc:
        lines.append("- (no statistical validation recorded)")

    lines += ["", "## Data sources"]
    lines += [f"- {s}" for s in card["data_sources"]] or ["- (none recorded)"]

    if card["warnings"]:
        lines += ["", "## Warnings"]
        lines += [f"- {w}" for w in card["warnings"]]

    return "\n".join(lines) + "\n"


def write_run_card(
    report: GateReport,
    out_dir: str | Path,
    *,
    config: dict | None = None,
    strategy_path: str | Path | None = None,
    artifact_paths: Sequence[str | Path] = (),
    data_sources: Sequence[str] = (),
    bootstrap: dict | None = None,
    monte_carlo: dict | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Path]:
    """Write ``run_card.json`` + ``run_card.md`` to ``out_dir`` and return paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    hashes = run_card_hashes(
        config or {}, strategy_path=strategy_path, artifact_paths=artifact_paths
    )
    net = report.results.get("net_after_cost", {}) if report.results else {}
    card = {
        "schema_version": RUN_CARD_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate": report.candidate,
        "hypothesis": report.hypothesis,
        "verdict": report.verdict,
        "verdict_reasons": list(report.verdict_reasons),
        "net_after_cost": net,
        "reproducibility": hashes,
        "data_sources": list(data_sources),
        "validation": {"bootstrap": bootstrap, "monte_carlo": monte_carlo},
        "warnings": list(warnings),
        "framing": _FEE_KILL_NOTE,
        "gate_report": report.to_dict(),
    }
    json_path = out / "run_card.json"
    json_path.write_text(json.dumps(card, indent=2))
    md_path = out / "run_card.md"
    md_path.write_text(_render_run_card_md(card))
    return {"json": json_path, "md": md_path}
