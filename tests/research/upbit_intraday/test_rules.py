from __future__ import annotations

import numpy as np
import pytest

from research.upbit_intraday import evaluate as E
from research.upbit_intraday import rules as R

N_SLOTS = (
    R.epoch(R.DATA_END_EXCLUSIVE_UTC) - R.epoch(R.DATA_START_UTC)
) // R.BAR_SECONDS
SLOT0 = R.epoch(R.DATA_START_UTC) // R.BAR_SECONDS
# a slot inside the dev half, far from the edges
S_DEV = 1500


def make_panel(markets: list[str], *, q: float = 30e6) -> R.Panel:
    nm = len(markets)
    price = np.full((nm, N_SLOTS), 100.0)
    return R.Panel(
        markets=tuple(markets),
        slot0=SLOT0,
        o=price.copy(),
        h=price * 1.001,
        l=price * 0.999,
        c=price.copy(),
        v=np.full((nm, N_SLOTS), 1.0),
        q=np.full((nm, N_SLOTS), q),
    )


def spike(panel: R.Panel, mi: int, s: int, *, close: float, q: float) -> None:
    panel.c[mi, s] = close
    panel.h[mi, s] = max(close, panel.h[mi, s])
    panel.l[mi, s] = min(close, panel.l[mi, s])
    panel.q[mi, s] = q


def test_up_signal_needs_price_break_volume_multiple_and_floor():
    p = make_panel(["KRW-AAA"])
    spike(p, 0, S_DEV, close=105.0, q=3.5 * 30e6)
    mask = R.raw_signal_mask(p, 1, 16, 3.0)
    assert np.flatnonzero(mask[0]).tolist() == [S_DEV]
    # X=5 needs 5x the median
    assert not R.raw_signal_mask(p, 1, 16, 5.0)[0].any()
    # equal to the prior high is not a break (close <= max high)
    p2 = make_panel(["KRW-AAA"])
    spike(p2, 0, S_DEV, close=100.05, q=4 * 30e6)
    assert not R.raw_signal_mask(p2, 1, 16, 3.0)[0].any()
    # below the 20M KRW floor even with a huge multiple
    p3 = make_panel(["KRW-AAA"], q=1e6)
    spike(p3, 0, S_DEV, close=110.0, q=15e6)
    assert not R.raw_signal_mask(p3, 1, 16, 3.0)[0].any()


def test_down_signal_mirrors_up():
    p = make_panel(["KRW-AAA"])
    spike(p, 0, S_DEV, close=95.0, q=3.5 * 30e6)
    assert np.flatnonzero(R.raw_signal_mask(p, -1, 16, 3.0)[0]).tolist() == [S_DEV]
    assert not R.raw_signal_mask(p, 1, 16, 3.0)[0].any()


def test_lookback_coverage_gaps_make_no_signal_not_a_signal():
    p = make_panel(["KRW-AAA"])
    spike(p, 0, S_DEV, close=105.0, q=4 * 30e6)
    # 5 of the prior 16 slots have no candle: 11/16 < 75% coverage
    for s in range(S_DEV - 5, S_DEV):
        for arr in (p.o, p.h, p.l, p.c, p.v, p.q):
            arr[0, s] = np.nan
    assert not R.raw_signal_mask(p, 1, 16, 3.0)[0].any()
    # 4 missing (12/16 = 75%) still counts; the missing slots are zero volume
    # in the median, which stays at the observed level (>= 12 of 16 present)
    q = make_panel(["KRW-AAA"])
    spike(q, 0, S_DEV, close=105.0, q=4 * 30e6)
    for s in range(S_DEV - 4, S_DEV):
        for arr in (q.o, q.h, q.l, q.c, q.v, q.q):
            arr[0, s] = np.nan
    assert np.flatnonzero(R.raw_signal_mask(q, 1, 16, 3.0)[0]).tolist() == [S_DEV]


