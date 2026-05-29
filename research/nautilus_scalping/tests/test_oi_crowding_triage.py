"""ROB-362 PR3 — OI-crowding gross-triage pure machinery.

Pins the methodology that makes the triage honest: daily resampling of the 5-min OI
grid (no intra-day over-counting), non-overlapping close-to-close trades, correct
fade/ride sign, a full-horizon guard, and the cost-blind screen mapping. No network.
"""

import csv

import oi_crowding_triage as t
from discovery.screen import ClassifiedHypothesis, HypothesisSummary

DAY = 86_400_000
D1 = 1_640_995_200_000  # 2022-01-01 00:00:00 UTC
D2 = D1 + DAY


def _closes(prices, start=D1, step=DAY):
    return [(start + i * step, p) for i, p in enumerate(prices)]


# --------------------------------------------------------------------------- #
# daily resample
# --------------------------------------------------------------------------- #
def test_daily_oi_zscore_one_per_day_last_nonnull_wins():
    feats = [
        {"ts": D1, "oi_zscore": 0.5},
        {"ts": D1 + 300_000, "oi_zscore": 1.5},  # later same day -> day1 reading
        {"ts": D1 + 600_000, "oi_zscore": None},  # None never overwrites a real reading
        {"ts": D2, "oi_zscore": -2.2},
    ]
    out = t.daily_oi_zscore(feats)
    assert len(out) == 2
    assert out[0][1] == 1.5
    assert out[1][1] == -2.2


# --------------------------------------------------------------------------- #
# trade generation: sign, threshold, overlap, horizon guard
# --------------------------------------------------------------------------- #
def _gross(trade):
    return trade.net_ref_pnl + trade.commission_ref


def test_fade_and_ride_are_equal_and_opposite_on_rising_price():
    closes = _closes([100, 102, 104, 106, 108, 110, 110, 110])
    sig = [(D1, 3.0)]  # crowded long
    kw = {"threshold": 2.0, "horizon_days": 5, "notional": 1000.0, "ref_fee_bps": 10.0}
    (fade,) = t.crowding_trades(sig, closes, direction="fade", **kw)
    (ride,) = t.crowding_trades(sig, closes, direction="ride", **kw)
    assert _gross(fade) < 0  # short a crowded long on a rising tape -> loses
    assert _gross(ride) > 0  # ride the crowd -> gains
    assert abs(_gross(fade) + _gross(ride)) < 1e-9


def test_negative_z_fade_is_long():
    closes = _closes([100, 102, 104, 106, 108, 110, 110])
    (fade,) = t.crowding_trades(
        [(D1, -3.0)],
        closes,
        direction="fade",
        threshold=2.0,
        horizon_days=5,
        notional=1000.0,
        ref_fee_bps=10.0,
    )
    assert _gross(fade) > 0  # fade a crowded short -> long -> gains as price rises


def test_threshold_gate_blocks_weak_signal():
    closes = _closes([100, 101, 102, 103, 104, 105, 106])
    assert (
        t.crowding_trades(
            [(D1, 1.9)],
            closes,
            direction="ride",
            threshold=2.0,
            horizon_days=5,
            notional=1000.0,
            ref_fee_bps=10.0,
        )
        == []
    )


def test_non_overlapping_holds_skip_signals_inside_horizon():
    closes = _closes([100] * 20)
    sig = [(D1 + i * DAY, 3.0) for i in range(6)]  # qualifies every day, days 0..5
    trades = t.crowding_trades(
        sig,
        closes,
        direction="ride",
        threshold=2.0,
        horizon_days=5,
        notional=1000.0,
        ref_fee_bps=10.0,
    )
    assert len(trades) == 2  # day0 held through day5; only day0 and day5 open


def test_full_horizon_guard_drops_trades_without_a_forward_bar():
    closes = _closes([100, 101, 102])  # only 3 daily bars
    assert (
        t.crowding_trades(
            [(D1, 3.0)],
            closes,
            direction="ride",
            threshold=2.0,
            horizon_days=5,
            notional=1000.0,
            ref_fee_bps=10.0,
        )
        == []
    )


def test_invalid_direction_raises():
    import pytest

    with pytest.raises(ValueError):
        t.crowding_trades(
            [(D1, 3.0)],
            _closes([100, 101]),
            direction="sideways",
            threshold=2.0,
            horizon_days=1,
            notional=1000.0,
            ref_fee_bps=10.0,
        )


# --------------------------------------------------------------------------- #
# spec building + cost-blind triage
# --------------------------------------------------------------------------- #
def test_build_specs_emits_both_directions_pooled_across_symbols():
    closes = {
        "AAA": _closes([100, 102, 104, 106, 108, 110, 110]),
        "BBB": _closes([100, 98, 96, 94, 92, 90, 90]),
    }
    feats = {
        "AAA": [{"ts": D1, "oi_zscore": 3.0}],
        "BBB": [{"ts": D1, "oi_zscore": 3.0}],
    }
    specs = t.build_specs(feats, closes, t.TriageConfig(min_samples=1))
    assert [s["name"] for s in specs] == ["oi_crowding_fade", "oi_crowding_ride"]
    assert all(s["summary"].sample_count == 2 for s in specs)  # pooled across symbols


def test_triage_screens_out_trivial_gross():
    closes = {"AAA": _closes([100] * 10)}  # flat -> zero gross
    feats = {"AAA": [{"ts": D1 + i * DAY, "oi_zscore": 3.0} for i in range(3)]}
    cfg = t.TriageConfig(min_samples=1)
    res = t.triage(t.build_specs(feats, closes, cfg), cfg)
    assert t.overall_verdict(res) == "screened_out"


def _ch(rec):
    s = HypothesisSummary(
        name="x",
        conditions="c",
        sample_count=300,
        gross_expectancy_bps=1.0,
        fee_adjusted_bps=0.0,
        oos_fee_adjusted_bps=None,
        oos_gross_bps=0.5,
    )
    return ClassifiedHypothesis(s, rec, "reason")


def test_overall_verdict_mapping():
    so, prom, nmd = "screened_out", "promote_to_full_validation", "needs_more_data"
    mk = lambda a, b: [  # noqa: E731
        {"direction": "fade", "classified": _ch(a)},
        {"direction": "ride", "classified": _ch(b)},
    ]
    assert t.overall_verdict(mk(so, prom)) == "edge_found"
    assert t.overall_verdict(mk(so, nmd)) == "needs_more_data"
    assert t.overall_verdict(mk(so, so)) == "screened_out"


# --------------------------------------------------------------------------- #
# feature CSV load + frozen config
# --------------------------------------------------------------------------- #
def test_load_oi_features_parses_and_sorts(tmp_path):
    p = tmp_path / "AAA.csv"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ts", "symbol", "oi_zscore"])
        w.writeheader()
        w.writerow({"ts": "200", "symbol": "AAA", "oi_zscore": "1.5"})
        w.writerow({"ts": "100", "symbol": "AAA", "oi_zscore": ""})  # empty -> None
    rows = t.load_oi_features(p)
    assert [r["ts"] for r in rows] == [100, 200]  # chronological
    assert rows[0]["oi_zscore"] is None
    assert rows[1]["oi_zscore"] == 1.5


def test_config_hash_stable_and_sensitive():
    assert t.FROZEN.config_hash() == t.TriageConfig().config_hash()
    assert t.TriageConfig(horizon_days=7).config_hash() != t.FROZEN.config_hash()
