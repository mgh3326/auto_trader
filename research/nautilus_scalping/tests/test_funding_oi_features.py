"""ROB-356 (PR2) — PIT-safe funding+OI feature construction.

These tests pin the survivorship/known-after semantics that are the whole point of
the artifact:

  * post-delist frozen rows trimmed at ``delisted_at`` EXCLUSIVE (funding + OI);
  * funding attached to the OI grid by BACKWARD as-of join (no future leakage):
    a realized ``last_funding_rate`` reaches an OI row only at/after its ``calc_time``;
  * per-row ``funding_interval_hours`` carried through interval changes (8h->4h);
  * feature rows bounded by per-symbol OI availability (the dense crowding axis);
  * OI features (delta / pct-change / rolling z) derived from ACTUAL open interest,
    never an OHLCV/volume proxy; zero-variance windows are bounded, not NaN.
"""

import funding_oi_features as fof
from funding_oi_archive import FundingRow, MetricRow


def _m(ts: int, oi: float = 100.0, oiv: float = 1.0) -> MetricRow:
    return MetricRow(
        create_time=ts,
        symbol="EOSUSDT",
        sum_open_interest=oi,
        sum_open_interest_value=oiv,
        count_toptrader_long_short_ratio=1.0,
        sum_toptrader_long_short_ratio=1.0,
        count_long_short_ratio=1.0,
        sum_taker_long_short_vol_ratio=1.0,
    )


def _f(ts: int, rate: float = 0.0001, interval: int = 8) -> FundingRow:
    return FundingRow(
        calc_time=ts, funding_interval_hours=interval, last_funding_rate=rate
    )


# --------------------------------------------------------------------------- #
# delist trim — EXCLUSIVE
# --------------------------------------------------------------------------- #
def test_trim_metrics_at_delist_is_exclusive():
    rows = [_m(100), _m(200), _m(300)]
    out = fof.trim_metrics(rows, delisted_at=200)
    assert [r.create_time for r in out] == [100]  # 200 dropped (>= exclusive bound)


def test_trim_funding_at_delist_is_exclusive():
    rows = [_f(100), _f(200), _f(300)]
    out = fof.trim_funding(rows, delisted_at=200)
    assert [r.calc_time for r in out] == [100]


def test_trim_none_keeps_all_rows():
    rows = [_m(100), _m(200)]
    assert fof.trim_metrics(rows, delisted_at=None) == rows


# --------------------------------------------------------------------------- #
# backward as-of join — KNOWN-AFTER, no future leakage
# --------------------------------------------------------------------------- #
def test_asof_funding_picks_latest_at_or_before():
    funding = [_f(100, 0.1), _f(300, 0.2)]
    assert fof.asof_funding(250, funding).last_funding_rate == 0.1  # 300 not yet known
    assert (
        fof.asof_funding(300, funding).last_funding_rate == 0.2
    )  # known exactly at calc_time


def test_asof_funding_none_before_first_calc_time():
    funding = [_f(200, 0.2)]
    assert fof.asof_funding(199, funding) is None  # known-after: nothing realized yet


# --------------------------------------------------------------------------- #
# build_features — grid, bounding, leakage, interval carry
# --------------------------------------------------------------------------- #
def test_features_are_on_the_oi_grid_and_bounded_by_oi_start():
    # funding starts BEFORE OI; the feature grid still starts at the first OI row
    funding = [_f(50, 0.1), _f(150, 0.2)]
    metrics = [_m(100), _m(200)]
    feats = fof.build_features("EOSUSDT", funding, metrics)
    assert [f.ts for f in feats] == [100, 200]  # OI start bounds the panel


def test_features_no_future_funding_leakage():
    funding = [_f(150, 0.2)]
    metrics = [_m(100), _m(200)]
    feats = fof.build_features("EOSUSDT", funding, metrics)
    # OI@100 predates the only funding calc (150) -> no realized funding known yet
    assert feats[0].last_funding_rate is None
    assert feats[0].funding_calc_time is None
    # OI@200 is at/after calc 150 -> realized funding now known
    assert feats[1].last_funding_rate == 0.2
    assert feats[1].funding_calc_time == 150


def test_features_carry_per_row_funding_interval_change():
    funding = [_f(100, 0.1, interval=8), _f(160, 0.2, interval=4)]  # 8h -> 4h
    metrics = [_m(120), _m(180)]
    feats = fof.build_features("EOSUSDT", funding, metrics)
    assert feats[0].funding_interval_hours == 8  # asof 100
    assert feats[1].funding_interval_hours == 4  # asof 160 (interval changed)


def test_features_trim_delisted_before_building():
    funding = [_f(100, 0.1)]
    metrics = [_m(100), _m(200), _m(300)]
    feats = fof.build_features("EOSUSDT", funding, metrics, delisted_at=300)
    assert [f.ts for f in feats] == [100, 200]  # 300 frozen-tail dropped (exclusive)


# --------------------------------------------------------------------------- #
# OI features from ACTUAL open interest
# --------------------------------------------------------------------------- #
def test_oi_delta_and_pct_change():
    metrics = [_m(100, oi=100.0), _m(200, oi=110.0)]
    feats = fof.build_features("EOSUSDT", [], metrics)
    assert feats[0].oi_delta is None and feats[0].oi_pct_change is None  # no prior
    assert feats[1].oi_delta == 10.0
    assert abs(feats[1].oi_pct_change - 0.1) < 1e-12


def test_oi_pct_change_none_when_prev_is_zero():
    metrics = [_m(100, oi=0.0), _m(200, oi=5.0)]
    feats = fof.build_features("EOSUSDT", [], metrics)
    assert feats[1].oi_pct_change is None  # guard div-by-zero


def test_oi_rolling_zscore_zero_variance_is_zero_not_nan():
    metrics = [_m(t, oi=100.0) for t in (100, 200, 300)]
    feats = fof.build_features("EOSUSDT", [], metrics, oi_window=3)
    assert feats[0].oi_zscore is None and feats[1].oi_zscore is None  # warmup
    assert feats[2].oi_zscore == 0.0  # constant series -> bounded 0, never NaN


def test_oi_rolling_zscore_known_value():
    # window=3 over [100,110,120]; mean=110, pop std=sqrt(200/3); z of last
    metrics = [_m(100, oi=100.0), _m(200, oi=110.0), _m(300, oi=120.0)]
    feats = fof.build_features("EOSUSDT", [], metrics, oi_window=3)
    import math

    expected = (120.0 - 110.0) / math.sqrt(200.0 / 3.0)
    assert abs(feats[2].oi_zscore - expected) < 1e-9
