from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.selection_sweep import data, spec, sweep
from research.selection_sweep.panel import (
    build_panel,
    eligibility,
    features,
    outcomes,
)

DAYS = pd.bdate_range("2020-01-01", periods=430)


def _frame(prices: np.ndarray, *, volume: float = 1000.0) -> pd.DataFrame:
    close = pd.Series(prices, index=DAYS[: len(prices)])
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]) * 1.001,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": volume,
            "traded_value": close * volume,
        }
    )


def _world(n_names: int = 40, seed: int = 0, drift: float = 0.003):
    rng = np.random.default_rng(seed)
    mu = np.linspace(-drift, drift, n_names)
    frames = {}
    for i in range(n_names):
        steps = rng.normal(mu[i], 0.01, len(DAYS))
        frames[f"S{i:02d}"] = _frame(100 * np.exp(np.cumsum(steps)))
    bench = _frame(100 * np.exp(np.cumsum(rng.normal(0, 0.005, len(DAYS)))))
    return frames, bench


@pytest.fixture(scope="module")
def world():
    frames, bench = _world()
    panel = build_panel("us", frames, bench, start="2020-01-01", end="2021-08-31")
    window = (str(DAYS[300].date()), str(DAYS[-1].date()))
    return panel, window


def test_grid_is_the_registered_size():
    cells = spec.grid()
    assert len(cells) == len({c.key for c in cells}) == 2 * 7 * 5 * 2 == 140
    assert spec.CARRY_CAP * 1 <= len(spec.SEEDS) and len(spec.SEEDS) == 200
    assert spec.EXPLORATION[1] < spec.VALIDATION[0] < spec.VALIDATION[1]
    # the gap between windows covers the longest horizon
    gap = len(pd.bdate_range(spec.EXPLORATION[1], spec.VALIDATION[0])) - 1
    assert gap >= max(spec.HORIZONS)


def test_outcome_is_open_to_open_with_h_session_hold(world):
    panel, _ = world
    ret, bench, exit_idx = outcomes(panel, 3)
    i = 100
    sym = 5
    expect = panel.open.iloc[i + 4, sym] / panel.open.iloc[i + 1, sym] - 1
    assert ret[i, sym] == pytest.approx(expect)
    assert bench[i] == pytest.approx(
        panel.bench_open.iloc[i + 4] / panel.bench_open.iloc[i + 1] - 1
    )
    assert exit_idx[i] == i + 4
    # the last decisions have no complete hold
    assert np.isnan(ret[-3:]).all() and (exit_idx[-3:] == -1).all()


def test_terminal_exit_uses_last_close_and_gap_stays_unknown():
    frames, bench = _world(n_names=3)
    frames["S00"] = frames["S00"].iloc[:200]  # series ends early -> terminated
    gap = frames["S01"].copy()
    gap.loc[DAYS[150], ["open", "close"]] = np.nan  # a hole inside a live series
    frames["S01"] = gap
    panel = build_panel("us", frames, bench, start="2020-01-01", end="2021-08-31")
    assert panel.terminated.tolist() == [True, False, False]
    ret, _, _ = outcomes(panel, 5)
    s00 = panel.symbols.index("S00")
    # decision 197: entry 198 exists, exit 203 is past the end -> last close (idx 199)
    expect = panel.close.iloc[199, s00] / panel.open.iloc[198, s00] - 1
    assert ret[197, s00] == pytest.approx(expect)
    # entry already past the last bar: unknown, not filled
    assert np.isnan(ret[199, s00])
    s01 = panel.symbols.index("S01")
    # any hold that touches the hole at session 150 is unknown, entry/exit/inside
    for i in (144, 146, 149):
        assert np.isnan(ret[i, s01])
    assert np.isfinite(ret[140, s01]) and np.isfinite(ret[150, s01])


def test_features_do_not_read_the_future(world):
    panel, _ = world
    base = features(panel)
    t = 350
    frames, bench = _world()
    for s in frames:  # rewrite everything strictly after t
        later = frames[s].index > DAYS[t]
        frames[s].loc[later, ["open", "high", "low", "close"]] *= 3.0
        frames[s].loc[later, "volume"] *= 7
    changed = build_panel("us", frames, bench, start="2020-01-01", end="2021-08-31")
    after = features(changed)
    for name in base:
        np.testing.assert_allclose(
            base[name][: t + 1], after[name][: t + 1], equal_nan=True
        )
    assert (
        eligibility(panel, base)[: t + 1] == eligibility(changed, after)[: t + 1]
    ).all()


def test_loaders_never_return_rows_after_end(tmp_path):
    rows = pd.DataFrame(
        {
            "date": pd.date_range("2023-12-20", "2024-01-10"),
            **dict.fromkeys(data.BAR_COLUMNS, 1.0),
        }
    )
    rows.to_csv(tmp_path / "AAA.csv", index=False)
    rows.to_csv(tmp_path / "SPY.csv", index=False)
    frames, bench, info = data.load_us(tmp_path, ["AAA", "ZZZ"], end="2023-12-31")
    assert frames["AAA"].index.max() <= pd.Timestamp("2023-12-31")
    assert bench.index.max() <= pd.Timestamp("2023-12-31")
    assert info == {"requested": 2, "obtained": 1, "missing": 1}