def test_signal_ignores_the_future():
    p = make_panel(["KRW-AAA"])
    spike(p, 0, S_DEV, close=105.0, q=4 * 30e6)
    before = R.raw_signal_mask(p, 1, 16, 3.0)[0].copy()
    # change every bar after the signal bar (entry bar included)
    rng = np.random.default_rng(1)
    for arr in (p.o, p.h, p.l, p.c, p.q):
        arr[0, S_DEV + 1 :] = arr[0, S_DEV + 1 :] * rng.uniform(
            0.5, 2.0, N_SLOTS - S_DEV - 1
        )
    after = R.raw_signal_mask(p, 1, 16, 3.0)[0]
    assert after[: S_DEV + 1].tolist() == before[: S_DEV + 1].tolist()


def test_cooldown_keeps_first_signal_of_a_burst():
    slots = np.array([100, 105, 116, 117, 140])
    assert R.apply_cooldown(slots, 16).tolist() == [100, 117, 140]


def test_partitions():
    assert R.in_partition("KRW-AAA", "A_breakout")
    assert not R.in_partition("KRW-BTC", "A_breakout")
    assert R.in_partition("KRW-BTC", "B_held_rebound")
    assert not R.in_partition("KRW-AAA", "B_held_rebound")
    assert R.in_partition("KRW-BTC", "C_downside")
    assert not R.in_partition("KRW-USDT", "C_downside")


def test_gross_entry_next_open_delay_and_exit_forward_fill():
    p = make_panel(["KRW-AAA"])
    s = S_DEV
    p.o[0, s + 1] = 110.0  # entry open at the next bar
    p.o[0, s + 2] = 120.0
    p.c[0, s + 4] = 121.0  # exit close for h=4, delay 0 (slot s+4)
    ctx = R.build_context(p)
    assert ctx.gross(4, 0)[0, s] == pytest.approx(121.0 / 110.0 - 1)
    # delay 1: enter at the open of s+2, exit at the close of slot s+1+4 = s+5
    p.c[0, s + 5] = 130.0
    ctx = R.build_context(p)
    assert ctx.gross(4, 1)[0, s] == pytest.approx(130.0 / 120.0 - 1)
    # exit slot without a candle keeps the last traded close
    for arr in (p.o, p.h, p.l, p.c, p.v, p.q):
        arr[0, s + 4] = np.nan
    p.c[0, s + 3] = 117.0
    ctx = R.build_context(p)
    assert ctx.gross(4, 0)[0, s] == pytest.approx(117.0 / 110.0 - 1)
    # missing entry candle -> NaN, never zero
    for arr in (p.o, p.h, p.l, p.c, p.v, p.q):
        arr[0, s + 1] = np.nan
    ctx = R.build_context(p)
    assert np.isnan(ctx.gross(4, 0)[0, s])
    # beyond the end of data -> NaN
    assert np.isnan(ctx.gross(96, 0)[0, N_SLOTS - 10])


def test_net_uses_direction_and_bucket_cost():
    p = make_panel(["KRW-AAA"], q=10e6)  # 96 * 10M = 0.96B -> L1
    s = S_DEV
    p.o[0, s + 1] = 100.0
    p.c[0, s + 4] = 103.0
    ctx = R.build_context(p)
    assert ctx.bucket[0, s] == 0
    assert ctx.net(4, 0, 1, "base")[0, s] == pytest.approx(0.03 - 0.0050)
    # short-equivalent: price falls 3% -> +3% avoided loss, same round-trip cost
    p.c[0, s + 4] = 97.0
    ctx = R.build_context(p)
    assert ctx.net(4, 0, -1, "base")[0, s] == pytest.approx(0.03 - 0.0050)
    assert ctx.net(4, 0, -1, "fee_only")[0, s] == pytest.approx(0.03 - 0.0010)
    big = make_panel(["KRW-BBB"], q=1e9)  # 96B/day -> L3
    assert R.build_context(big).bucket[0, S_DEV] == 2


def test_adverse_excursion_long_and_short():
    p = make_panel(["KRW-AAA"])
    s = S_DEV
    p.o[0, s + 1] = 100.0
    p.l[0, s + 2] = 96.0
    p.h[0, s + 3] = 108.0
    mae_long = R.adverse_excursion(p, np.array([0]), np.array([s]), 4, 0, 1)
    mae_short = R.adverse_excursion(p, np.array([0]), np.array([s]), 4, 0, -1)
    assert mae_long[0] == pytest.approx(0.04)
    assert mae_short[0] == pytest.approx(0.08)


