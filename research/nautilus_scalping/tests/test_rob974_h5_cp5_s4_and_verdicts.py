"""ROB-983 (H5, CP5) -- S4 falsification gates, pair-executor historical
state, direct-verdict tri-state, and campaign decision.

Pooled/per-fold S4 timeout ceilings, ``M_t>+3%`` upward-subbook strict
positivity, ``abs(Corr(pair gross return, M_t))<=0.15``, pair concentration
(conditional -- share ``>0.70`` AND other-two-pooled ``<=0``), slow-only
failure (``[8h,32h)`` non-positive AND ``[32h,48h]`` positive), and
exit-reason/pair attribution (``MEAN_EXIT``/``STALL_EXIT`` never leak into
``TIMEOUT``).

S4 is historical-screen-only: ``S4PairExecutorState`` is a fixed literal
(``volatility_percentile``/counts are exactly ``None``, never ``0``;
``demo_eligible`` is exactly ``False``).

Direct verdicts (``compute_direct_verdict``) are incomplete-first, then
hard-gate-fail, then pass -- independent of the campaign decision table
(``compute_campaign_decision``), which never overwrites them. Observable S4
superiority (``s4_shows_observable_superiority``) and both-pass ranking
(``rank_both_pass``) are report-only.
"""

from __future__ import annotations

import dataclasses

import pytest
from rob974_h5_contracts import FOLD_IDS, H5InputError, MetricTrade
from rob974_h5_s4 import (
    S4_HISTORICAL_PAIR_EXECUTOR_STATE,
    S4_POOLED_TIMEOUT_MAX,
    S4_SLOW_BUCKET_MID_MINUTES,
    S4PairExecutorState,
    StrategyRankMetrics,
    compute_campaign_decision,
    compute_direct_verdict,
    evaluate_s4_falsification,
    rank_both_pass,
    s4_shows_observable_superiority,
)


def _trade(
    fold_id: str,
    net_bps: float,
    *,
    exit_reason: str = "TP",
    holding_minutes: float = 10.0,
    dimension: str = "XRP-DOGE",
    direction: str = "long",
    exit_ts: int = 0,
    market_return_4h: float = 0.01,
    gross_notional: float | None = 100.0,
    path_scenario: str = "primary_stress17",
) -> MetricTrade:
    return MetricTrade(
        strategy="S4",
        config_id="S4-00",
        fold_id=fold_id,
        path_scenario=path_scenario,
        dimension=dimension,
        direction=direction,
        entry_ts=exit_ts,
        exit_ts=exit_ts + 60_000,
        holding_minutes=holding_minutes,
        exit_reason=exit_reason,
        gross_bps=net_bps + 5.0,
        net_bps=net_bps,
        tp_bps=68.0,
        sl_bps=40.0,
        gross_notional=gross_notional,
        market_return_4h=market_return_4h,
        volatility_percentile=None,
    )


def _uniform_trades(n: int, exit_reason: str = "TP", net_bps: float = 10.0):
    return tuple(
        _trade(FOLD_IDS[i % 8], net_bps, exit_reason=exit_reason, exit_ts=i)
        for i in range(n)
    )


def _upward_trades(n: int, net_bps: float = 10.0):
    # D3 fix: the upward (E22) subbook must carry path_scenario ==
    # "upward_stress22" -- never the primary book's "primary_stress17".
    return tuple(
        _trade(FOLD_IDS[i % 8], net_bps, exit_ts=i, path_scenario="upward_stress22")
        for i in range(n)
    )