def test_yahoo_adjustment_and_traded_value():
    body = (
        b'{"chart":{"result":[{"timestamp":[1700000000,1700086400],'
        b'"indicators":{"quote":[{"open":[10,20],"high":[11,22],"low":[9,18],'
        b'"close":[10,20],"volume":[5,6]}],"adjclose":[{"adjclose":[5,20]}]}}]}}'
    )
    frame = data.parse_yahoo_chart(body)
    assert frame.close.tolist() == [5, 20] and frame.open.tolist() == [5, 20]
    assert frame.traded_value.tolist() == [50, 120]  # raw close x volume


def test_planted_signal_beats_random_controls_and_shuffled_one_does_not():
    frames, bench = _world(n_names=40, drift=0.004)
    panel = build_panel("us", frames, bench, start="2020-01-01", end="2021-08-31")
    window = (str(DAYS[300].date()), str(DAYS[-1].date()))
    series = sweep.run_market(panel, window, horizons=(5,), seeds=spec.SEEDS)[5]
    lo = sweep.cell_metrics(spec.Cell("us", "momentum_60", 5, "long_only"), series)
    ls = sweep.cell_metrics(spec.Cell("us", "momentum_60", 5, "long_short"), series)
    assert lo["net_primary_mean"] > 0 and lo["random_p"] <= 0.05
    assert ls["net_primary_mean"] > 0 and ls["random_p"] <= 0.05
    # the random control is centred on zero excess over the equal-weight universe
    assert abs(lo["random_mean"]) < 0.002
    # a reversal ranker has the opposite orientation here and must not pass
    rev = sweep.cell_metrics(spec.Cell("us", "reversal_5", 5, "long_short"), series)
    assert rev["random_p"] > 0.5 and rev["net_primary_mean"] < 0


def test_controls_use_the_same_cost_and_universe(world):
    panel, window = world
    series = sweep.run_market(panel, window, horizons=(3,), seeds=(1, 2, 3))[3]
    assert series.rand_long.shape == (len(series.dates), 3)
    cell = spec.Cell("us", "momentum_20", 3, "long_short")
    m = sweep.cell_metrics(cell, series)
    cost = 4 * spec.COST_BPS["us"] / 1e4
    gross = m["long_gross_mean"] - m["short_gross_mean"]
    assert m["net_primary_mean"] == pytest.approx(gross - cost, abs=1e-12)
    assert m["long_excess_vs_ew"] + m["short_leg_excess_vs_ew"] == pytest.approx(
        gross, abs=1e-12
    )


def test_newey_west_iid_is_near_standard_and_degenerate_inputs_are_none():
    x = np.random.default_rng(1).normal(0.0, 1.0, 4000)
    assert abs(sweep.newey_west_t(x, 5)) < 4
    assert sweep.newey_west_t(np.zeros(500), 5) is None
    assert sweep.newey_west_t(x[:10], 5) is None


def _row(cell, **over):
    m = {
        "dates": 500,
        "net_primary_mean": 0.01,
        "long_excess_vs_benchmark": 0.01,
        "t_newey_west": 3.0,
        "random_p": 0.01,
        "positive_years": 3,
    }
    m.update(over)
    return {"cell": cell, "side": "long_only", "metrics": m}


def test_carry_forward_requires_every_condition_and_caps():
    good = _row("a")
    assert sweep.carry_forward([good]) == ["a"]
    for bad in (
        {"net_primary_mean": -0.1},
        {"long_excess_vs_benchmark": -0.1},
        {"t_newey_west": 1.9},
        {"t_newey_west": None},
        {"random_p": 0.06},
        {"positive_years": 1},
        {"dates": 50},
    ):
        assert sweep.carry_forward([_row("x", **bad)]) == []
    many = [_row(f"c{i:02d}", t_newey_west=2.0 + i / 10) for i in range(15)]
    got = sweep.carry_forward(many)
    assert len(got) == spec.CARRY_CAP and got[0] == "c14"


def test_redenomination_across_a_gap_is_not_a_return():
    # a 1000:1 token swap with missing sessions in between must not become +99,900%
    frames, bench = _world(n_names=3)
    swapped = frames["S00"].copy()
    swapped.loc[DAYS[200:203], ["open", "high", "low", "close"]] = np.nan
    swapped.loc[DAYS[203:], ["open", "high", "low", "close"]] *= 1000
    frames["S00"] = swapped
    panel = build_panel("us", frames, bench, start="2020-01-01", end="2021-08-31")
    ret, _, _ = outcomes(panel, 5)
    s00 = panel.symbols.index("S00")
    assert np.isnan(ret[194:202, s00]).all()
    assert np.nanmax(np.abs(ret[:, s00])) < 1.0