def _controls_panel() -> tuple[R.Panel, int]:
    names = ["KRW-AAA", "KRW-BBB", "KRW-CCC", "KRW-DDD", "KRW-EEE", "KRW-FFF"]
    p = make_panel(names)
    spike(p, 0, S_DEV, close=105.0, q=4 * 30e6)  # AAA signals
    spike(p, 1, S_DEV, close=106.0, q=4 * 30e6)  # BBB also signals: not a control
    return p, S_DEV


def test_signal_set_controls_exclude_signalers_and_the_signal_market():
    p, s = _controls_panel()
    ctx = R.build_context(p)
    cfg = R.Config("A_breakout", 16, 3.0)
    sset = R.make_signal_set(ctx, cfg)
    assert sset.s0.tolist() == [s, s]
    assert sorted(sset.mi.tolist()) == [0, 1]
    for k in range(2):
        picked = set(sset.b_mi[sset.b_gid == k].tolist())
        assert picked <= {2, 3, 4, 5}  # neither signaler is a control
        assert picked == {2, 3, 4, 5}  # pool of 4 < K=10 -> all
        # control A: same market, same KST hour, quiet period, same half
        slots = sset.a_s[sset.a_gid == k]
        assert len(slots) > 0
        assert all(ctx.halves[x] == "dev" for x in slots)
        assert all(ctx.hours[x] == ctx.hours[s] for x in slots)
        assert all(abs(int(x) - s) > R.COOLDOWN_BARS for x in slots)


def test_signal_set_is_deterministic_and_drops_missing_entry_bar():
    p, s = _controls_panel()
    for arr in (p.o, p.h, p.l, p.c, p.v, p.q):
        arr[1, s + 1] = np.nan  # BBB has no candle at the next open
    ctx = R.build_context(p)
    cfg = R.Config("A_breakout", 16, 3.0)
    a = R.make_signal_set(ctx, cfg)
    b = R.make_signal_set(R.build_context(p), cfg)
    assert a.mi.tolist() == [0] and a.entry_missing["dev"] == 1
    assert a.b_mi.tolist() == b.b_mi.tolist() and a.a_s.tolist() == b.a_s.tolist()


def test_thin_control_pool_gives_no_control_value():
    p = make_panel(["KRW-AAA", "KRW-BBB"])
    spike(p, 0, S_DEV, close=105.0, q=4 * 30e6)
    ctx = R.build_context(p)
    sset = R.make_signal_set(ctx, R.Config("A_breakout", 16, 3.0))
    assert sset.thin_b == 1 and len(sset.b_gid) == 0
    net = ctx.net(16, 0, 1, "base")
    assert np.isnan(R.control_means(net, sset.b_gid, sset.b_mi, sset.b_s, 1)[0])


def test_halves_and_days():
    p = make_panel(["KRW-AAA"])
    halves = R.half_of_entry(p)
    first_dev = np.flatnonzero(halves == "dev")[0]
    assert p.entry_time_epoch(first_dev) == R.epoch(R.DEV_START)
    last_dev = np.flatnonzero(halves == "dev")[-1]
    assert p.entry_time_epoch(last_dev) == R.epoch(R.DEV_END_EXCLUSIVE) - R.BAR_SECONDS
    first_test = np.flatnonzero(halves == "test")[0]
    assert p.entry_time_epoch(first_test) == R.epoch(R.TEST_START)
    last_test = np.flatnonzero(halves == "test")[-1]
    assert p.entry_time_epoch(last_test) == R.epoch(R.TEST_END_INCLUSIVE)
    # 5-day gap between the halves, and the last test signal still has 24 h of data
    gap_slots = (
        (halves == "") & (np.arange(p.n) > last_dev) & (np.arange(p.n) < first_test)
    ).sum()
    assert gap_slots == 5 * R.SLOTS_PER_DAY
    assert p.n - 1 - last_test >= R.HORIZONS[-1] - 1
    assert (
        R.kst_day(p, np.array([first_dev]))[0]
        == (R.epoch(R.DEV_START) + 9 * 3600) // 86400
    )