class TestPooledTimeoutBoundary:
    def _trades_with_timeout_ratio(self, ratio: float, n: int = 1000) -> tuple:
        n_timeout = round(ratio * n)
        trades = []
        for i in range(n):
            reason = "TIMEOUT" if i < n_timeout else "TP"
            trades.append(_trade(FOLD_IDS[i % 8], 10.0, exit_reason=reason, exit_ts=i))
        return tuple(trades)

    def test_exact_20pct_passes(self):
        result = evaluate_s4_falsification(
            primary_trades=self._trades_with_timeout_ratio(S4_POOLED_TIMEOUT_MAX),
            upward_trades=_upward_trades(10),
        )
        assert "s4_pooled_timeout_above_20pct" not in result.reasons

    def test_above_20pct_fails(self):
        result = evaluate_s4_falsification(
            primary_trades=self._trades_with_timeout_ratio(0.201),
            upward_trades=_upward_trades(10),
        )
        assert "s4_pooled_timeout_above_20pct" in result.reasons


class TestFoldTimeoutBoundary:
    def test_one_fold_above_30pct_fails_even_if_pooled_ok(self):
        trades = []
        for i in range(10):
            reason = "TIMEOUT" if i < 4 else "TP"  # 4/10 = 40% > 30%
            trades.append(_trade("fold-00", 10.0, exit_reason=reason, exit_ts=i))
        for fold_id in FOLD_IDS[1:]:
            for i in range(50):
                trades.append(_trade(fold_id, 10.0, exit_reason="TP", exit_ts=i))
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s4_fold_timeout_above_30pct" in result.reasons
        assert "s4_pooled_timeout_above_20pct" not in result.reasons

    def test_exact_30pct_in_one_fold_passes_that_fold(self):
        trades = []
        for i in range(20):
            reason = "TIMEOUT" if i < 6 else "TP"  # 6/20 = 30%
            trades.append(_trade("fold-00", 10.0, exit_reason=reason, exit_ts=i))
        for fold_id in FOLD_IDS[1:]:
            for i in range(20):
                trades.append(_trade(fold_id, 10.0, exit_reason="TP", exit_ts=i))
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s4_fold_timeout_above_30pct" not in result.reasons


