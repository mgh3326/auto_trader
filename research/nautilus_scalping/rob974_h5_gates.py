"""ROB-983 (H5, CP3) -- common hard gates, E0, and win authority.

All conditions must pass for a strategy ``historical_pass`` (H5 spec AC7-15);
every stable failure reason is collected here rather than short-circuited on
the first failing gate.

E0 (AC16-17) is the mean PRICE-ONLY ``gross_bps`` over the exact
``primary_stress17`` selected-OOS basket membership -- it excludes fixed
scenario cost and funding, and is computed from the SAME trade set as every
other primary gate (never a fourth 0bp scenario or a counterfactual column).

Win authority (AC18-20): observed win rate is the fraction of baskets with
``net_bps>0`` (a plain count-based fraction, not weight-adjusted). Per-trade
``pBE=(SL_bps+17)/(TP_bps+SL_bps)`` is weighted by gross basket notional for
S4 and equal-weight (1.0) for S3 fixed-notional trades.
``win_margin=observed_win_rate-weighted_pBE`` must independently pass
alongside (never instead of) PF.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from rob974_h5_contracts import FOLD_IDS, MetricTrade

__all__ = [
    "E0_MIN_BPS",
    "MAX_MONTHLY_CONCENTRATION",
    "MIN_POSITIVE_FOLDS",
    "MIN_TRADES_PER_FOLD",
    "PF17_MIN",
    "POOLED_E17_MIN_BPS",
    "WIN_MARGIN_MIN",
    "CommonGateResult",
    "evaluate_common_gates",
]

POOLED_E17_MIN_BPS = 5.0
PF17_MIN = 1.15
MIN_POSITIVE_FOLDS = 5
MAX_MONTHLY_CONCENTRATION = 0.50
E0_MIN_BPS = 25.0
WIN_MARGIN_MIN = 0.03
MIN_TRADES_PER_FOLD = 5

REASON_POOLED_E17_BELOW = "pooled_e17_below_5bp"
REASON_PF17_BELOW = "pf17_below_1_15"
REASON_INSUFFICIENT_POSITIVE_FOLDS = "insufficient_positive_folds"
REASON_NO_POSITIVE_MONTHS = "no_positive_months"
REASON_CONCENTRATION_ABOVE = "monthly_concentration_above_50pct"
REASON_E22_NOT_POSITIVE = "e22_not_positive"
REASON_E0_BELOW = "e0_below_25bp"
REASON_WIN_MARGIN_BELOW = "win_margin_below_3pp"
REASON_INSUFFICIENT_SAMPLE = "insufficient_sample"


@dataclass(frozen=True, slots=True)
class CommonGateResult:
    passed: bool
    reasons: tuple[str, ...]
    pooled_e17_bps: float | None
    pf17: float | None
    positive_fold_count: int
    monthly_concentration: float | None
    e22_bps: float | None
    e0_bps: float | None
    observed_win_rate: float | None
    weighted_pbe: float | None
    win_margin: float | None
    fold_trade_counts: dict[str, int]


def _utc_month_key(exit_ts_ms: int) -> str:
    dt = datetime.fromtimestamp(exit_ts_ms / 1000.0, tz=UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


def _pbe(trade: MetricTrade) -> float:
    return (trade.sl_bps + 17.0) / (trade.tp_bps + trade.sl_bps)


def _weight(trade: MetricTrade) -> float:
    return trade.gross_notional if trade.gross_notional is not None else 1.0


def evaluate_common_gates(
    *,
    primary_trades: tuple[MetricTrade, ...],
    upward_trades: tuple[MetricTrade, ...],
) -> CommonGateResult:
    reasons: set[str] = set()

    fold_trade_counts: dict[str, int] = dict.fromkeys(FOLD_IDS, 0)
    fold_net_sum: dict[str, float] = dict.fromkeys(FOLD_IDS, 0.0)
    for t in primary_trades:
        fold_trade_counts[t.fold_id] += 1
        fold_net_sum[t.fold_id] += t.net_bps

    for fold_id in FOLD_IDS:
        if fold_trade_counts[fold_id] < MIN_TRADES_PER_FOLD:
            reasons.add(REASON_INSUFFICIENT_SAMPLE)

    pooled_e17_bps: float | None = None
    pf17: float | None = None
    e0_bps: float | None = None
    observed_win_rate: float | None = None
    weighted_pbe: float | None = None
    win_margin: float | None = None

    if primary_trades:
        net_values = [t.net_bps for t in primary_trades]
        pooled_e17_bps = sum(net_values) / len(net_values)
        if pooled_e17_bps < POOLED_E17_MIN_BPS:
            reasons.add(REASON_POOLED_E17_BELOW)

        gross_profit = sum(v for v in net_values if v > 0)
        gross_loss = -sum(v for v in net_values if v < 0)
        if gross_loss == 0.0:
            pf17 = math.inf if gross_profit > 0 else float("nan")
        else:
            pf17 = gross_profit / gross_loss
        if not (pf17 >= PF17_MIN):  # NaN-safe: NaN >= x is False
            reasons.add(REASON_PF17_BELOW)

        e0_bps = sum(t.gross_bps for t in primary_trades) / len(primary_trades)
        if e0_bps < E0_MIN_BPS:
            reasons.add(REASON_E0_BELOW)

        wins = sum(1 for v in net_values if v > 0)
        observed_win_rate = wins / len(net_values)

        total_weight = sum(_weight(t) for t in primary_trades)
        weighted_pbe = sum(_pbe(t) * _weight(t) for t in primary_trades) / total_weight
        win_margin = observed_win_rate - weighted_pbe
        if win_margin < WIN_MARGIN_MIN:
            reasons.add(REASON_WIN_MARGIN_BELOW)
    else:
        reasons.add(REASON_POOLED_E17_BELOW)
        reasons.add(REASON_PF17_BELOW)
        reasons.add(REASON_E0_BELOW)
        reasons.add(REASON_WIN_MARGIN_BELOW)

    positive_fold_count = sum(
        1
        for fold_id in FOLD_IDS
        if fold_trade_counts[fold_id] > 0 and fold_net_sum[fold_id] > 0
    )
    if positive_fold_count < MIN_POSITIVE_FOLDS:
        reasons.add(REASON_INSUFFICIENT_POSITIVE_FOLDS)

    monthly_net: dict[str, float] = defaultdict(float)
    for t in primary_trades:
        monthly_net[_utc_month_key(t.exit_ts)] += t.net_bps
    positive_months = {k: v for k, v in monthly_net.items() if v > 0}
    monthly_concentration: float | None = None
    if not positive_months:
        reasons.add(REASON_NO_POSITIVE_MONTHS)
    else:
        total_positive = sum(positive_months.values())
        monthly_concentration = max(positive_months.values()) / total_positive
        if monthly_concentration > MAX_MONTHLY_CONCENTRATION:
            reasons.add(REASON_CONCENTRATION_ABOVE)

    e22_bps: float | None = None
    if upward_trades:
        e22_bps = sum(t.net_bps for t in upward_trades) / len(upward_trades)
    if e22_bps is None or not (e22_bps > 0.0):
        reasons.add(REASON_E22_NOT_POSITIVE)

    return CommonGateResult(
        passed=not reasons,
        reasons=tuple(sorted(reasons)),
        pooled_e17_bps=pooled_e17_bps,
        pf17=pf17,
        positive_fold_count=positive_fold_count,
        monthly_concentration=monthly_concentration,
        e22_bps=e22_bps,
        e0_bps=e0_bps,
        observed_win_rate=observed_win_rate,
        weighted_pbe=weighted_pbe,
        win_margin=win_margin,
        fold_trade_counts=fold_trade_counts,
    )