def test_bootstrap_is_deterministic_and_clusters_by_day():
    vals = np.array([1.0, 1.0, -1.0, 2.0, 0.5, np.nan])
    days = np.array([1, 1, 2, 3, 3, 3])
    a = E.cluster_bootstrap_ci(vals, days, 0.95, 7)
    b = E.cluster_bootstrap_ci(vals, days, 0.95, 7)
    assert a == b and a[0] <= a[1]
    one_day = E.cluster_bootstrap_ci(np.array([1.0, 3.0]), np.array([5, 5]), 0.95, 7)
    assert one_day == (2.0, 2.0)  # one cluster: nothing to resample


def _rec(config: str, n: int, x: float, nb: int, ex: float) -> dict:
    return {
        "half": "dev",
        "family": "A_breakout",
        "config": config,
        "n": n,
        "x": x,
        "h": R.PRIMARY_H,
        "delay": 0,
        "scenario": "base",
        "bucket": None,
        "n_b": nb,
        "excess_b_mean": ex,
    }


def test_selection_rule_threshold_and_tie_break():
    recs = [
        _rec("c1", 16, 3.0, 40, 0.010),
        _rec("c2", 16, 5.0, 29, 0.050),  # below the minimum n: ineligible
        _rec("c3", 32, 3.0, 40, 0.010),  # ties c1 on excess: larger N wins
        _rec("c4", 32, 5.0, 40, 0.004),
    ]
    assert E.select_config(recs, "A_breakout")["config"] == "c3"
    assert E.select_config(recs[1:2], "A_breakout") is None
    assert E.select_config(recs, "C_downside") is None


def test_verdict_rule():
    good = {
        "n_used": 60,
        "n_days": 20,
        "excess_b_lo_bonferroni": 0.001,
        "excess_a_mean": 0.002,
        "mean_net": 0.004,
    }
    assert E.verdict(good)["verdict"] == "SUPPORTED"
    assert (
        E.verdict({**good, "excess_b_lo_bonferroni": -0.001})["verdict"]
        == "NOT_SUPPORTED"
    )
    assert E.verdict({**good, "excess_a_mean": -0.001})["verdict"] == "NOT_SUPPORTED"
    assert E.verdict({**good, "mean_net": -0.001})["verdict"] == "NOT_SUPPORTED"
    assert E.verdict({**good, "n_used": 29})["verdict"] == "INSUFFICIENT_SAMPLE"
    assert E.verdict({**good, "n_days": 9})["verdict"] == "INSUFFICIENT_SAMPLE"
    assert E.verdict(None)["verdict"] == "INSUFFICIENT_SAMPLE"


def test_end_to_end_summary_on_planted_edge():
    """A planted breakout that rallies beats control; a planted failure does not."""
    names = [f"KRW-M{i:02d}" for i in range(14)]
    p = make_panel(names)
    days_slots = [S_DEV + 96 * d for d in range(0, 15)]
    for d, s in enumerate(days_slots):
        spike(p, d % 5, s, close=105.0, q=4 * 30e6)
        p.o[d % 5, s + 1] = 105.0
        p.c[d % 5, s + 16] = 110.0  # +4.8% over 4 h
    ctx = R.build_context(p)
    cfg = R.Config("A_breakout", 16, 3.0)
    sset = R.make_signal_set(ctx, cfg)
    rec = E.summarize(ctx, sset, "dev", 16, 0, "base")
    assert rec["n_used"] == 15 and rec["n_days"] == 15
    assert rec["mean_net"] == pytest.approx(
        110.0 / 105.0 - 1 - 0.0050, rel=1e-6
    )  # bucket L1 cost
    assert rec["excess_b_mean"] > 0.04 and rec["excess_b_lo_bonferroni"] > 0
    assert rec["hit_rate"] == 1.0
