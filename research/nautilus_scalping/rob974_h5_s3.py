"""ROB-983 (H5, CP4) -- S3 falsification gates and attribution.

Pooled/per-fold timeout ceilings, all-long-entry bullish-subbook upward E22
strict positivity, first-4h SL-dependence (undefined denominator is
``incomplete``, never a convenient zero/pass), symbol dependence (both the
"exactly one symbol positive" AND "other two pooled <=0" predicates
required), and exit-reason/symbol attribution (``THESIS_EXIT`` is never
``TIMEOUT``).

First-4h SL dependence: the numerator is the absolute E17 loss confined to
``holding_minutes<=240 AND exit_reason=="SL"``; the denominator is the
absolute E17 loss of ALL losing trades (``net_bps<0``, any exit reason) --
never the signed sum, which would let large losses cancel. A zero
denominator (no losing trades at all) makes the ratio structurally
undefined, not a free pass.

Symbol dependence fails only when EXACTLY ONE of the three S3 symbols has a
positive pooled E17 AND the other two symbols' pooled (combined) E17 is
``<=0``. Two-or-more positive symbols never trips this gate. Missing
evidence for any of the three symbols makes the check structurally
incomplete rather than silently skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rob974_h5_contracts import FOLD_IDS, S3_SYMBOLS, H5InputError, MetricTrade
from rob974_h5_gates import attribution_bucket, validate_selected_oos_membership

__all__ = [
    "S3_ABS_M_BIN_ORDER",
    "S3_ABS_S_BIN_ORDER",
    "S3_DIRECTION_ORDER",
    "S3_FIRST_4H_MINUTES",
    "S3_FOLD_TIMEOUT_MAX",
    "S3_POOLED_TIMEOUT_MAX",
    "S3_Q_BIN_ORDER",
    "S3_SL_DEPENDENCE_MAX",
    "S3_VOLATILITY_BIN_ORDER",
    "S3FalsificationResult",
    "evaluate_s3_falsification",
]

S3_POOLED_TIMEOUT_MAX = 0.15
S3_FOLD_TIMEOUT_MAX = 0.25
S3_SL_DEPENDENCE_MAX = 0.50
S3_FIRST_4H_MINUTES = 240.0

S3_ABS_S_BIN_ORDER = ("[1.25,1.75)", "[1.75,2.50)", "[2.50,inf)")
S3_Q_BIN_ORDER = ("[q_min,0.50)", "[0.50,0.85)", "[0.85,1.25]")
S3_ABS_M_BIN_ORDER = ("[0.0075,0.015)", "[0.015,0.03)", "[0.03,inf)")
S3_VOLATILITY_BIN_ORDER = ("[20,40)", "[40,60)", "[60,75)", "[75,90]")
S3_DIRECTION_ORDER = ("Long", "Short")

REASON_POOLED_TIMEOUT_ABOVE = "s3_pooled_timeout_above_15pct"
REASON_FOLD_TIMEOUT_ABOVE = "s3_fold_timeout_above_25pct"
REASON_BULLISH_LONG_E22_NOT_POSITIVE = "s3_bullish_long_e22_not_positive"
REASON_FIRST_4H_SL_DEPENDENCE_ABOVE = "s3_first_4h_sl_dependence_above_50pct"
REASON_SYMBOL_DEPENDENCE = "s3_symbol_dependence"

INCOMPLETE_FIRST_4H_SL_DENOMINATOR_UNDEFINED = "s3_first_4h_sl_denominator_undefined"
INCOMPLETE_SYMBOL_EVIDENCE_MISSING = "s3_symbol_evidence_missing"
INCOMPLETE_POOLED_TIMEOUT_UNDEFINED = "s3_pooled_timeout_undefined"


@dataclass(frozen=True, slots=True)
class S3FalsificationResult:
    passed: bool
    reasons: tuple[str, ...]
    incomplete_reasons: tuple[str, ...]
    pooled_timeout_ratio: float
    fold_timeout_ratios: dict[str, float]
    bullish_long_e22_bps: float | None
    first_4h_sl_dependence: float | None
    attribution: dict[str, object]


def _profit_factor(net_values: list[float]) -> float | None:
    if not net_values:
        return None
    profit = sum(v for v in net_values if v > 0)
    loss = -sum(v for v in net_values if v < 0)
    if loss == 0.0:
        return math.inf if profit > 0 else float("nan")
    return profit / loss


def _bucket(trades: tuple[MetricTrade, ...]) -> dict[str, float | int | None]:
    net_values = [t.net_bps for t in trades]
    gross_values = [t.gross_bps for t in trades]
    holding_values = [t.holding_minutes for t in trades]
    return {
        "trades": len(trades),
        "e17_bps": sum(net_values) / len(net_values) if net_values else None,
        "e0_bps": sum(gross_values) / len(gross_values) if gross_values else None,
        "pf": _profit_factor(net_values),
        "avg_holding_minutes": (
            sum(holding_values) / len(holding_values) if holding_values else None
        ),
    }


def _s3_abs_s_bin(value: float) -> str:
    value = abs(value)
    if 1.25 <= value < 1.75:
        return S3_ABS_S_BIN_ORDER[0]
    if 1.75 <= value < 2.50:
        return S3_ABS_S_BIN_ORDER[1]
    if value >= 2.50:
        return S3_ABS_S_BIN_ORDER[2]
    raise H5InputError("s3_attribution_abs_S_outside_registered_bins")


def _s3_q_bin(value: float, q_min: float) -> str:
    if q_min <= value < 0.50:
        return S3_Q_BIN_ORDER[0]
    if 0.50 <= value < 0.85:
        return S3_Q_BIN_ORDER[1]
    if 0.85 <= value <= 1.25:
        return S3_Q_BIN_ORDER[2]
    raise H5InputError("s3_attribution_Q_outside_registered_bins")


def _s3_abs_m_bin(value: float) -> str:
    value = abs(value)
    if 0.0075 <= value < 0.015:
        return S3_ABS_M_BIN_ORDER[0]
    if 0.015 <= value < 0.03:
        return S3_ABS_M_BIN_ORDER[1]
    if value >= 0.03:
        return S3_ABS_M_BIN_ORDER[2]
    raise H5InputError("s3_attribution_abs_M_outside_registered_bins")


def _s3_volatility_bin(value: float) -> str:
    if 20.0 <= value < 40.0:
        return S3_VOLATILITY_BIN_ORDER[0]
    if 40.0 <= value < 60.0:
        return S3_VOLATILITY_BIN_ORDER[1]
    if 60.0 <= value < 75.0:
        return S3_VOLATILITY_BIN_ORDER[2]
    if 75.0 <= value <= 90.0:
        return S3_VOLATILITY_BIN_ORDER[3]
    raise H5InputError("s3_attribution_volatility_outside_registered_bins")


def _s3_registered_attribution(
    trades: tuple[MetricTrade, ...],
) -> dict[str, object]:
    by_abs_s: dict[str, list[MetricTrade]] = {name: [] for name in S3_ABS_S_BIN_ORDER}
    by_q: dict[str, list[MetricTrade]] = {name: [] for name in S3_Q_BIN_ORDER}
    by_abs_m: dict[str, list[MetricTrade]] = {name: [] for name in S3_ABS_M_BIN_ORDER}
    by_volatility: dict[str, list[MetricTrade]] = {
        name: [] for name in S3_VOLATILITY_BIN_ORDER
    }
    top_cross: dict[str, dict[str, list[MetricTrade]]] = {
        direction: {name: [] for name in S3_ABS_M_BIN_ORDER}
        for direction in S3_DIRECTION_ORDER
    }
    for trade in trades:
        attribution = trade.attribution
        if (
            attribution is None
            or attribution.S is None
            or attribution.Q is None
            or attribution.q_min is None
            or attribution.volatility_percentile is None
            or attribution.market_return_tercile is None
        ):
            raise H5InputError("s3_registered_attribution_row_incomplete")
        abs_m_bin = _s3_abs_m_bin(attribution.market_return)
        by_abs_s[_s3_abs_s_bin(attribution.S)].append(trade)
        by_q[_s3_q_bin(attribution.Q, attribution.q_min)].append(trade)
        by_abs_m[abs_m_bin].append(trade)
        by_volatility[_s3_volatility_bin(attribution.volatility_percentile)].append(
            trade
        )
        direction = {"long": "Long", "short": "Short"}.get(trade.direction)
        if direction is None:
            raise H5InputError("s3_attribution_direction_unknown")
        if attribution.market_return_tercile == "top":
            top_cross[direction][abs_m_bin].append(trade)
    return {
        "by_abs_S_bin": {
            name: attribution_bucket(tuple(by_abs_s[name]))
            for name in S3_ABS_S_BIN_ORDER
        },
        "by_pullback_Q_bin": {
            name: attribution_bucket(tuple(by_q[name])) for name in S3_Q_BIN_ORDER
        },
        "by_abs_M_bin": {
            name: attribution_bucket(tuple(by_abs_m[name]))
            for name in S3_ABS_M_BIN_ORDER
        },
        "by_volatility_percentile_bin": {
            name: attribution_bucket(tuple(by_volatility[name]))
            for name in S3_VOLATILITY_BIN_ORDER
        },
        "top_tercile_by_direction_and_abs_M_bin": {
            direction: {
                name: attribution_bucket(tuple(top_cross[direction][name]))
                for name in S3_ABS_M_BIN_ORDER
            }
            for direction in S3_DIRECTION_ORDER
        },
    }


def evaluate_s3_falsification(
    *,
    primary_trades: tuple[MetricTrade, ...],
    upward_trades: tuple[MetricTrade, ...],
) -> S3FalsificationResult:
    validate_selected_oos_membership(
        primary_trades=primary_trades,
        upward_trades=upward_trades,
        authority="s3_falsification",
        expected_strategy="S3",
    )

    reasons: set[str] = set()
    incomplete_reasons: set[str] = set()

    # -- Pooled timeout ratio (AC: pooled <=15%). --------------------------
    total = len(primary_trades)
    timeout_count = sum(1 for t in primary_trades if t.exit_reason == "TIMEOUT")
    if total == 0:
        pooled_timeout_ratio = 0.0
        incomplete_reasons.add(INCOMPLETE_POOLED_TIMEOUT_UNDEFINED)
    else:
        pooled_timeout_ratio = timeout_count / total
        if pooled_timeout_ratio > S3_POOLED_TIMEOUT_MAX:
            reasons.add(REASON_POOLED_TIMEOUT_ABOVE)

    # -- Per-fold timeout ratio (AC: every fold <=25%). --------------------
    fold_counts: dict[str, int] = dict.fromkeys(FOLD_IDS, 0)
    fold_timeouts: dict[str, int] = dict.fromkeys(FOLD_IDS, 0)
    for t in primary_trades:
        fold_counts[t.fold_id] += 1
        if t.exit_reason == "TIMEOUT":
            fold_timeouts[t.fold_id] += 1
    fold_timeout_ratios: dict[str, float] = {}
    for fold_id in FOLD_IDS:
        if fold_counts[fold_id] == 0:
            continue
        ratio = fold_timeouts[fold_id] / fold_counts[fold_id]
        fold_timeout_ratios[fold_id] = ratio
        if ratio > S3_FOLD_TIMEOUT_MAX:
            reasons.add(REASON_FOLD_TIMEOUT_ABOVE)

    # -- Bullish all-long-entry upward E22 (AC: strict >0). ----------------
    long_upward_net = [t.net_bps for t in upward_trades if t.direction == "long"]
    bullish_long_e22_bps = (
        sum(long_upward_net) / len(long_upward_net) if long_upward_net else None
    )
    if bullish_long_e22_bps is None or not (bullish_long_e22_bps > 0.0):
        reasons.add(REASON_BULLISH_LONG_E22_NOT_POSITIVE)

    # -- First-4h SL dependence (AC: strict >50% fails). --------------------
    all_loss_magnitudes = [abs(t.net_bps) for t in primary_trades if t.net_bps < 0.0]
    denominator = sum(all_loss_magnitudes)
    first_4h_sl_dependence: float | None = None
    if denominator == 0.0:
        incomplete_reasons.add(INCOMPLETE_FIRST_4H_SL_DENOMINATOR_UNDEFINED)
    else:
        numerator = sum(
            abs(t.net_bps)
            for t in primary_trades
            if t.net_bps < 0.0
            and t.exit_reason == "SL"
            and t.holding_minutes <= S3_FIRST_4H_MINUTES
        )
        first_4h_sl_dependence = numerator / denominator
        if first_4h_sl_dependence > S3_SL_DEPENDENCE_MAX:
            reasons.add(REASON_FIRST_4H_SL_DEPENDENCE_ABOVE)

    # -- Symbol dependence (AC: exactly-one-positive AND others pooled<=0). -
    symbol_trades: dict[str, list[MetricTrade]] = {s: [] for s in S3_SYMBOLS}
    for t in primary_trades:
        if t.dimension in symbol_trades:
            symbol_trades[t.dimension].append(t)
    missing_symbols = [s for s in S3_SYMBOLS if not symbol_trades[s]]
    if missing_symbols:
        incomplete_reasons.add(INCOMPLETE_SYMBOL_EVIDENCE_MISSING)
    else:
        symbol_e17 = {
            s: sum(t.net_bps for t in trades) / len(trades)
            for s, trades in symbol_trades.items()
        }
        positive_symbols = [s for s, v in symbol_e17.items() if v > 0.0]
        if len(positive_symbols) == 1:
            lone = positive_symbols[0]
            others = [t for s in S3_SYMBOLS if s != lone for t in symbol_trades[s]]
            others_pooled_e17 = (
                sum(t.net_bps for t in others) / len(others) if others else 0.0
            )
            if others_pooled_e17 <= 0.0:
                reasons.add(REASON_SYMBOL_DEPENDENCE)

    # -- Attribution: exit-reason and symbol breakdowns. --------------------
    exit_groups: dict[str, list[MetricTrade]] = {}
    for t in primary_trades:
        exit_groups.setdefault(t.exit_reason, []).append(t)
    by_exit_reason = {
        reason: _bucket(tuple(trades)) for reason, trades in exit_groups.items()
    }
    by_symbol = {
        s: _bucket(tuple(trades)) for s, trades in symbol_trades.items() if trades
    }

    attribution: dict[str, object] = {
        "by_exit_reason": by_exit_reason,
        "by_symbol": by_symbol,
    }
    if primary_trades and primary_trades[0].attribution is not None:
        attribution.update(_s3_registered_attribution(primary_trades))

    return S3FalsificationResult(
        passed=not reasons,
        reasons=tuple(sorted(reasons)),
        incomplete_reasons=tuple(sorted(incomplete_reasons)),
        pooled_timeout_ratio=pooled_timeout_ratio,
        fold_timeout_ratios=fold_timeout_ratios,
        bullish_long_e22_bps=bullish_long_e22_bps,
        first_4h_sl_dependence=first_4h_sl_dependence,
        attribution=attribution,
    )
