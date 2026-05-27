"""ROB-339 — hypothesis evaluation machinery + the five family functions.

The machinery is the auditable core: a chronological in-sample/OOS split, a
fee-adjusted in-sample expectancy as the decision metric, and an OOS-tail
confirmation read. Families are thin condition masks over features.
"""

from __future__ import annotations

import pandas as pd
from discovery.features import add_features
from discovery.hypotheses import evaluate_hypothesis, run_all_hypotheses
from discovery.screen import HypothesisSummary


def _featured_for_machinery() -> pd.DataFrame:
    # 10 bars; fwd_ret_3m hand-set so signal-row means are exact.
    df = pd.DataFrame({"fwd_ret_3m": [90.0, 0, 110.0, 0, 0, 100.0, 0, 0, 40.0, 0]})
    return df


def test_evaluate_hypothesis_split_and_fee_adjust() -> None:
    bars = _featured_for_machinery()
    mask = pd.Series([True, False, True, False, False, True, False, False, True, False])
    s = evaluate_hypothesis(
        bars,
        mask,
        name="m",
        conditions="cond",
        fwd_col="fwd_ret_3m",
        direction="long",
        fee_budget_bps=8.0,
        oos_frac=0.25,
    )
    # split at int(10*0.75)=7 -> in-sample signals at rows 0,2,5 ; OOS signal at row 8
    assert s.sample_count == 3
    assert round(s.gross_expectancy_bps, 4) == 100.0  # mean(90,110,100)
    assert round(s.fee_adjusted_bps, 4) == 92.0  # 100 - 8
    assert round(s.oos_fee_adjusted_bps, 4) == 32.0  # 40 - 8


def test_direction_short_flips_outcome_sign() -> None:
    bars = pd.DataFrame({"fwd_ret_3m": [-100.0, -100.0, -100.0, 0, 0, 0, 0, 0]})
    mask = pd.Series([True, True, True, False, False, False, False, False])
    s = evaluate_hypothesis(
        bars,
        mask,
        name="r",
        conditions="c",
        fwd_col="fwd_ret_3m",
        direction="short",
        fee_budget_bps=8.0,
        oos_frac=0.25,
    )
    assert round(s.gross_expectancy_bps, 4) == 100.0  # short profits from -100 moves


def test_run_all_hypotheses_returns_five_named_summaries() -> None:
    dt = pd.date_range("2026-03-02 13:00", periods=400, freq="1min", tz="UTC")
    # gentle uptrend + noise so signals actually fire
    close = pd.Series(range(400), dtype=float) * 0.01 + 100.0
    bars = pd.DataFrame(
        {
            "dt": dt,
            "open": close,
            "high": close + 0.05,
            "low": close - 0.05,
            "close": close,
            "volume": 10.0,
        }
    )
    featured = add_features(bars)
    summaries = run_all_hypotheses(featured, fee_budget_bps=8.0)
    assert all(isinstance(s, HypothesisSummary) for s in summaries)
    names = {s.name for s in summaries}
    assert names == {
        "momentum_continuation",
        "sweep_reversal",
        "vol_regime_filter",
        "time_of_day_filter",
        "maker_viability",
    }
    maker = next(s for s in summaries if s.name == "maker_viability")
    assert maker.missed_fill_ratio is not None