class TestHighMarketReturnE22:
    def test_positive_high_m_subbook_passes(self):
        highs = tuple(
            _trade(
                "fold-00",
                5.0,
                market_return_4h=0.05,
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s4_falsification(
            primary_trades=_uniform_trades(40), upward_trades=highs
        )
        assert "s4_high_market_return_e22_not_positive" not in result.reasons

    def test_zero_high_m_subbook_fails(self):
        highs = tuple(
            _trade(
                "fold-00",
                0.0,
                market_return_4h=0.05,
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s4_falsification(
            primary_trades=_uniform_trades(40), upward_trades=highs
        )
        assert "s4_high_market_return_e22_not_positive" in result.reasons

    def test_low_m_trades_excluded_from_subbook_mutant(self):
        # Large winning LOW-M_t trades must not satisfy the strict M_t>3%
        # subbook gate -- only market_return_4h>0.03 rows count.
        low_m_wins = tuple(
            _trade(
                "fold-00",
                500.0,
                market_return_4h=0.01,
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        high_m_losses = tuple(
            _trade(
                "fold-00",
                -1.0,
                market_return_4h=0.05,
                exit_ts=100 + i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s4_falsification(
            primary_trades=_uniform_trades(40),
            upward_trades=low_m_wins + high_m_losses,
        )
        assert "s4_high_market_return_e22_not_positive" in result.reasons


class TestPathScenarioMembershipBinding:
    """D3 exploit repro (adversarial verify R1, finding 1): S4 falsification
    must fail-closed reject a path-scenario swap (all "primary" trades
    tagged base13, or the upward book tagged primary_stress17) instead of
    silently computing metrics over the wrong membership."""

    def test_primary_trades_wrong_path_scenario_rejected(self):
        wrong_path_primary = tuple(
            _trade(FOLD_IDS[i % 8], 20.0, exit_ts=i, path_scenario="base13")
            for i in range(40)
        )
        with pytest.raises(H5InputError):
            evaluate_s4_falsification(
                primary_trades=wrong_path_primary, upward_trades=_upward_trades(10)
            )

    def test_upward_trades_wrong_path_scenario_rejected(self):
        wrong_path_upward = tuple(
            _trade(FOLD_IDS[i % 8], 10.0, exit_ts=i, path_scenario="primary_stress17")
            for i in range(10)
        )
        with pytest.raises(H5InputError):
            evaluate_s4_falsification(
                primary_trades=_uniform_trades(40), upward_trades=wrong_path_upward
            )


class TestStrategyAndSelectedOosMembershipBinding:
    def test_s4_evaluator_rejects_pure_s3_book(self):
        def as_s3(trade: MetricTrade) -> MetricTrade:
            return dataclasses.replace(
                trade,
                strategy="S3",
                config_id="S3-00",
                dimension="XRPUSDT",
                gross_notional=None,
                volatility_percentile=50.0,
            )

        with pytest.raises(H5InputError):
            evaluate_s4_falsification(
                primary_trades=tuple(as_s3(t) for t in _uniform_trades(40)),
                upward_trades=tuple(as_s3(t) for t in _upward_trades(10)),
            )

    def test_s4_evaluator_rejects_mixed_selected_configs(self):
        upward = list(_upward_trades(10))
        upward[-1] = dataclasses.replace(upward[-1], config_id="S4-23")
        with pytest.raises(H5InputError):
            evaluate_s4_falsification(
                primary_trades=_uniform_trades(40), upward_trades=tuple(upward)
            )


def _corr_trades(a: int, b: int, c: int, d: int) -> tuple:
    # Balanced two-value coding: X in {+0.02,-0.02}, Y (gross_bps) in
    # {10.0,0.0} (net_bps {5.0,-5.0}). With balanced marginals
    # (a+b==c+d==n/2, a+c==b+d==n/2) Pearson corr reduces exactly to
    # (a+d-b-c)/n, invariant to the actual two values chosen per side.
    trades = []
    ts = 0

    def add(n: int, m_val: float, net_val: float) -> None:
        nonlocal ts
        for _ in range(n):
            trades.append(
                _trade(FOLD_IDS[ts % 8], net_val, market_return_4h=m_val, exit_ts=ts)
            )
            ts += 1

    add(a, 0.02, 5.0)
    add(b, 0.02, -5.0)
    add(c, -0.02, 5.0)
    add(d, -0.02, -5.0)
    return tuple(trades)


class TestCorrelation:
    def test_corr_at_boundary_15pct_passes(self):
        # a=d=23, b=c=17, n=80 -> corr == (23+23-17-17)/80 == 0.15 exactly
        # (modulo float summation noise, well within tolerance).
        trades = _corr_trades(23, 17, 17, 23)
        result = evaluate_s4_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        assert result.correlation == pytest.approx(0.15, abs=1e-9)
        assert "s4_correlation_above_15pct" not in result.reasons

    def test_corr_clearly_above_15pct_fails(self):
        # a=d=24, b=c=16, n=80 -> corr == 16/80 == 0.20
        trades = _corr_trades(24, 16, 16, 24)
        result = evaluate_s4_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        assert result.correlation == pytest.approx(0.20, abs=1e-9)
        assert "s4_correlation_above_15pct" in result.reasons

    def test_corr_undefined_zero_variance_is_incomplete_not_pass(self):
        # Every trade shares the SAME market_return_4h -> zero X-variance.
        trades = tuple(
            _trade(
                FOLD_IDS[i % 8],
                10.0 if i % 2 == 0 else -10.0,
                market_return_4h=0.02,
                exit_ts=i,
            )
            for i in range(40)
        )
        result = evaluate_s4_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        assert result.correlation is None
        assert "s4_correlation_undefined" in result.incomplete_reasons
        assert "s4_correlation_above_15pct" not in result.reasons


class TestPairConcentration:
    def _pair_trades(self, xrp_doge: float, xrp_sol: float, doge_sol: float) -> tuple:
        trades = []
        for i in range(10):
            trades.append(
                _trade(FOLD_IDS[i % 8], xrp_doge, dimension="XRP-DOGE", exit_ts=i)
            )
            trades.append(
                _trade(FOLD_IDS[i % 8], xrp_sol, dimension="XRP-SOL", exit_ts=100 + i)
            )
            trades.append(
                _trade(FOLD_IDS[i % 8], doge_sol, dimension="DOGE-SOL", exit_ts=200 + i)
            )
        return tuple(trades)

    def test_dominant_pair_share_above_70pct_and_others_negative_fails(self):
        result = evaluate_s4_falsification(
            primary_trades=self._pair_trades(90.0, -5.0, -5.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_pair_concentration_above_70pct" in result.reasons

    def test_dominant_pair_share_above_70pct_but_others_pooled_positive_passes(self):
        # exactly-share-dominant alone isn't sufficient -- the OTHER two
        # pooled (combined) must ALSO be <=0 (second predicate).
        result = evaluate_s4_falsification(
            primary_trades=self._pair_trades(90.0, 20.0, -1.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_pair_concentration_above_70pct" not in result.reasons

    def test_two_positive_pairs_below_70pct_share_passes(self):
        result = evaluate_s4_falsification(
            primary_trades=self._pair_trades(40.0, 40.0, -5.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_pair_concentration_above_70pct" not in result.reasons

    def test_missing_pair_evidence_is_incomplete(self):
        trades = []
        for i in range(10):
            trades.append(
                _trade(FOLD_IDS[i % 8], 50.0, dimension="XRP-DOGE", exit_ts=i)
            )
            trades.append(
                _trade(FOLD_IDS[i % 8], -10.0, dimension="XRP-SOL", exit_ts=100 + i)
            )
            # DOGE-SOL has zero trades entirely.
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s4_pair_evidence_missing" in result.incomplete_reasons

    def test_unequal_trade_counts_use_net_sum_share_not_per_pair_mean(self):
        # D12 fix (adversarial verify R1, finding 3): concentration must be
        # the SUM of net_bps per pair, never a per-pair MEAN -- a mean-based
        # implementation is skewed by unequal trade counts. Repro: XRP-DOGE
        # net+100 (1 trade), XRP-SOL net+100 total (100 trades of +1.0
        # each), DOGE-SOL net-200 (1 trade). True sum-based share is
        # 100/(100+100)=0.5 -- must NOT fail. A buggy mean-based
        # implementation computes XRP-DOGE mean=100, XRP-SOL mean=1.0,
        # share=100/101=0.9901 -- incorrectly fails.
        trades = [
            _trade(FOLD_IDS[0], 100.0, dimension="XRP-DOGE", exit_ts=0),
            _trade(FOLD_IDS[1], -200.0, dimension="DOGE-SOL", exit_ts=1),
        ]
        trades.extend(
            _trade(FOLD_IDS[i % 8], 1.0, dimension="XRP-SOL", exit_ts=100 + i)
            for i in range(100)
        )
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert result.pair_concentration == pytest.approx(0.5, abs=1e-9)
        assert "s4_pair_concentration_above_70pct" not in result.reasons

    def test_exact_70pct_net_sum_share_passes(self):
        # dominant XRP-DOGE sum=70 (1 trade), second-positive XRP-SOL
        # sum=30 (1 trade) -> share == 70/100 == 0.70 exactly -> must NOT
        # fail (>0.70 is strict).
        trades = [
            _trade(FOLD_IDS[0], 70.0, dimension="XRP-DOGE", exit_ts=0),
            _trade(FOLD_IDS[1], 30.0, dimension="XRP-SOL", exit_ts=1),
            _trade(FOLD_IDS[2], -10.0, dimension="DOGE-SOL", exit_ts=2),
        ]
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert result.pair_concentration == pytest.approx(0.70, abs=1e-9)
        assert "s4_pair_concentration_above_70pct" not in result.reasons

    def test_just_above_70pct_net_sum_share_with_unequal_counts_fails(self):
        # dominant XRP-DOGE sum=71 (1 trade), second-positive XRP-SOL
        # sum=29 (100 trades of +0.29 each -- unequal count vs dominant),
        # DOGE-SOL sum=-50 (1 trade). Sum-based share = 71/100 = 0.71 >
        # 0.70, and others-pooled (XRP-SOL + DOGE-SOL combined mean) is
        # negative -> gate must fire.
        trades = [
            _trade(FOLD_IDS[0], 71.0, dimension="XRP-DOGE", exit_ts=0),
            _trade(FOLD_IDS[1], -50.0, dimension="DOGE-SOL", exit_ts=1),
        ]
        trades.extend(
            _trade(FOLD_IDS[i % 8], 0.29, dimension="XRP-SOL", exit_ts=100 + i)
            for i in range(100)
        )
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert result.pair_concentration == pytest.approx(0.71, abs=1e-6)
        assert "s4_pair_concentration_above_70pct" in result.reasons


class TestSlowOnlyFailure:
    def _bucket_trades(self, mid_net: float, slow_net: float) -> tuple:
        trades = []
        for i in range(10):
            trades.append(_trade("fold-00", mid_net, holding_minutes=1000.0, exit_ts=i))
        for i in range(10):
            trades.append(
                _trade("fold-00", slow_net, holding_minutes=2000.0, exit_ts=100 + i)
            )
        # Filler winners well outside both buckets so other gates stay quiet.
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 50.0, holding_minutes=60.0, exit_ts=1000 + i)
            )
        return tuple(trades)

    def test_fires_when_mid_bucket_nonpositive_and_slow_bucket_positive(self):
        result = evaluate_s4_falsification(
            primary_trades=self._bucket_trades(mid_net=-5.0, slow_net=5.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_slow_only_failure" in result.reasons

    def test_not_triggered_when_mid_bucket_positive(self):
        result = evaluate_s4_falsification(
            primary_trades=self._bucket_trades(mid_net=5.0, slow_net=5.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_slow_only_failure" not in result.reasons

    def test_not_triggered_when_slow_bucket_nonpositive(self):
        result = evaluate_s4_falsification(
            primary_trades=self._bucket_trades(mid_net=-5.0, slow_net=-1.0),
            upward_trades=_upward_trades(10),
        )
        assert "s4_slow_only_failure" not in result.reasons

    def test_missing_bucket_evidence_is_incomplete(self):
        trades = []
        for i in range(10):
            trades.append(_trade("fold-00", -5.0, holding_minutes=1000.0, exit_ts=i))
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 50.0, holding_minutes=60.0, exit_ts=1000 + i)
            )
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s4_slow_bucket_evidence_missing" in result.incomplete_reasons
        assert "s4_slow_only_failure" not in result.reasons

    def test_exactly_32h_boundary_is_slow_bucket_not_mid_bucket(self):
        # holding_minutes == S4_SLOW_BUCKET_MID_MINUTES (32h) must land in
        # the SLOW ([32h,48h]) bucket, never the MID ([8h,32h)) bucket.
        trades = []
        for i in range(10):
            trades.append(
                _trade(
                    "fold-00",
                    5.0,
                    holding_minutes=S4_SLOW_BUCKET_MID_MINUTES,
                    exit_ts=i,
                )
            )
        for i in range(10):
            trades.append(
                _trade("fold-00", -5.0, holding_minutes=1000.0, exit_ts=100 + i)
            )
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 50.0, holding_minutes=60.0, exit_ts=1000 + i)
            )
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        # mid bucket (1000min, negative) <=0, slow bucket (32h exactly,
        # positive) >0 -> gate must fire.
        assert "s4_slow_only_failure" in result.reasons


class TestAttributionAndExitTaxonomy:
    def test_mean_and_stall_exit_never_classified_as_timeout(self):
        trades = tuple(
            _trade(
                FOLD_IDS[i % 8],
                10.0,
                exit_reason="MEAN_EXIT" if i % 2 == 0 else "STALL_EXIT",
                exit_ts=i,
            )
            for i in range(40)
        )
        result = evaluate_s4_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        by_exit = result.attribution["by_exit_reason"]
        assert by_exit["MEAN_EXIT"]["trades"] == 20
        assert by_exit["STALL_EXIT"]["trades"] == 20
        assert by_exit.get("TIMEOUT", {}).get("trades", 0) == 0
        assert result.pooled_timeout_ratio == 0.0

    def test_attribution_by_pair_and_exit_reason_nonempty(self):
        trades = []
        for i in range(20):
            trades.append(
                _trade(
                    FOLD_IDS[i % 8],
                    10.0,
                    exit_reason="TP",
                    dimension="XRP-DOGE",
                    exit_ts=i,
                )
            )
        for i in range(20):
            trades.append(
                _trade(
                    FOLD_IDS[i % 8],
                    -5.0,
                    exit_reason="SL",
                    dimension="XRP-SOL",
                    exit_ts=100 + i,
                )
            )
        result = evaluate_s4_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert set(result.attribution["by_pair"].keys()) >= {"XRP-DOGE"}
        assert result.attribution["by_pair"]["XRP-DOGE"]["trades"] > 0
        assert result.attribution["by_exit_reason"]["TP"]["trades"] > 0
        assert result.attribution["by_exit_reason"]["SL"]["trades"] > 0


class TestS4PairExecutorState:
    def test_historical_constant_matches_fixed_literal_values(self):
        state = S4_HISTORICAL_PAIR_EXECUTOR_STATE
        assert state.volatility_percentile is None
        assert state.volatility_percentile_provenance == "not_defined_for_s4"
        assert state.pair_executor_state == "not_evaluated"
        assert state.order_count is None
        assert state.residual_count is None
        assert state.pair_exec_fail_count is None
        assert state.readiness == "historical_screen_only"
        assert state.demo_eligible is False

    def test_volatility_percentile_zero_mutant_rejected(self):
        with pytest.raises(H5InputError):
            S4PairExecutorState(
                volatility_percentile=0.0,
                volatility_percentile_provenance="not_defined_for_s4",
                pair_executor_state="not_evaluated",
                order_count=None,
                residual_count=None,
                pair_exec_fail_count=None,
                readiness="historical_screen_only",
                demo_eligible=False,
            )

    def test_numeric_pair_exec_count_mutant_rejected(self):
        with pytest.raises(H5InputError):
            S4PairExecutorState(
                volatility_percentile=None,
                volatility_percentile_provenance="not_defined_for_s4",
                pair_executor_state="not_evaluated",
                order_count=0,
                residual_count=None,
                pair_exec_fail_count=None,
                readiness="historical_screen_only",
                demo_eligible=False,
            )

    def test_demo_eligible_true_mutant_rejected(self):
        with pytest.raises(H5InputError):
            S4PairExecutorState(
                volatility_percentile=None,
                volatility_percentile_provenance="not_defined_for_s4",
                pair_executor_state="not_evaluated",
                order_count=None,
                residual_count=None,
                pair_exec_fail_count=None,
                readiness="historical_screen_only",
                demo_eligible=True,
            )


class TestDirectVerdict:
    def test_incomplete_takes_priority_over_hard_gate_fail(self):
        assert (
            compute_direct_verdict(incomplete_reasons=("x",), hard_gate_reasons=("y",))
            == "incomplete"
        )

    def test_hard_gate_reason_without_incomplete_is_fail(self):
        assert (
            compute_direct_verdict(incomplete_reasons=(), hard_gate_reasons=("y",))
            == "historical_fail"
        )

    def test_no_reasons_is_pass(self):
        assert (
            compute_direct_verdict(incomplete_reasons=(), hard_gate_reasons=())
            == "historical_pass"
        )


_S3_RANK = StrategyRankMetrics(
    min_fold_e17=5.0, pooled_e17=10.0, monthly_concentration=0.3, timeout_ratio=0.1
)
_S4_RANK = StrategyRankMetrics(
    min_fold_e17=6.0, pooled_e17=8.0, monthly_concentration=0.4, timeout_ratio=0.2
)
_S4_SUPERIOR_RANK = StrategyRankMetrics(
    min_fold_e17=6.0,
    pooled_e17=16.0,
    monthly_concentration=0.4,
    timeout_ratio=0.12,
)


class TestCampaignDecision:
    def test_incomplete_first_s3_incomplete(self):
        result = compute_campaign_decision(
            s3_direct_verdict="incomplete", s4_direct_verdict="historical_pass"
        )
        assert result.campaign_decision == "incomplete"
        assert result.campaign_historical_verdict == "incomplete"
        assert result.historical_preferred is None

    def test_incomplete_first_s4_incomplete(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_pass", s4_direct_verdict="incomplete"
        )
        assert result.campaign_decision == "incomplete"
        assert result.campaign_historical_verdict == "incomplete"
        assert result.historical_preferred is None

    def test_both_fail(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_fail", s4_direct_verdict="historical_fail"
        )
        assert result.campaign_decision == "both_fail"
        assert result.demo_candidate is None
        assert result.campaign_historical_verdict == "historical_fail"
        assert result.historical_preferred is None

    def test_s3_only(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_pass", s4_direct_verdict="historical_fail"
        )
        assert result.campaign_decision == "s3_only"
        assert result.demo_candidate == "S3"
        assert result.campaign_historical_verdict == "historical_pass"
        assert result.historical_preferred == "S3"

    def test_s4_only_no_demo(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_fail", s4_direct_verdict="historical_pass"
        )
        assert result.campaign_decision == "s4_only_no_demo"
        assert result.demo_candidate is None
        assert result.campaign_historical_verdict == "historical_pass"
        assert result.historical_preferred == "S4"

    def test_both_pass_s3_demo_candidate(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_pass",
            s4_direct_verdict="historical_pass",
            s3_rank_metrics=_S3_RANK,
            s4_rank_metrics=_S4_RANK,
        )
        assert result.campaign_decision == "both_pass_s3_demo_candidate"
        assert result.demo_candidate == "S3"
        assert result.campaign_historical_verdict == "historical_pass"
        # rank_both_pass(_S3_RANK, _S4_RANK): S4's higher min_fold_e17 (6>5)
        # decides first -> "S4" is the historically-preferred strategy, even
        # though S3 remains the (operationally forced) demo candidate.
        assert result.historical_preferred == "S4"

    def test_both_pass_requires_rank_metrics(self):
        with pytest.raises(H5InputError):
            compute_campaign_decision(
                s3_direct_verdict="historical_pass", s4_direct_verdict="historical_pass"
            )

    def test_campaign_decision_never_overwrites_direct_verdicts(self):
        result = compute_campaign_decision(
            s3_direct_verdict="historical_fail", s4_direct_verdict="historical_pass"
        )
        assert result.s3_direct_verdict == "historical_fail"
        assert result.s4_direct_verdict == "historical_pass"

    def test_unknown_s3_verdict_rejected(self):
        # D13 fix (adversarial verify R1, finding 4): compute_campaign_decision
        # must reject a non-closed-enum verdict string rather than silently
        # falling through to a default branch.
        with pytest.raises(H5InputError):
            compute_campaign_decision(
                s3_direct_verdict="bogus", s4_direct_verdict="historical_pass"
            )

    def test_unknown_s4_verdict_rejected(self):
        with pytest.raises(H5InputError):
            compute_campaign_decision(
                s3_direct_verdict="historical_pass", s4_direct_verdict="bogus"
            )


class TestObservableS4Superiority:
    def test_true_when_all_three_criteria_met(self):
        assert (
            s4_shows_observable_superiority(
                pooled_e17_s3=10.0,
                pooled_e17_s4=16.0,
                min_fold_e17_s3=2.0,
                min_fold_e17_s4=3.0,
                pooled_timeout_s3=0.10,
                pooled_timeout_s4=0.12,
            )
            is True
        )

    def test_false_when_pooled_e17_gap_insufficient(self):
        assert (
            s4_shows_observable_superiority(
                pooled_e17_s3=10.0,
                pooled_e17_s4=14.0,
                min_fold_e17_s3=2.0,
                min_fold_e17_s4=3.0,
                pooled_timeout_s3=0.10,
                pooled_timeout_s4=0.12,
            )
            is False
        )

    def test_false_when_min_fold_e17_worse(self):
        assert (
            s4_shows_observable_superiority(
                pooled_e17_s3=10.0,
                pooled_e17_s4=16.0,
                min_fold_e17_s3=2.0,
                min_fold_e17_s4=1.0,
                pooled_timeout_s3=0.10,
                pooled_timeout_s4=0.12,
            )
            is False
        )

    def test_false_when_timeout_gap_too_wide(self):
        assert (
            s4_shows_observable_superiority(
                pooled_e17_s3=10.0,
                pooled_e17_s4=16.0,
                min_fold_e17_s3=2.0,
                min_fold_e17_s4=3.0,
                pooled_timeout_s3=0.10,
                pooled_timeout_s4=0.20,
            )
            is False
        )

    def test_superiority_is_report_only_never_changes_campaign_decision(self):
        result_true = compute_campaign_decision(
            s3_direct_verdict="historical_pass",
            s4_direct_verdict="historical_pass",
            s4_observable_superiority=True,
            s3_rank_metrics=_S3_RANK,
            s4_rank_metrics=_S4_SUPERIOR_RANK,
        )
        result_false = compute_campaign_decision(
            s3_direct_verdict="historical_pass",
            s4_direct_verdict="historical_pass",
            s4_observable_superiority=False,
            s3_rank_metrics=_S3_RANK,
            s4_rank_metrics=_S4_RANK,
        )
        assert (
            result_true.campaign_decision
            == result_false.campaign_decision
            == "both_pass_s3_demo_candidate"
        )
        assert result_true.demo_candidate == result_false.demo_candidate == "S3"
        assert result_true.s4_observable_superiority is True
        assert result_false.s4_observable_superiority is False

    def test_supplied_superiority_must_match_rank_recomputation(self):
        with pytest.raises(
            H5InputError,
            match="campaign_s4_observable_superiority_forged_or_stale",
        ):
            compute_campaign_decision(
                s3_direct_verdict="historical_pass",
                s4_direct_verdict="historical_pass",
                s4_observable_superiority=True,
                s3_rank_metrics=_S3_RANK,
                s4_rank_metrics=_S4_RANK,
            )


class TestRankBothPass:
    def test_prefers_higher_min_fold_e17(self):
        s3 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.1,
        )
        s4 = StrategyRankMetrics(
            min_fold_e17=6.0,
            pooled_e17=8.0,
            monthly_concentration=0.4,
            timeout_ratio=0.2,
        )
        assert rank_both_pass(s3_metrics=s3, s4_metrics=s4) == "S4"

    def test_falls_back_to_pooled_e17_when_min_fold_tied(self):
        s3 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.1,
        )
        s4 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=12.0,
            monthly_concentration=0.5,
            timeout_ratio=0.3,
        )
        assert rank_both_pass(s3_metrics=s3, s4_metrics=s4) == "S4"

    def test_falls_back_to_monthly_concentration_lower_is_better(self):
        s3 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.5,
            timeout_ratio=0.1,
        )
        s4 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.3,
        )
        assert rank_both_pass(s3_metrics=s3, s4_metrics=s4) == "S4"

    def test_falls_back_to_timeout_lower_is_better(self):
        s3 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.2,
        )
        s4 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.1,
        )
        assert rank_both_pass(s3_metrics=s3, s4_metrics=s4) == "S4"

    def test_operational_complexity_tiebreak_favors_s3_when_all_tied(self):
        s3 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.1,
        )
        s4 = StrategyRankMetrics(
            min_fold_e17=5.0,
            pooled_e17=10.0,
            monthly_concentration=0.3,
            timeout_ratio=0.1,
        )
        assert rank_both_pass(s3_metrics=s3, s4_metrics=s4) == "S3"
