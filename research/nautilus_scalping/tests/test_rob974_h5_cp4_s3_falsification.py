"""ROB-983 (H5, CP4) -- S3 falsification gates and attribution.

Pooled/per-fold timeout ceilings, all-long-entry bullish-subbook upward E22
strict positivity, first-4h SL-dependence (undefined denominator is
incomplete, never a convenient zero/pass), symbol dependence (both the
"exactly one positive" AND "other two pooled <=0" predicates required), and
exit-reason/symbol/direction attribution (THESIS_EXIT is never TIMEOUT).
"""

from __future__ import annotations

import dataclasses

import pytest
from rob974_h5_contracts import FOLD_IDS, H5InputError, MetricTrade
from rob974_h5_s3 import (
    S3_FIRST_4H_MINUTES,
    S3_POOLED_TIMEOUT_MAX,
    evaluate_s3_falsification,
)


def _trade(
    fold_id: str,
    net_bps: float,
    *,
    exit_reason: str = "TP",
    holding_minutes: float = 10.0,
    dimension: str = "XRPUSDT",
    direction: str = "long",
    exit_ts: int = 0,
    path_scenario: str = "primary_stress17",
) -> MetricTrade:
    return MetricTrade(
        strategy="S3",
        config_id="S3-00",
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
        gross_notional=None,
        market_return_4h=0.01,
        volatility_percentile=50.0,
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

    def test_exact_15pct_passes(self):
        result = evaluate_s3_falsification(
            primary_trades=self._trades_with_timeout_ratio(S3_POOLED_TIMEOUT_MAX),
            upward_trades=_upward_trades(10),
        )
        assert "s3_pooled_timeout_above_15pct" not in result.reasons

    def test_above_15pct_fails(self):
        result = evaluate_s3_falsification(
            primary_trades=self._trades_with_timeout_ratio(0.151),
            upward_trades=_upward_trades(10),
        )
        assert "s3_pooled_timeout_above_15pct" in result.reasons


class TestFoldTimeoutBoundary:
    def test_one_fold_above_25pct_fails_even_if_pooled_ok(self):
        trades = []
        # fold-00: 10 trades, 3 timeouts (30% > 25%)
        for i in range(10):
            reason = "TIMEOUT" if i < 3 else "TP"
            trades.append(_trade("fold-00", 10.0, exit_reason=reason, exit_ts=i))
        # remaining folds: all TP, plenty of trades, keeps pooled ratio low
        for fold_id in FOLD_IDS[1:]:
            for i in range(50):
                trades.append(_trade(fold_id, 10.0, exit_reason="TP", exit_ts=i))
        result = evaluate_s3_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s3_fold_timeout_above_25pct" in result.reasons
        assert "s3_pooled_timeout_above_15pct" not in result.reasons

    def test_exact_25pct_in_one_fold_passes_that_fold(self):
        trades = []
        for i in range(20):
            reason = "TIMEOUT" if i < 5 else "TP"  # 5/20 = 25%
            trades.append(_trade("fold-00", 10.0, exit_reason=reason, exit_ts=i))
        for fold_id in FOLD_IDS[1:]:
            for i in range(20):
                trades.append(_trade(fold_id, 10.0, exit_reason="TP", exit_ts=i))
        result = evaluate_s3_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s3_fold_timeout_above_25pct" not in result.reasons


class TestBullishLongE22:
    def test_positive_bullish_subbook_passes(self):
        longs = tuple(
            _trade(
                "fold-00",
                5.0,
                direction="long",
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s3_falsification(
            primary_trades=_uniform_trades(40), upward_trades=longs
        )
        assert "s3_bullish_long_e22_not_positive" not in result.reasons

    def test_zero_bullish_subbook_fails(self):
        longs = tuple(
            _trade(
                "fold-00",
                0.0,
                direction="long",
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s3_falsification(
            primary_trades=_uniform_trades(40), upward_trades=longs
        )
        assert "s3_bullish_long_e22_not_positive" in result.reasons

    def test_shorts_are_excluded_from_bullish_subbook_mutant(self):
        # A large positive SHORT subbook must not be allowed to satisfy the
        # bullish-LONG gate -- only direction=="long" rows count.
        shorts = tuple(
            _trade(
                "fold-00",
                500.0,
                direction="short",
                exit_ts=i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        longs_losing = tuple(
            _trade(
                "fold-00",
                -1.0,
                direction="long",
                exit_ts=100 + i,
                path_scenario="upward_stress22",
            )
            for i in range(5)
        )
        result = evaluate_s3_falsification(
            primary_trades=_uniform_trades(40),
            upward_trades=shorts + longs_losing,
        )
        assert "s3_bullish_long_e22_not_positive" in result.reasons


class TestPathScenarioMembershipBinding:
    """D3 exploit repro (adversarial verify R1, finding 1): common gates and
    S3/S4 falsification must fail-closed reject a path-scenario swap (all
    "primary" trades tagged base13, or the upward book tagged
    primary_stress17) instead of silently computing metrics over the wrong
    membership."""

    def test_primary_trades_wrong_path_scenario_rejected(self):
        wrong_path_primary = tuple(
            _trade(FOLD_IDS[i % 8], 20.0, exit_ts=i, path_scenario="base13")
            for i in range(40)
        )
        with pytest.raises(H5InputError):
            evaluate_s3_falsification(
                primary_trades=wrong_path_primary, upward_trades=_upward_trades(10)
            )

    def test_upward_trades_wrong_path_scenario_rejected(self):
        wrong_path_upward = tuple(
            _trade(FOLD_IDS[i % 8], 10.0, exit_ts=i, path_scenario="primary_stress17")
            for i in range(10)
        )
        with pytest.raises(H5InputError):
            evaluate_s3_falsification(
                primary_trades=_uniform_trades(40), upward_trades=wrong_path_upward
            )


class TestStrategyAndSelectedOosMembershipBinding:
    def test_s3_evaluator_rejects_pure_s4_book(self):
        def as_s4(trade: MetricTrade) -> MetricTrade:
            return dataclasses.replace(
                trade,
                strategy="S4",
                config_id="S4-00",
                dimension="XRP-DOGE",
                gross_notional=100.0,
                volatility_percentile=None,
            )

        with pytest.raises(H5InputError):
            evaluate_s3_falsification(
                primary_trades=tuple(as_s4(t) for t in _uniform_trades(40)),
                upward_trades=tuple(as_s4(t) for t in _upward_trades(10)),
            )

    def test_s3_evaluator_rejects_mixed_selected_configs(self):
        primary = list(_uniform_trades(40))
        primary[-1] = dataclasses.replace(primary[-1], config_id="S3-23")
        with pytest.raises(H5InputError):
            evaluate_s3_falsification(
                primary_trades=tuple(primary), upward_trades=_upward_trades(10)
            )


class TestFirst4hSlDependence:
    def _trades_with_dependence(self, ratio: float) -> tuple:
        # denominator: all losses total |net| = 1000 (10 losing trades of -100)
        # numerator target: ratio * 1000, split evenly among first-4h SL losses
        total_loss = 1000.0
        n_loss_trades = 10
        per_loss = total_loss / n_loss_trades
        target_first4h = ratio * total_loss
        n_first4h = round(target_first4h / per_loss)
        trades = []
        for i in range(n_loss_trades):
            if i < n_first4h:
                trades.append(
                    _trade(
                        "fold-00",
                        -per_loss,
                        exit_reason="SL",
                        holding_minutes=S3_FIRST_4H_MINUTES,
                        exit_ts=i,
                    )
                )
            else:
                trades.append(
                    _trade(
                        "fold-00",
                        -per_loss,
                        exit_reason="SL",
                        holding_minutes=500.0,
                        exit_ts=i,
                    )
                )
        # plenty of winners so other gates don't interfere
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 20.0, exit_reason="TP", exit_ts=1000 + i)
            )
        return tuple(trades)

    def test_exact_50pct_passes(self):
        result = evaluate_s3_falsification(
            primary_trades=self._trades_with_dependence(0.5),
            upward_trades=_upward_trades(10),
        )
        assert "s3_first_4h_sl_dependence_above_50pct" not in result.reasons

    def test_above_50pct_fails(self):
        result = evaluate_s3_falsification(
            primary_trades=self._trades_with_dependence(0.6),
            upward_trades=_upward_trades(10),
        )
        assert "s3_first_4h_sl_dependence_above_50pct" in result.reasons

    def test_exactly_240_minutes_is_included_not_excluded(self):
        # A trade at EXACTLY holding_minutes=240 with exit=SL must count in
        # the numerator (the "<=240" boundary, not "<240").
        trades = [
            _trade(
                "fold-00", -10.0, exit_reason="SL", holding_minutes=240.0, exit_ts=0
            ),
        ]
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 20.0, exit_reason="TP", exit_ts=100 + i)
            )
        result = evaluate_s3_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        # numerator == denominator == 10 -> ratio 1.0 -> must FAIL (not
        # silently excluded to 0/undefined).
        assert "s3_first_4h_sl_dependence_above_50pct" in result.reasons

    def test_zero_losses_denominator_is_incomplete_not_pass(self):
        trades = _uniform_trades(40, exit_reason="TP", net_bps=10.0)
        result = evaluate_s3_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        assert result.first_4h_sl_dependence is None
        assert "s3_first_4h_sl_denominator_undefined" in result.incomplete_reasons
        assert "s3_first_4h_sl_dependence_above_50pct" not in result.reasons

    def test_signed_loss_denominator_mutant_would_change_ratio(self):
        # Sanity: denominator must be built from ABSOLUTE loss magnitude,
        # never signed net_bps (which would let large losses cancel).
        trades = [
            _trade(
                "fold-00", -800.0, exit_reason="SL", holding_minutes=100.0, exit_ts=0
            ),
            _trade(
                "fold-00", -200.0, exit_reason="SL", holding_minutes=500.0, exit_ts=1
            ),
        ]
        for i in range(50):
            trades.append(
                _trade(FOLD_IDS[i % 8], 20.0, exit_reason="TP", exit_ts=100 + i)
            )
        result = evaluate_s3_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        # true ratio: 800 / (800+200) = 0.8 > 0.5 -> fails
        assert result.first_4h_sl_dependence == pytest.approx(0.8, abs=1e-9)
        assert "s3_first_4h_sl_dependence_above_50pct" in result.reasons


class TestSymbolDependence:
    def _symbol_trades(self, xrp: float, doge: float, sol: float) -> tuple:
        trades = []
        for i in range(10):
            trades.append(_trade(FOLD_IDS[i % 8], xrp, dimension="XRPUSDT", exit_ts=i))
            trades.append(
                _trade(FOLD_IDS[i % 8], doge, dimension="DOGEUSDT", exit_ts=100 + i)
            )
            trades.append(
                _trade(FOLD_IDS[i % 8], sol, dimension="SOLUSDT", exit_ts=200 + i)
            )
        return tuple(trades)

    def test_one_positive_others_pooled_negative_fails(self):
        result = evaluate_s3_falsification(
            primary_trades=self._symbol_trades(50.0, -10.0, -10.0),
            upward_trades=_upward_trades(10),
        )
        assert "s3_symbol_dependence" in result.reasons

    def test_one_positive_but_other_two_pooled_positive_passes(self):
        # exactly one symbol positive alone isn't sufficient -- the OTHER
        # two pooled must ALSO be <=0 (second predicate).
        result = evaluate_s3_falsification(
            primary_trades=self._symbol_trades(50.0, 5.0, -1.0),
            upward_trades=_upward_trades(10),
        )
        assert "s3_symbol_dependence" not in result.reasons

    def test_two_positive_symbols_never_fails_this_gate(self):
        result = evaluate_s3_falsification(
            primary_trades=self._symbol_trades(50.0, 50.0, -10.0),
            upward_trades=_upward_trades(10),
        )
        assert "s3_symbol_dependence" not in result.reasons

    def test_missing_symbol_trades_is_incomplete(self):
        trades = []
        for i in range(10):
            trades.append(_trade(FOLD_IDS[i % 8], 50.0, dimension="XRPUSDT", exit_ts=i))
            trades.append(
                _trade(FOLD_IDS[i % 8], -10.0, dimension="DOGEUSDT", exit_ts=100 + i)
            )
            # SOLUSDT has zero trades entirely
        result = evaluate_s3_falsification(
            primary_trades=tuple(trades), upward_trades=_upward_trades(10)
        )
        assert "s3_symbol_evidence_missing" in result.incomplete_reasons


class TestAttributionAndExitTaxonomy:
    def test_thesis_exit_is_never_classified_as_timeout(self):
        trades = _uniform_trades(40, exit_reason="THESIS_EXIT", net_bps=10.0)
        result = evaluate_s3_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        by_exit = result.attribution["by_exit_reason"]
        assert "THESIS_EXIT" in by_exit
        assert by_exit["THESIS_EXIT"]["trades"] == 40
        # THESIS_EXIT trades never leak into the TIMEOUT bucket.
        assert by_exit.get("TIMEOUT", {}).get("trades", 0) == 0
        assert result.pooled_timeout_ratio == 0.0

    def test_attribution_by_symbol_and_exit_reason_nonempty(self):
        trades = self._mixed_trades()
        result = evaluate_s3_falsification(
            primary_trades=trades, upward_trades=_upward_trades(10)
        )
        assert set(result.attribution["by_symbol"].keys()) >= {"XRPUSDT"}
        assert result.attribution["by_symbol"]["XRPUSDT"]["trades"] > 0
        assert result.attribution["by_exit_reason"]["TP"]["trades"] > 0
        assert result.attribution["by_exit_reason"]["SL"]["trades"] > 0

    @staticmethod
    def _mixed_trades():
        trades = []
        for i in range(20):
            trades.append(
                _trade(
                    FOLD_IDS[i % 8],
                    10.0,
                    exit_reason="TP",
                    dimension="XRPUSDT",
                    exit_ts=i,
                )
            )
        for i in range(20):
            trades.append(
                _trade(
                    FOLD_IDS[i % 8],
                    -5.0,
                    exit_reason="SL",
                    dimension="DOGEUSDT",
                    exit_ts=100 + i,
                )
            )
        return tuple(trades)
