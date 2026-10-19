"""ROB-1404 Part 2A -- pure-function tests (no DB, no network).

Fixtures are built with the v1 price-store finalisers so they carry the
production frame shape (session_date / open_ts / close_ts, yfinance-KR hourly
bars 09:00..14:00 with the last bar folded to the 15:30 close).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from research.rob1383_buy_drought.inputs import TossOrder
from research.rob1383_buy_drought.prices import KST, _finalise, finalise_hourly
from research.rob1383_buy_drought.stats import cluster_bootstrap_ci
from research.rob1404_kr_entry import clock, explore_2b, inference, methods, outcomes
from research.rob1404_kr_entry.population import (
    Candidate,
    epoch_of,
    overlaps_regular_window,
    strategy_label,
    version_at,
)

NOW = datetime(2026, 12, 31, tzinfo=UTC)
SESSIONS = [date(2026, 9, 1) + timedelta(days=i) for i in range(40)]
SESSIONS = [d for d in SESSIONS if d.weekday() < 5][:25]


def kst(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=KST)


def make_daily(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "session_date": SESSIONS[: len(rows)],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [1000.0] * len(rows),
        }
    )
    return _finalise("kr", frame, NOW)


def make_hourly(
    day: date, bars: list[tuple[float, float, float, float]]
) -> pd.DataFrame:
    starts = [kst(day, 9 + i) for i in range(len(bars))]
    frame = pd.DataFrame(
        {
            "start_ts": [s.astimezone(UTC) for s in starts],
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [10.0] * len(bars),
        }
    )
    return finalise_hourly("kr", frame, NOW)


FLAT = (100.0, 101.0, 99.0, 100.0)
D = make_daily([FLAT] * 25)
CAL = clock.SessionCalendar(tuple(SESSIONS))
H0 = make_hourly(
    SESSIONS[0],
    [
        (100.0, 100.5, 99.5, 100.0),
        (110.0, 110.5, 109.5, 110.0),
        (120.0, 120.5, 119.5, 120.0),
        (130.0, 130.5, 129.5, 130.0),
        (140.0, 140.5, 139.5, 140.0),
        (150.0, 150.5, 149.5, 150.0),
    ],
)


# ------------------------------------------------------------------- clock


def test_nep_skips_the_hour_containing_the_signal():
    e = clock.next_executable_price(D, H0, kst(SESSIONS[0], 11, 37))
    assert (e.price, e.basis) == (130.0, "next_hourly_open")  # the 12:00 bar
    assert e.at == pd.Timestamp(kst(SESSIONS[0], 12)).tz_convert(UTC)


def test_nep_inside_last_hour_uses_the_closing_auction_not_a_past_close():
    e = clock.next_executable_price(D, H0, kst(SESSIONS[0], 14, 39))
    assert e.basis == "closing_auction"
    assert e.price == D["close"].iloc[0]
    assert e.at == D["close_ts"].iloc[0]  # prints after the signal


def test_nep_after_auction_cutoff_rolls_to_next_open():
    e = clock.next_executable_price(D, H0, kst(SESSIONS[0], 15, 25))
    assert e.pos == 1 and e.basis == "session_open_daily"


def test_evening_signal_buys_next_open_and_d0_is_next_session():
    t = kst(SESSIONS[0], 16, 4)
    assert clock.d0_index(D, t) == 1
    e = clock.next_executable_price(D, H0, t)
    n = clock.next_day_open(D, H0, t)
    assert (e.pos, n.pos) == (1, 1) and e.price == n.price


def test_premarket_signal_buys_that_sessions_open():
    e = clock.next_executable_price(D, H0, kst(SESSIONS[0], 8, 5))
    assert (e.pos, e.basis, e.price) == (0, "session_open_hourly", 100.0)


def test_next_day_open_for_intraday_signal_is_next_session():
    n = clock.next_day_open(D, H0, kst(SESSIONS[0], 11, 37))
    assert n.pos == 1


def test_first_touch_hour_ignores_the_order_hour():
    # 11:00 bar low 119.5 would touch 125, but it starts before the 11:37 order.
    at = clock.first_touch_hour(
        H0, SESSIONS[0], 125.0, start=pd.Timestamp(kst(SESSIONS[0], 11, 37))
    )
    assert at is None
    at = clock.first_touch_hour(
        H0, SESSIONS[0], 135.0, start=pd.Timestamp(kst(SESSIONS[0], 11, 37))
    )
    assert at == pd.Timestamp(kst(SESSIONS[0], 12)).tz_convert(UTC)


# ------------------------------------------------------------ fill states


@pytest.mark.parametrize(
    ("strict", "incl", "limit", "state"),
    [
        (
            98.0,
            98.0,
            100.0,
            "filled",
        ),  # trades through (KRX tick at 100 = 0.1? -> 98 << 100)
        (None, 100.0, 100.0, "unknown"),  # touch, no strict-window trade-through
        (100.0, 100.0, 100.0, "unknown"),  # exactly at the limit
        (101.0, 101.0, 100.0, "unfilled"),
        (None, None, 100.0, "unknown"),  # no bars -> never "unfilled"
    ],
)
def test_classify_limit_three_states(strict, incl, limit, state):
    assert methods.classify_limit(strict, incl, limit)[0] == state


@pytest.mark.parametrize(
    ("q", "k"), [(1, 0), (2, 0), (4, 0), (5, 1), (7, 1), (9, 1), (10, 2), (98, 19)]
)
def test_near_shares_whole_and_at_most_twenty_percent(q, k):
    assert methods.near_shares(q) == k
    assert methods.near_shares(q) <= 0.2 * q


def _cand(signal: datetime, expiry: datetime, anchor: float, q: int) -> Candidate:
    return Candidate(
        order_id="o1",
        symbol="000001",
        signal_at=signal,
        expired_at=expiry,
        anchor=anchor,
        quantity=q,
        strategy="open_trade",
        nxt_only=False,
        parent_id="p1",
        parent_rank=0,
        policy_version="2026-09-30.1",
        policy_epoch="E4_2026-09-30.1",
    )


def _order(c: Candidate) -> TossOrder:
    return TossOrder(
        order_id=c.order_id,
        market="kr",
        symbol=c.symbol,
        side="BUY",
        status="REJECTED",
        order_type="LIMIT",
        price=c.anchor,
        quantity=float(c.quantity),
        ordered_at=c.signal_at,
        canceled_at=c.expired_at,
        filled_qty=0.0,
        avg_fill=None,
        filled_at=None,
    )


def test_resubmit_gap_down_fills_at_the_open():
    rows = [FLAT, (100.0, 101.0, 99.0, 100.0), (95.0, 96.0, 94.0, 95.0)] + [FLAT] * 22
    bars = make_daily(rows)
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[0], 15, 34), 96.0, 3)
    leg, missing = methods._resubmit_leg(c, bars, None, CAL)
    assert missing is None
    assert (leg.state, leg.price, leg.pos, leg.reason) == (
        "filled",
        95.0,
        2,
        "gap_down_open",
    )


def test_resubmit_touch_only_is_unknown_not_cash():
    rows = [FLAT, (100.0, 101.0, 96.0, 100.0)] + [FLAT] * 23
    bars = make_daily(rows)
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[0], 15, 34), 96.0, 3)
    leg, _ = methods._resubmit_leg(c, bars, None, CAL)
    assert leg.state == "unknown"


def test_resubmit_censored_when_three_sessions_not_seen():
    bars = make_daily([FLAT] * 3)
    c = _cand(kst(SESSIONS[1], 9, 30), kst(SESSIONS[1], 15, 34), 50.0, 3)
    leg, missing = methods._resubmit_leg(c, bars, None, CAL)
    assert leg is None and missing == "immature:resubmit_window_censored"


def test_split_int_has_no_near_leg_below_five_shares():
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c, _order(c), D, H0, CAL)
    assert [leg.state for leg in m["split_int"].legs] == ["unfilled"]
    assert m["split_int"].detail["near_shares"] == 0
    assert len(m["split_frac"].legs) == 2  # v1 fractional idealisation keeps it


# ------------------------------------------------------------- outcomes


def _legs(*legs: methods.Leg) -> methods.MethodLegs:
    return methods.MethodLegs("x", tuple(legs))


def test_unknown_leg_never_becomes_zero_return():
    leg = methods.Leg(100.0, 2, "unknown", reason="touch_without_trade_through")
    r = outcomes.evaluate(
        _legs(leg),
        D,
        H0,
        kst(SESSIONS[0], 8, 5),
        2,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "unknown"
    key = f"y__{outcomes.BASE_COST.name}"
    assert r[key] is None
    assert r[f"{key}__if_unknown_unfilled"] == 0.0
    assert r[f"{key}__if_unknown_filled"] != 0.0


def test_immature_horizon_has_no_value():
    bars = make_daily([FLAT] * 4)
    leg = methods.Leg(100.0, 1, "filled", True, D["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        bars,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "immature"
    assert not any(k.startswith("y__") for k in r)


def test_leg_pnl_costs_and_etf_tax():
    leg = methods.Leg(100.0, 10, "filled", marketable=True)
    base = outcomes.BASE_COST
    pnl = outcomes.leg_pnl(leg, 110.0, base, base.tax)
    assert pnl == pytest.approx(
        10 * 10 - 10 * 100 * 0.00015 - 10 * 110 * (0.00015 + 0.002)
    )
    assert outcomes.leg_pnl(leg, 110.0, base, 0.0) > pnl
    tick = outcomes.CostModel("t", 0.0, 0.0, "tick")
    # buy one tick higher, sell one tick lower (KRX tick 0.1 below 1,000? app table)
    assert outcomes.leg_pnl(leg, 110.0, tick, 0.0) < 100.0


def test_market_adjustment_uses_each_legs_own_interval():
    idx = outcomes.Bench(make_daily([FLAT] * 25), None)
    leg = methods.Leg(100.0, 1, "filled", True, D["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        D,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=True,
        market_bench=idx,
        sector_peers=None,
    )
    # stock 100 -> 100 and index open 100 -> close 100: adj == net
    assert r[f"y_mktadj__{outcomes.BASE_COST.name}"] == pytest.approx(
        r[f"y__{outcomes.BASE_COST.name}"]
    )


def test_sector_adjustment_missing_below_min_peers_not_zero():
    peer = outcomes.Bench(make_daily([FLAT] * 25), None)
    leg = methods.Leg(100.0, 1, "filled", True, D["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        D,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=[peer, peer],
    )
    assert r[f"y_sectoradj__{outcomes.BASE_COST.name}"] is None
    assert r["sector_adj_missing"] is True


# ------------------------------------------------------------ inference


def test_symbol_cluster_ci_equals_v1_algorithm():
    rng = np.random.default_rng(7)
    v = rng.normal(size=40).tolist()
    sym = [f"s{i % 9}" for i in range(40)]
    reps = inference.cluster_replicates(v, sym, seed=1383)
    lo, hi = np.percentile(reps, 2.5), np.percentile(reps, 97.5)
    assert (lo, hi) == pytest.approx(cluster_bootstrap_ci(v, sym))


def test_block_bootstrap_keeps_empty_calendar_days_and_same_day_rows():
    cal = [date(2026, 9, d) for d in range(1, 21)]
    # day 1 holds +10 and -10 (sum 0); day 20 holds +1. If day-1 rows always
    # travel together every finite replicate mean lies in [0, 1].
    values = [10.0, -10.0, 1.0]
    days = [cal[0], cal[0], cal[19]]
    reps = inference.block_replicates(values, days, cal, block=5, resamples=500, seed=1)
    finite = reps[np.isfinite(reps)]
    assert finite.size and finite.min() >= 0.0 and finite.max() <= 1.0
    # empty calendar days stay in: some replicates draw no observation at all
    assert np.isnan(reps).any()
    with pytest.raises(ValueError):
        inference.block_replicates([1.0], [date(2027, 1, 1)], cal)


def test_verdict_never_claims_no_edge_when_ci_spans_zero():
    assert inference.verdict(-1.0, 2.0).startswith("판별 불가")
    assert inference.verdict(-3.0, -0.1).startswith("음")
    assert inference.verdict(0.1, 3.0).startswith("양")
    assert inference.verdict(0.0, 0.0).startswith("0(")


def test_required_n_scales_with_design_effect():
    n1 = inference.required_n(7.0, 1.0, 1.0)
    assert n1 == pytest.approx(6.18 * 49, rel=0.01)
    assert inference.required_n(7.0, 1.0, 2.0) == pytest.approx(2 * n1)


# ----------------------------------------------------------- population


def test_version_at_and_epoch_buckets():
    seen = [
        (datetime(2026, 9, 7, 5, 38, tzinfo=UTC), "2026-09-07.3"),
        (datetime(2026, 9, 25, 11, 23, tzinfo=UTC), "2026-09-24.1"),
        (datetime(2026, 10, 1, 2, 38, tzinfo=UTC), "2026-09-30.1"),
    ]
    assert version_at(seen, datetime(2026, 9, 1, tzinfo=UTC)) is None
    assert version_at(seen, datetime(2026, 9, 30, tzinfo=UTC)) == "2026-09-24.1"
    assert epoch_of("2026-09-02.1") == "E0_pre_S176"
    assert epoch_of("2026-09-07.3") == "E1_S176_moderate"
    assert epoch_of("2026-09-24.1") == "E2_664_one_share"
    assert epoch_of("2026-09-28.2") == "E3_876_877"
    assert epoch_of("2026-09-30.1") == "E4_2026-09-30.1"
    assert epoch_of(None) == "unknown"


def test_nxt_only_is_judged_by_clock_not_by_cached_bars():
    fri = date(2026, 10, 16)
    # Regular-session order on a day with no cached bar is NOT NXT-only.
    assert overlaps_regular_window(kst(fri, 9, 12), kst(fri, 20, 4))
    assert overlaps_regular_window(kst(fri, 11, 37), kst(fri, 15, 34))
    # Evening NXT order (placed 16:04, expired 20:03) never meets 09:00-15:30.
    assert not overlaps_regular_window(kst(fri, 16, 4), kst(fri, 20, 3))
    assert not overlaps_regular_window(kst(fri, 15, 49), kst(fri, 20, 3))
    # Pre-market NXT order that lives into the regular session does overlap.
    assert overlaps_regular_window(kst(fri, 8, 5), kst(fri, 20, 3))


def test_strategy_label():
    assert strategy_label("kr-open-trade-2026-10-16-0905") == "open_trade"
    assert strategy_label("kr-nxt-open-2026-08-31") == "open_trade"
    assert strategy_label("kr-dca-154") == "dca"
    assert strategy_label("strategy-lab") == "strategy_lab"
    assert strategy_label(None) == "unknown"


# --------------------------------------------------------------- 2B explore


def test_wilder_atr_matches_hand_computation():
    high = np.array(
        [10, 11, 12, 11, 13, 12, 14, 13, 15, 14, 16, 15, 17, 16, 18, 17], float
    )
    low = high - 2
    close = high - 1
    tr = np.maximum.reduce(
        [high[1:] - low[1:], abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])]
    )
    expected = tr[:14].mean()
    expected = (expected * 13 + tr[14]) / 14
    assert explore_2b.wilder_atr(high, low, close) == pytest.approx(expected)
    assert explore_2b.wilder_atr(high[:10], low[:10], close[:10]) is None


# ------------------------------------------- tester r1 regressions (B1-B3)


def test_r1_b1_partial_session_daily_low_without_hourly_is_not_a_fill():
    # Low 98 may have printed at 09:00, before the 11:37 order; with no hourly
    # bars nothing proves a post-order trade-through -> UNKNOWN, not FILLED.
    bars = make_daily([(100.0, 101.0, 98.0, 100.0)] + [FLAT] * 24)
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 120.0, 5)
    leg, _ = methods._near_leg(c, _order(c), bars, None, 1, CAL)
    assert leg.state == "unknown"
    r = outcomes.evaluate(
        methods.MethodLegs("split_int", (leg,)),
        bars,
        None,
        c.signal_at,
        5,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "unknown" and r[f"y__{outcomes.BASE_COST.name}"] is None


def test_r1_b1_whole_session_daily_low_is_exact_evidence():
    # An order alive for the whole next session: its daily low is post-order.
    bars = make_daily(
        [(100.0, 101.0, 97.0, 100.0), (100.0, 101.0, 90.0, 100.0)] + [FLAT] * 23
    )
    c = _cand(kst(SESSIONS[0], 16, 4), kst(SESSIONS[1], 20, 3), 120.0, 5)
    leg, _ = methods._near_leg(c, _order(c), bars, None, 1, CAL)
    assert (leg.state, leg.pos) == ("filled", 1)  # day 1 is wholly post-order
    # Partial day 0 (no hourly) is skipped as evidence; day 1 still proves it.
    c2 = _cand(kst(SESSIONS[0], 15, 0), kst(SESSIONS[1], 20, 3), 120.0, 5)
    leg2, _ = methods._near_leg(c2, _order(c2), bars, None, 1, CAL)
    # day 0's low 97 (possibly pre-order, no hourly) trades through the 99
    # limit but is no evidence; the fill must come from day 1, not be masked
    assert (leg2.state, leg2.pos) == ("filled", 1)
    assert leg2.at >= pd.Timestamp(c2.signal_at)


def test_r1_b2_nan_low_is_unknown_not_cash():
    nan = float("nan")
    assert methods.classify_limit(nan, nan, 99.0)[0] == "unknown"
    assert methods.classify_limit(None, nan, 99.0)[0] == "unknown"
    bars = make_daily([FLAT] * 25)
    bars.loc[1, "low"] = nan  # _finalise drops NaN rows; a cache read does not
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[0], 15, 34), 96.0, 3)
    leg, _ = methods._resubmit_leg(c, bars, None, CAL)
    assert leg.state == "unknown" and leg.reason == "nonfinite_low"


def test_r1_b3_missing_exit_bar_is_data_missing_not_next_row():
    bars = make_daily([FLAT] * 25)
    bars = bars[bars["session_date"] != SESSIONS[5]].reset_index(drop=True)
    leg = methods.Leg(100.0, 1, "filled", True, D["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        bars,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "data_missing" and r["missing_reason"] == "exit_bar_missing"
    assert not any(k.startswith("y__") for k in r)


def test_r1_b3_exit_after_last_bar_is_immature_and_d0_gap_is_missing():
    short = make_daily([FLAT] * 4)
    leg = methods.Leg(100.0, 1, "filled", True, D["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        short,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "immature"
    gap = D[D["session_date"] != SESSIONS[0]].reset_index(drop=True)
    r = outcomes.evaluate(
        _legs(leg),
        gap,
        None,
        kst(SESSIONS[0], 8, 5),
        1,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert r["status"] == "data_missing" and r["missing_reason"] == "d0_bar_missing"


def test_r1_b3_resubmit_counts_calendar_sessions_and_flags_gaps():
    bars = make_daily([FLAT] * 25)
    bars = bars[bars["session_date"] != SESSIONS[2]].reset_index(drop=True)
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[0], 15, 34), 50.0, 3)
    leg, _ = methods._resubmit_leg(c, bars, None, CAL)
    assert leg.state == "unknown" and leg.reason == "resubmit_bar_missing"


def test_r1_next_day_open_requires_the_calendar_next_session():
    gap = D[D["session_date"] != SESSIONS[1]].reset_index(drop=True)
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c, _order(c), gap, H0, CAL)
    assert m["next_day_open"].missing == "data_missing:entry_bar_missing"
    assert m["nep"].missing is None  # D0 bar exists; NEP prints on D0


def test_r1_markout_counts_calendar_sessions():
    gap = D[D["session_date"] != SESSIONS[5]].reset_index(drop=True)
    leg = methods.Leg(100.0, 1, "filled", True, gap["open_ts"].iloc[0], 0)
    rows = outcomes.markouts(_legs(leg), gap, None, CAL)
    assert rows[0]["markout5"] is None and rows[0]["markout5_status"] == "data_missing"
    assert rows[0]["fill_session"] == str(SESSIONS[0])


def test_explore_atr_limit_nonpositive_is_flagged_not_raised():
    rows = [(10.0, 30.0, 1.0, 2.0)] * 25
    bars = make_daily(rows)
    out = explore_2b.candidate_row(bars, None, kst(SESSIONS[19], 16, 0), 2.0)
    assert out["atr_limit_nonpositive"] is True


# ------------------------------------------- tester r2 regressions


def test_r2_b1_calendar_gap_inside_lifetime_is_unknown_not_cash():
    # Order lives SESSIONS[0] 09:30 .. SESSIONS[2] 20:00; the stock has no bar
    # on SESSIONS[1] -> price there is unobserved -> no "never touched" proof.
    high_lows = make_daily([(100.0, 101.0, 99.8, 100.0)] * 25)  # near = 99
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[2], 20, 3), 50.0, 5)
    leg, _ = methods._near_leg(c, _order(c), high_lows, None, 1, CAL)
    assert leg.state == "unfilled"  # control: every session seen, lows > 99
    gap = high_lows[high_lows["session_date"] != SESSIONS[1]].reset_index(drop=True)
    leg, _ = methods._near_leg(c, _order(c), gap, None, 1, CAL)
    assert leg.state == "unknown", leg


def test_r2_b2_partial_hourly_cache_is_not_a_complete_window():
    # Hourly bars 09:00, 10:00, 12:00..14:00 (11:00 dropped); the dropped hour
    # could hold the touch -> fall back to the daily low as the bound.
    hours = [(100.0, 101.0, 99.5, 100.0)] * 6
    full = make_hourly(SESSIONS[0], hours)
    part = full[full["start_ts"] != pd.Timestamp(kst(SESSIONS[0], 11))].reset_index(
        drop=True
    )
    assert methods.hourly_covers_session(full, D, 0)
    assert not methods.hourly_covers_session(part, D, 0)
    bars = make_daily([(100.0, 101.0, 98.0, 100.0)] + [FLAT] * 24)
    c = _cand(kst(SESSIONS[0], 9, 30), kst(SESSIONS[0], 15, 34), 50.0, 5)
    # near = tick_floor(ref * 0.995); daily low 98 touches it, the partial
    # hourly lows (99.5) do not: the leg must not be proven unfilled.
    leg, detail = methods._near_leg(c, _order(c), bars, part, 1, CAL)
    assert detail["near_limit"] >= 98.0
    assert leg.state != "unfilled", (leg, detail)
    leg_full, _ = methods._near_leg(c, _order(c), bars, full, 1, CAL)
    assert leg_full.state == "unfilled"  # complete cache: the 99.5 lows prove it


def test_r2_s1_nonfinite_markout_close_is_missing():
    bars = make_daily([FLAT] * 25)
    bars.loc[5, "close"] = float("nan")
    leg = methods.Leg(100.0, 1, "filled", True, bars["open_ts"].iloc[0], 0)
    rows = outcomes.markouts(_legs(leg), bars, None, CAL)
    assert rows[0]["markout5"] is None and rows[0]["markout5_status"] == "data_missing"


def test_nep_missing_next_hour_is_missing_not_a_later_hour():
    no_noon = H0[H0["start_ts"] != pd.Timestamp(kst(SESSIONS[0], 12))].reset_index(
        drop=True
    )
    assert clock.next_executable_price(D, no_noon, kst(SESSIONS[0], 11, 37)) is None
    assert clock.next_executable_price(D, None, kst(SESSIONS[0], 11, 37)) is None
    # a later signal whose next hour exists is unaffected
    e = clock.next_executable_price(D, no_noon, kst(SESSIONS[0], 12, 10))
    assert (e.price, e.basis) == (140.0, "next_hourly_open")
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c, _order(c), D, no_noon, CAL)
    assert m["nep"].missing == "data_missing:entry_bar_missing"


def test_headline_interval_is_the_envelope_of_dependence_bootstraps():
    rng = np.random.default_rng(3)
    cal = [date(2026, 8, 1) + timedelta(days=i) for i in range(40)]
    v = rng.normal(-1.0, 3.0, size=30).tolist()
    days = [cal[i] for i in rng.integers(0, 40, size=30)]
    syms = [f"s{i % 7}" for i in range(30)]
    out = inference.summarise(v, symbols=syms, days=days, calendar=cal)
    names = ("symbol", "date_block", "date_block5", "date_block20")
    assert out["ci_lo"] == min(out[f"{n}_lo"] for n in names)
    assert out["ci_hi"] == max(out[f"{n}_hi"] for n in names)
    assert out["headline_ci"].startswith("envelope(")
    assert out["mde_one_sided_80"] == pytest.approx(
        inference.MDE_FACTOR * max(out[f"{n}_se"] for n in names)
    )


def test_nep_signal_exactly_on_an_hour_excludes_the_bar_starting_then():
    e = clock.next_executable_price(D, H0, kst(SESSIONS[0], 12, 0))
    assert e.at == pd.Timestamp(kst(SESSIONS[0], 13)).tz_convert(UTC)
    assert e.price == 140.0
    # an order placed exactly at a bar start may fill in that bar (it is after)
    at = clock.first_touch_hour(
        H0, SESSIONS[0], 135.0, start=pd.Timestamp(kst(SESSIONS[0], 12))
    )
    assert at == pd.Timestamp(kst(SESSIONS[0], 12)).tz_convert(UTC)


def test_r2_s2_missing_d0_bar_before_cutoff_is_not_read_as_a_roll():
    gap = D[D["session_date"] != SESSIONS[0]].reset_index(drop=True)
    c = _cand(kst(SESSIONS[0], 15, 19), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c, _order(c), gap, H0, CAL)
    assert m["nep"].missing == "data_missing:entry_bar_missing"
    c_late = _cand(kst(SESSIONS[0], 15, 21), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c_late, _order(c_late), D, H0, CAL)
    assert m["nep"].missing is None and m["nep"].legs[0].pos == 1  # genuine roll


def test_r2_b3_nonfinite_reference_is_data_missing():
    bars = make_daily([FLAT] * 25)
    bars.loc[0, "open"] = float("inf")
    leg = methods.Leg(100.0, 5, "filled", True, bars["open_ts"].iloc[0], 0)
    r = outcomes.evaluate(
        _legs(leg),
        bars,
        None,
        kst(SESSIONS[0], 8, 5),
        5,
        5,
        cal=CAL,
        is_etf=False,
        market_bench=None,
        sector_peers=None,
    )
    assert (
        r["status"] == "data_missing" and r["missing_reason"] == "reference_nonfinite"
    )


def test_r2_s3_explore_summary_with_no_valid_atr_row():
    import research.rob1404_kr_entry.run as run_mod

    row = explore_2b.candidate_row(
        make_daily([(10.0, 30.0, 1.0, 2.0)] * 25), None, kst(SESSIONS[19], 16, 0), 2.0
    )
    ex = pd.DataFrame([{"parent_rank": 0, **row}])
    out = run_mod.explore_summary(ex).set_index("level")
    assert out.loc["parent", "n_atr_limit_nonpositive"] == 1
    assert out.loc["parent", "n_atr_limit_valid"] == 0
    assert out.loc["parent", "n_atr_limit_shallower_than_deep_min_3pct"] == 0


def test_bench_return_is_none_on_nonfinite_prices():
    bars = make_daily([FLAT] * 25)
    bars.loc[5, "close"] = float("nan")
    b = outcomes.Bench(bars, None)
    assert b.ret(bars["open_ts"].iloc[0], SESSIONS[5]) is None
    assert b.ret(bars["open_ts"].iloc[0], SESSIONS[6]) == pytest.approx(0.0)


def test_explore_nonfinite_atr_is_unavailable_not_nonpositive():
    bars = make_daily([FLAT] * 25)
    bars.loc[10, "high"] = float("nan")
    out = explore_2b.candidate_row(bars, None, kst(SESSIONS[19], 16, 0), 90.0)
    assert out["atr_available"] is False and "atr_limit_nonpositive" not in out


# ------------------------------------------- tester r3 regressions


def test_r3_b1_truncated_hourly_tail_is_not_full_coverage():
    full = make_hourly(SESSIONS[0], [(100.0, 100.5, 100.0, 100.0)] * 6)
    assert methods.hourly_covers_session(full, D, 0)
    tail = full.copy()
    tail.loc[tail.index[-1], "end_ts"] = pd.Timestamp(kst(SESSIONS[0], 15)).tz_convert(
        UTC
    )
    assert not methods.hourly_covers_session(tail, D, 0)
    bars = make_daily([(100.0, 101.0, 98.0, 100.0)] + [FLAT] * 24)
    c = _cand(kst(SESSIONS[0], 14, 10), kst(SESSIONS[0], 15, 34), 50.0, 5)
    leg, detail = methods._near_leg(c, _order(c), bars, tail, 1, CAL)
    assert leg.state != "unfilled", (leg, detail)  # 98 at 15:15 is possible


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 0.0, -1.0])
def test_r3_b2_invalid_reference_is_data_missing_in_build_methods(bad):
    bars = make_daily([FLAT] * 25)
    bars.loc[0, "open"] = bad
    c = _cand(kst(SESSIONS[0], 8, 5), kst(SESSIONS[0], 15, 34), 90.0, 5)
    m = methods.build_methods(c, _order(c), bars, None, CAL)  # must not raise
    assert m["split_int"].missing == "data_missing:reference_invalid"
    assert m["split_frac"].missing == "data_missing:reference_invalid"


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_r3_b3_nonpositive_entry_price_is_data_missing(bad):
    h = H0.copy()
    h.loc[h["start_ts"] == pd.Timestamp(kst(SESSIONS[0], 12)), "open"] = bad
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 90.0, 5)
    m = methods.build_methods(c, _order(c), D, h, CAL)
    assert m["nep"].missing == "data_missing:entry_bar_missing"
    assert methods.valid_price(1.0) and not methods.valid_price(bad)


def test_r3_s1_uncomputable_envelope_member_gives_no_verdict(monkeypatch):
    real = inference.block_replicates

    def broken(values, days, calendar, *, block=10, **kw):
        r = real(values, days, calendar, block=block, **kw)
        return np.full_like(r, np.nan) if block == 20 else r

    monkeypatch.setattr(inference, "block_replicates", broken)
    cal = [date(2026, 8, 1) + timedelta(days=i) for i in range(10)]
    out = inference.summarise(
        [1.0, 2.0, 3.0], symbols=["a", "b", "c"], days=cal[:3], calendar=cal
    )
    assert out["headline_ci"].startswith("envelope_incomplete(")
    assert inference.verdict(out["ci_lo"], out["ci_hi"]).startswith("판별 불가")
