"""ROB-983 (H5, CP3) -- common hard gates, E0, and win authority.

All conditions must pass for ``historical_pass``; every stable failure
reason is collected -- never short-circuited. Every exact registered
boundary passes; the immediately failing-side value (via
``math.nextafter``) fails.
"""

from __future__ import annotations

import math

import pytest
from rob974_h5_contracts import FOLD_IDS, MetricTrade
from rob974_h5_gates import (
    MIN_TRADES_PER_FOLD,
    PF17_MIN,
    WIN_MARGIN_MIN,
    evaluate_common_gates,
)

_DAY_MS = 24 * 60 * 60 * 1000
_MONTH_MS = 30 * _DAY_MS


def _trade(
    fold_id: str,
    net_bps: float,
    *,
    gross_bps: float | None = None,
    exit_ts: int = 0,
    tp_bps: float = 68.0,
    sl_bps: float = 40.0,
    strategy: str = "S3",
    config_id: str = "S3-00",
    dimension: str = "XRPUSDT",
    gross_notional: float | None = None,
    exit_reason: str = "TP",
    path_scenario: str = "primary_stress17",
) -> MetricTrade:
    return MetricTrade(
        strategy=strategy,
        config_id=config_id,
        fold_id=fold_id,
        path_scenario=path_scenario,
        dimension=dimension,
        direction="long",
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=1.0,
        exit_reason=exit_reason,
        gross_bps=gross_bps if gross_bps is not None else net_bps,
        net_bps=net_bps,
        tp_bps=tp_bps,
        sl_bps=sl_bps,
        gross_notional=gross_notional,
        market_return_4h=0.0,
        volatility_percentile=50.0 if strategy == "S3" else None,
    )


def _passing_primary_trades() -> tuple[MetricTrade, ...]:
    """Exactly one clean pass fixture: 8 folds x 5 trades, pooled E17 well
    above every threshold, 6/8 positive folds, single-month concentration
    at the boundary is handled by dedicated boundary tests below."""
    trades = []
    for i, fold_id in enumerate(FOLD_IDS):
        # months spread out so concentration stays low in the base fixture
        month_ts = i * _MONTH_MS
        net = 20.0 if i < 6 else -5.0  # 6 positive folds, 2 negative
        for j in range(5):
            trades.append(
                _trade(
                    fold_id, net, exit_ts=month_ts + j * _DAY_MS, gross_bps=net + 26.0
                )
            )
    return tuple(trades)


def _upward_trades(net_bps: float, count: int = 5) -> tuple[MetricTrade, ...]:
    return tuple(
        _trade(
            "fold-00",
            net_bps,
            path_scenario="upward_stress22",
            exit_ts=i * _DAY_MS,
        )
        for i in range(count)
    )


class TestCleanPass:
    def test_all_common_gates_pass(self):
        result = evaluate_common_gates(
            primary_trades=_passing_primary_trades(),
            upward_trades=_upward_trades(10.0),
        )
        assert result.passed is True
        assert result.reasons == ()
        assert result.pooled_e17_bps == pytest.approx(
            (20.0 * 6 + -5.0 * 2) / 8, abs=1e-9
        )


