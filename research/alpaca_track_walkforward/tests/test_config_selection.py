"""ROB-1062 H4 (AC7-AC10) — TRAIN-only config selection rule."""

from __future__ import annotations

import blind_counts as bc
import config_selection as cs
import pytest


def _metric(config_id, *, closed=40, median_e120=50.0, turnover=0.2, cost_pct=3.0):
    return cs.ConfigTrainMetrics(
        config_id=config_id,
        closed_trades_count=closed,
        median_trade_e120_bp=median_e120,
        turnover_p=turnover,
        annualized_stress_cost_pct=cost_pct,
    )


def test_select_config_rejects_anything_other_than_train_literal():
    with pytest.raises(cs.OOSDataReachedSelectionError, match="OOS"):
        cs.select_config([], data_window="OOS", stress_cost_cap_pct=6.0)
    with pytest.raises(cs.OOSDataReachedSelectionError):
        cs.select_config(
            [], data_window="train", stress_cost_cap_pct=6.0
        )  # case-sensitive


def test_picks_max_median_e120_among_passing_configs():
    metrics = [
        _metric("AP-A1-00", median_e120=40.0),
        _metric("AP-A1-01", median_e120=90.0),
        _metric("AP-A1-02", median_e120=10.0),
    ]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "SELECTED"
    assert result.selected_config_id == "AP-A1-01"


def test_tie_break_1_lower_turnover_wins():
    metrics = [
        _metric("AP-A1-00", median_e120=50.0, turnover=0.25),
        _metric("AP-A1-01", median_e120=50.0, turnover=0.10),
    ]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.selected_config_id == "AP-A1-01"


def test_tie_break_2_canonical_config_id_ascending():
    metrics = [
        _metric("AP-A1-05", median_e120=50.0, turnover=0.10),
        _metric("AP-A1-02", median_e120=50.0, turnover=0.10),
    ]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.selected_config_id == "AP-A1-02"


def test_below_min_closed_trades_excluded():
    metrics = [_metric("AP-A1-00", closed=29, median_e120=999.0)]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "NO_SELECTED_CONFIG"
    assert result.selected_config_id is None


def test_exactly_30_closed_trades_is_the_inclusive_boundary():
    metrics = [_metric("AP-A1-00", closed=30, median_e120=1.0)]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "SELECTED"


def test_non_positive_median_e120_excluded():
    metrics = [
        _metric("AP-A1-00", median_e120=0.0),
        _metric("AP-A1-01", median_e120=-5.0),
    ]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "NO_SELECTED_CONFIG"


def test_cost_cap_exceeded_excluded_pnl_blind_even_with_great_median_e120():
    metrics = [_metric("AP-A1-00", median_e120=1000.0, cost_pct=6.01)]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "NO_SELECTED_CONFIG"


def test_cost_gate_is_compared_before_pnl_and_stops_pnl_access_on_cost_failure():
    events: list[str] = []

    class CostSpy(float):
        def __le__(self, other):
            events.append("COST<=")
            return False

    class PnLSpy(float):
        def __gt__(self, other):
            events.append("PNL>")
            return True

    metrics = [
        _metric(
            "AP-A1-00",
            median_e120=PnLSpy(1000.0),
            cost_pct=CostSpy(99.0),
        )
    ]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "NO_SELECTED_CONFIG"
    assert events == ["COST<="]


def test_ap_a2_turnover_band_is_pnl_blind_and_rejects_90pct_before_e120():
    events: list[str] = []

    class PnLSpy(float):
        def __gt__(self, other):
            events.append("PNL>")
            return True

    result = cs.select_config(
        [
            _metric(
                "AP-A2-00",
                closed=30,
                median_e120=PnLSpy(1.0),
                turnover=0.90,
                cost_pct=1.0,
            )
        ],
        data_window="TRAIN",
        stress_cost_cap_pct=18.0,
        turnover_band=(0.2083, 0.2885),
    )
    assert result.status == "NO_SELECTED_CONFIG"
    assert events == []


def test_ap_a2_selection_cannot_omit_the_sealed_turnover_band():
    with pytest.raises(ValueError, match="requires the sealed turnover band"):
        cs.select_config(
            [_metric("AP-A2-00", turnover=0.25)],
            data_window="TRAIN",
            stress_cost_cap_pct=18.0,
        )


@pytest.mark.parametrize("turnover", [0.2083, 0.2885])
def test_ap_a2_turnover_band_boundaries_are_inclusive(turnover):
    result = cs.select_config(
        [_metric("AP-A2-00", closed=30, median_e120=1.0, turnover=turnover)],
        data_window="TRAIN",
        stress_cost_cap_pct=18.0,
        turnover_band=(0.2083, 0.2885),
    )
    assert result.selected_config_id == "AP-A2-00"


def test_full_nav_per_trade_36pct_misread_is_rejected_by_unchanged_caps():
    full_nav_drag = bc.annualized_stress_cost_pct(
        entry_filled_notionals=(2000.0,) * 30,
        window_days=365,
        nav_usd=2000.0,
        cost_bp=120.0,
    )
    assert full_nav_drag == pytest.approx(36.0)
    result = cs.select_config(
        [_metric("AP-A1-00", closed=30, median_e120=999.0, cost_pct=full_nav_drag)],
        data_window="TRAIN",
        stress_cost_cap_pct=6.0,
    )
    assert result.status == "NO_SELECTED_CONFIG"


def test_no_config_passes_yields_no_selected_config_never_a_fallback():
    metrics = [_metric("AP-A1-00", closed=0, median_e120=None)]
    result = cs.select_config(metrics, data_window="TRAIN", stress_cost_cap_pct=6.0)
    assert result.status == "NO_SELECTED_CONFIG"
    assert result.selected_config_id is None


def test_zero_closed_trades_metric_construction_requires_none_median():
    with pytest.raises(ValueError, match="zero closed trades"):
        cs.ConfigTrainMetrics(
            config_id="x",
            closed_trades_count=0,
            median_trade_e120_bp=1.0,
            turnover_p=0.1,
            annualized_stress_cost_pct=1.0,
        )


def test_nonzero_closed_trades_metric_construction_requires_a_median():
    with pytest.raises(ValueError, match="must carry a median E120"):
        cs.ConfigTrainMetrics(
            config_id="x",
            closed_trades_count=5,
            median_trade_e120_bp=None,
            turnover_p=0.1,
            annualized_stress_cost_pct=1.0,
        )


def test_selection_result_construction_invariants():
    with pytest.raises(ValueError, match="must carry a selected_config_id"):
        cs.ConfigSelectionResult(status="SELECTED", selected_config_id=None)
    with pytest.raises(ValueError, match="must not carry"):
        cs.ConfigSelectionResult(status="NO_SELECTED_CONFIG", selected_config_id="x")
