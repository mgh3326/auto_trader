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
    leg, missing = methods._resubmit_leg(c, bars, None)
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
    leg, _ = methods._resubmit_leg(c, bars, None)
    assert leg.state == "unknown"


def test_resubmit_censored_when_three_sessions_not_seen():
    bars = make_daily([FLAT] * 3)
    c = _cand(kst(SESSIONS[1], 9, 30), kst(SESSIONS[1], 15, 34), 50.0, 3)
    leg, missing = methods._resubmit_leg(c, bars, None)
    assert leg is None and missing == "resubmit_window_censored"


def test_split_int_has_no_near_leg_below_five_shares():
    c = _cand(kst(SESSIONS[0], 11, 37), kst(SESSIONS[0], 15, 34), 90.0, 4)
    m = methods.build_methods(c, _order(c), D, H0)
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