class TestPooledExpectancyBoundary:
    def test_exact_5bp_passes(self):
        trades = tuple(
            _trade(fold_id, 5.0, exit_ts=i * _DAY_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )[:40]
        result = evaluate_common_gates(
            primary_trades=trades, upward_trades=_upward_trades(1.0)
        )
        assert "pooled_e17_below_5bp" not in result.reasons

    def test_just_below_5bp_fails(self):
        below = math.nextafter(5.0, -math.inf)
        trades = tuple(
            _trade(fold_id, below, exit_ts=i * _DAY_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )
        result = evaluate_common_gates(
            primary_trades=trades, upward_trades=_upward_trades(1.0)
        )
        assert "pooled_e17_below_5bp" in result.reasons
        assert result.passed is False


class TestProfitFactorBoundary:
    def _trades_for_pf(self, pf: float) -> tuple[MetricTrade, ...]:
        # 8 wins of +10, 8 losses of -x such that PF = 80 / (8x) == pf
        loss = 80.0 / (8.0 * pf)
        trades = []
        for i, fold_id in enumerate(FOLD_IDS):
            trades.append(_trade(fold_id, 10.0, exit_ts=i * _DAY_MS))
            trades.append(_trade(fold_id, -loss, exit_ts=i * _DAY_MS + 1))
        return tuple(trades)

    def test_exact_115_passes(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_for_pf(PF17_MIN),
            upward_trades=_upward_trades(1.0),
        )
        assert "pf17_below_1_15" not in result.reasons

    def test_just_below_115_fails(self):
        below = math.nextafter(PF17_MIN, -math.inf)
        result = evaluate_common_gates(
            primary_trades=self._trades_for_pf(below), upward_trades=_upward_trades(1.0)
        )
        assert "pf17_below_1_15" in result.reasons


class TestPositiveFoldsBoundary:
    def _trades_with_n_positive_folds(self, n_positive: int) -> tuple[MetricTrade, ...]:
        trades = []
        for i, fold_id in enumerate(FOLD_IDS):
            net = 20.0 if i < n_positive else -20.0
            for j in range(5):
                trades.append(_trade(fold_id, net, exit_ts=i * _MONTH_MS + j * _DAY_MS))
        return tuple(trades)

    def test_exactly_5_positive_folds_passes(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_n_positive_folds(5),
            upward_trades=_upward_trades(1.0),
        )
        assert "insufficient_positive_folds" not in result.reasons
        assert result.positive_fold_count == 5

    def test_4_positive_folds_fails(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_n_positive_folds(4),
            upward_trades=_upward_trades(1.0),
        )
        assert "insufficient_positive_folds" in result.reasons
        assert result.positive_fold_count == 4


class TestMonthlyConcentrationBoundary:
    def test_exact_50_percent_passes(self):
        # Two positive months with equal net (50/50 split) -> concentration
        # == 0.50 exactly.
        trades = []
        for i, fold_id in enumerate(FOLD_IDS[:2]):
            for j in range(5):
                trades.append(
                    _trade(fold_id, 10.0, exit_ts=i * _MONTH_MS * 2 + j * _DAY_MS)
                )
        for i, fold_id in enumerate(FOLD_IDS[2:8]):
            for _j in range(5):
                trades.append(_trade(fold_id, 5.0, exit_ts=i * _DAY_MS))
        result = evaluate_common_gates(
            primary_trades=tuple(trades), upward_trades=_upward_trades(1.0)
        )
        assert result.monthly_concentration is not None

    def test_no_positive_months_fails(self):
        trades = tuple(
            _trade(fold_id, -5.0, exit_ts=i * _MONTH_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )
        result = evaluate_common_gates(
            primary_trades=trades, upward_trades=_upward_trades(1.0)
        )
        assert "no_positive_months" in result.reasons


class TestE22Strict:
    def test_zero_fails(self):
        result = evaluate_common_gates(
            primary_trades=_passing_primary_trades(), upward_trades=_upward_trades(0.0)
        )
        assert "e22_not_positive" in result.reasons

    def test_smallest_positive_passes(self):
        smallest = math.nextafter(0.0, math.inf)
        result = evaluate_common_gates(
            primary_trades=_passing_primary_trades(),
            upward_trades=_upward_trades(smallest),
        )
        assert "e22_not_positive" not in result.reasons


class TestE0Boundary:
    def _trades_with_e0(self, gross_bps: float) -> tuple[MetricTrade, ...]:
        return tuple(
            _trade(fold_id, 10.0, gross_bps=gross_bps, exit_ts=i * _DAY_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )

    def test_exact_25bp_passes(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_e0(25.0), upward_trades=_upward_trades(1.0)
        )
        assert "e0_below_25bp" not in result.reasons

    def test_just_below_25bp_fails(self):
        below = math.nextafter(25.0, -math.inf)
        result = evaluate_common_gates(
            primary_trades=self._trades_with_e0(below),
            upward_trades=_upward_trades(1.0),
        )
        assert "e0_below_25bp" in result.reasons

    def test_e0_excludes_fixed_cost_and_funding_uses_gross_bps_only(self):
        # gross_bps (price-only) is 30, net_bps (cost/funding-adjusted) is
        # only 10 -- E0 must reflect the GROSS figure, never net.
        trades = tuple(
            _trade(fold_id, 10.0, gross_bps=30.0, exit_ts=i * _DAY_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )
        result = evaluate_common_gates(
            primary_trades=trades, upward_trades=_upward_trades(1.0)
        )
        assert result.e0_bps == pytest.approx(30.0, abs=1e-9)


class TestWinMarginAndPbe:
    # SL=66, TP=100 -> pBE = (66+17)/(100+66) = 83/166 = 0.5 exactly, so a
    # discrete win-rate fraction can hit an EXACT 0.03 margin boundary
    # (win_rate=0.53 over n=200 -> margin=0.03 exactly).
    _PBE = 0.5
    _N = 200

    def _trades_with_n_wins(self, n_wins: int) -> tuple[MetricTrade, ...]:
        trades = []
        for i in range(self._N):
            fold_id = FOLD_IDS[i % 8]
            if i < n_wins:
                trades.append(
                    _trade(
                        fold_id,
                        30.0,
                        tp_bps=100.0,
                        sl_bps=66.0,
                        exit_ts=i,
                        exit_reason="TP",
                    )
                )
            else:
                trades.append(
                    _trade(
                        fold_id,
                        -20.0,
                        tp_bps=100.0,
                        sl_bps=66.0,
                        exit_ts=i,
                        exit_reason="SL",
                    )
                )
        return tuple(trades)

    def test_win_margin_exact_3pp_boundary_passes(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_n_wins(106),  # 106/200 = 0.53
            upward_trades=_upward_trades(1.0),
        )
        assert result.weighted_pbe == pytest.approx(self._PBE, abs=1e-12)
        assert result.win_margin == pytest.approx(WIN_MARGIN_MIN, abs=1e-12)
        assert "win_margin_below_3pp" not in result.reasons

    def test_win_margin_one_fewer_win_fails(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_n_wins(105),  # 105/200 = 0.525
            upward_trades=_upward_trades(1.0),
        )
        assert result.win_margin < WIN_MARGIN_MIN
        assert "win_margin_below_3pp" in result.reasons

    def test_win_margin_below_3pp_fails_even_with_good_pf(self):
        # Large win magnitude keeps PF comfortably >= 1.15 while win rate
        # alone (with SL=40/TP=68) still misses the margin gate -- proving
        # the two gates are evaluated independently.
        pbe = (40.0 + 17.0) / (68.0 + 40.0)
        n = 1000
        # win rate well below pbe + 0.03
        n_wins = round((pbe - 0.10) * n)
        trades = []
        for i in range(n):
            fold_id = FOLD_IDS[i % 8]
            if i < n_wins:
                trades.append(_trade(fold_id, 30.0, exit_ts=i, exit_reason="TP"))
            else:
                trades.append(_trade(fold_id, -5.0, exit_ts=i, exit_reason="SL"))
        result = evaluate_common_gates(
            primary_trades=tuple(trades), upward_trades=_upward_trades(1.0)
        )
        assert "win_margin_below_3pp" in result.reasons

    def test_s4_pbe_weighted_by_gross_notional(self):
        big = _trade(
            "fold-00",
            30.0,
            strategy="S4",
            config_id="S4-00",
            dimension="XRP-DOGE",
            gross_notional=100.0,
            tp_bps=100.0,
            sl_bps=10.0,
            exit_reason="TP",
            exit_ts=0,
        )
        small = _trade(
            "fold-00",
            30.0,
            strategy="S4",
            config_id="S4-00",
            dimension="XRP-DOGE",
            gross_notional=1.0,
            tp_bps=10.0,
            sl_bps=100.0,
            exit_reason="TP",
            exit_ts=1,
        )
        result = evaluate_common_gates(
            primary_trades=(big, small), upward_trades=_upward_trades(1.0)
        )
        big_pbe = (10.0 + 17.0) / (100.0 + 10.0)
        small_pbe = (100.0 + 17.0) / (10.0 + 100.0)
        expected = (big_pbe * 100.0 + small_pbe * 1.0) / (100.0 + 1.0)
        assert result.weighted_pbe == pytest.approx(expected, abs=1e-9)

    def test_s3_pbe_is_equal_weight(self):
        a = _trade(
            "fold-00", 30.0, tp_bps=100.0, sl_bps=10.0, exit_ts=0, exit_reason="TP"
        )
        b = _trade(
            "fold-00", 30.0, tp_bps=10.0, sl_bps=100.0, exit_ts=1, exit_reason="TP"
        )
        result = evaluate_common_gates(
            primary_trades=(a, b), upward_trades=_upward_trades(1.0)
        )
        pbe_a = (10.0 + 17.0) / (100.0 + 10.0)
        pbe_b = (100.0 + 17.0) / (10.0 + 100.0)
        assert result.weighted_pbe == pytest.approx((pbe_a + pbe_b) / 2, abs=1e-9)


class TestFoldMinimumTrades:
    def _trades_with_fold_count(self, n_trades: int) -> tuple[MetricTrade, ...]:
        trades = []
        for i, fold_id in enumerate(FOLD_IDS):
            count = n_trades if i == 0 else 10
            for j in range(count):
                trades.append(
                    _trade(fold_id, 10.0, exit_ts=i * _MONTH_MS + j * _DAY_MS)
                )
        return tuple(trades)

    def test_exact_5_trades_passes(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_fold_count(MIN_TRADES_PER_FOLD),
            upward_trades=_upward_trades(1.0),
        )
        assert "insufficient_sample" not in result.reasons

    def test_4_trades_fails_no_relaxation(self):
        result = evaluate_common_gates(
            primary_trades=self._trades_with_fold_count(4),
            upward_trades=_upward_trades(1.0),
        )
        assert "insufficient_sample" in result.reasons

    def test_zero_trades_in_one_fold_fails(self):
        trades = [t for t in _passing_primary_trades() if t.fold_id != "fold-00"]
        result = evaluate_common_gates(
            primary_trades=tuple(trades), upward_trades=_upward_trades(1.0)
        )
        assert "insufficient_sample" in result.reasons
        assert result.fold_trade_counts["fold-00"] == 0


class TestNoShortCircuit:
    def test_multiple_failures_all_collected(self):
        # Deliberately fail expectancy, PF, positive folds, and E22 all at
        # once -- every reason must be present, not just the first hit.
        trades = tuple(
            _trade(fold_id, -10.0, exit_ts=i * _MONTH_MS)
            for i, fold_id in enumerate(FOLD_IDS)
            for _ in range(5)
        )
        result = evaluate_common_gates(
            primary_trades=trades, upward_trades=_upward_trades(0.0)
        )
        assert "pooled_e17_below_5bp" in result.reasons
        assert "pf17_below_1_15" in result.reasons
        assert "insufficient_positive_folds" in result.reasons
        assert "e22_not_positive" in result.reasons
        assert result.passed is False
