"""ROB-320 — pure validated-signal gate.

Consumes chronological trade lists (no Nautilus) and produces a GateReport with
a ``validated`` / ``not_validated`` / ``insufficient_data`` verdict, walk-forward
fold metrics, gross/zero-fee/net-after-cost separation, baseline comparison, and
concrete overfit flags. Net at any fee is recomputed analytically from the
reference-fee run (same method as fee_sweep / compare_strategies).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

REF_FEE_BPS = 10.0
Verdict = Literal["validated", "not_validated", "insufficient_data"]


@dataclass(frozen=True)
class Trade:
    net_ref_pnl: float      # realized pnl at REF_FEE_BPS
    commission_ref: float   # commission paid at REF_FEE_BPS (negative)
    notional: float
    ts_opened: int


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
    scale = 1.0 - fee_bps / REF_FEE_BPS
    return t.net_ref_pnl + t.commission_ref * scale


def metrics_at_fee(trades: list[Trade], fee_bps: float, fold: str = "") -> FoldMetrics:
    rows = sorted(trades, key=lambda t: t.ts_opened)
    nets = [_net_at_fee(t, fee_bps) for t in rows]
    n = len(nets)
    if n == 0:
        return FoldMetrics(fold, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0)
    equity = peak = mdd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        mdd = min(mdd, equity - peak)
    return FoldMetrics(
        fold=fold, trades=n, net_pnl=sum(nets),
        win_rate_pct=100.0 * len(wins) / n,
        max_drawdown=mdd, profit_factor=pf, expectancy=sum(nets) / n,
    )


def walk_forward_split(
    trades: list[Trade], fractions: tuple[float, float, float] = (0.5, 0.25, 0.25)
) -> dict[str, list[Trade]]:
    rows = sorted(trades, key=lambda t: t.ts_opened)
    n = len(rows)
    n_train = int(n * fractions[0])
    n_val = int(n * fractions[1])
    return {
        "train": rows[:n_train],
        "val": rows[n_train:n_train + n_val],
        "oos": rows[n_train + n_val:],
    }


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
