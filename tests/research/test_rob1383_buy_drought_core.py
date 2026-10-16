"""ROB-1383 v1 -- core judgement functions (DB-free, network-free).

Fixtures copy the real *shapes*: bar frames built through ``prices`` session
bounds, decision clocks at the session times the cohort uses (UTC), and reason
strings that keep the exact phrase grammar of the cohort's ``contrary_evidence``
field with synthetic prices and no symbols (the repository is public).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from research.rob1383_buy_drought.core import (
    VARIANT_NAMES,
    SupportLevel,
    build_variant,
    classify_deep_limit_out,
    decision_index,
    evaluate_support_gate,
    excess_hit,
    expected_bench_return_pct,
    fill_legs,
    first_touch,
    forward_positions,
    forward_return,
    leg_filled,
    legs_return,
    level_failures,
    low_after,
    parse_reason_rsi,
    point_in_time,
    recomputed_support_failures,
    tick_floor,
    tick_size,
)
from research.rob1383_buy_drought.gates import (
    EpisodeInput,
    assign_episodes,
    compose,
    parse_text_gates,
    text_states_support_pass,
    union_gates,
)
from research.rob1383_buy_drought.prices import finalise_hourly, session_bounds
from research.rob1383_buy_drought.q2_q3_orders import expiry_bucket
from research.rob1383_buy_drought.stats import (
    cluster_bootstrap_ci,
    proportion_row,
    wilson_ci,
)


def _bars(
    market: str, days: list[date], lows: list[float] | None = None
) -> pd.DataFrame:
    n = len(days)
    lows = lows or [100.0] * n
    rows = []
    for d, low in zip(days, lows, strict=True):
        o, c = session_bounds(market, d)
        rows.append(
            {
                "session_date": d,
                "open": low + 3,
                "high": low + 6,
                "low": low,
                "close": low + 4,
                "volume": 1_000.0,
                "open_ts": pd.Timestamp(o),
                "close_ts": pd.Timestamp(c),
            }
        )
    return pd.DataFrame(rows)


# KRX sessions around the 2026-10 holidays are irrelevant here: weekdays only.
KR_DAYS = [date(2026, 10, 12) + timedelta(days=i) for i in range(5)]  # Mon..Fri
US_DAYS = KR_DAYS


# ------------------------------------------------------------------ windows


def test_kr_intraday_decision_uses_previous_session_close():
    bars = _bars("kr", KR_DAYS)
    # An order at 11:37 KST == 02:37Z, mid-session Fri.
    at = datetime(2026, 10, 16, 2, 37, tzinfo=UTC)
    assert bars["session_date"].iloc[decision_index(bars, at)] == date(2026, 10, 15)
    # The in-progress Friday bar is the first (and DAY-order) forward bar.
    assert forward_positions(bars, at, 1) == [4]


def test_kr_decision_exactly_at_close_counts_that_bar_as_known():
    bars = _bars("kr", KR_DAYS)
    at = datetime(2026, 10, 14, 6, 30, tzinfo=UTC)  # 15:30 KST
    assert bars["session_date"].iloc[decision_index(bars, at)] == date(2026, 10, 14)
    assert forward_positions(bars, at, 2) == [3, 4]


def test_us_open_trade_decision_before_close_is_previous_et_session():
    bars = _bars("us", US_DAYS)
    # A US open-trade decision at 13:44:16Z == 09:44 ET (22:44 KST).
    at = datetime(2026, 10, 16, 13, 44, 16, tzinfo=UTC)
    assert bars["session_date"].iloc[decision_index(bars, at)] == date(2026, 10, 15)


def test_crypto_morning_decision_before_0900_kst_roll():
    days = [date(2026, 10, 7) + timedelta(days=i) for i in range(5)]
    bars = _bars("crypto", days)
    # A crypto decision at 23:25:39Z == 08:25 KST next day: the 10-09
    # Upbit day (09:00 10-09 .. 09:00 10-10 KST) is still in progress.
    at = datetime(2026, 10, 9, 23, 25, 39, tzinfo=UTC)
    assert bars["session_date"].iloc[decision_index(bars, at)] == date(2026, 10, 8)
    assert bars["session_date"].iloc[forward_positions(bars, at, 1)[0]] == date(
        2026, 10, 9
    )


def test_forward_positions_rejects_zero():
    with pytest.raises(ValueError):
        forward_positions(_bars("kr", KR_DAYS), datetime(2026, 10, 12, tzinfo=UTC), 0)


# -------------------------------------------------------------------- touch


def test_touch_is_inclusive_at_the_limit():
    assert first_touch([101.0, 100.0, 99.0], 100.0) == 1


def test_touch_one_tick_above_is_not_a_touch():
    assert first_touch([100.01, 100.5], 100.0) is None


def test_touch_window_one_vs_five_off_by_one():
    lows = [101.0, 99.5, 98.0, 97.0, 96.0, 95.0]
    anchor = 99.9
    assert first_touch(lows[:1], anchor) is None  # N = 1: DAY order only
    assert first_touch(lows[:5], anchor) == 1  # N = 5
    assert first_touch(lows[:5], 95.0) is None  # 6th bar is outside N = 5


def test_touch_ignores_nan_lows():
    assert first_touch([math.nan, 99.0], 100.0) == 1


def test_forward_return_counts_from_touch_bar_and_censors():
    closes = [100, 101, 102, 103, 104, 105, 106]
    # day0 = touch bar (index 1); +5 bars -> index 6 close 106.
    assert forward_return(closes, 1, 5, 100.0) == pytest.approx(6.0)
    assert forward_return(closes, 2, 5, 100.0) is None  # index 7 missing
    with pytest.raises(ValueError):
        forward_return(closes, 0, 1, 0.0)


# --------------------------------------------------------------- order legs


def test_touch_vs_trade_through_at_the_limit_kr():
    # 57,700 KRW limit: KRX tick is 100 there.
    assert leg_filled("kr", 57_700.0, 57_700.0, "touch") is True
    assert leg_filled("kr", 57_700.0, 57_700.0, "trade_through") is False
    assert (
        leg_filled("kr", 57_650.0, 57_700.0, "trade_through") is False
    )  # off-grid, < 1 tick
    assert leg_filled("kr", 57_600.0, 57_700.0, "trade_through") is True
    assert leg_filled("kr", None, 57_700.0, "touch") is False


def test_trade_through_us_one_cent():
    assert leg_filled("us", 66.38, 66.38, "trade_through") is False
    assert leg_filled("us", 66.37, 66.38, "trade_through") is True
    with pytest.raises(ValueError):
        leg_filled("us", 66.0, 66.38, "queue")


def test_tick_size_per_market():
    assert tick_size("kr", 57_700) == 100
    assert tick_size("kr", 345_000) == 500
    assert tick_size("us", 336.0) == 0.01
    assert tick_size("crypto", 68.7) == 0.1


def test_fill_legs_20_80_split():
    legs = [(99.5, 0.2), (95.0, 0.8)]
    near_only = fill_legs("us", 97.0, legs, "touch")
    assert near_only.filled == (True, False)
    assert near_only.filled_weight == pytest.approx(0.2)
    both = fill_legs("us", 95.0, legs, "touch")
    assert both.filled_weight == pytest.approx(1.0)
    assert fill_legs("us", 95.0, legs, "trade_through").filled == (True, False)
    with pytest.raises(ValueError):
        fill_legs("us", 95.0, [(99.5, 0.2), (95.0, 0.7)], "touch")


def test_legs_return_is_notional_weighted():
    legs = [(100.0, 0.2), (95.0, 0.8)]
    # close 105: +5% on 0.2, +10.526% on 0.8.
    assert legs_return(105.0, legs, (True, True)) == pytest.approx(
        0.2 * 5 + 0.8 * 10.526315789
    )
    assert legs_return(105.0, legs, (True, False)) == pytest.approx(5.0)
    assert legs_return(105.0, legs, (False, False)) is None


def test_expected_bench_return_uses_lognormal_mean():
    r = [0.01, -0.01, 0.02, 0.0]
    mu = sum(r) / 4
    var = sum((x - mu) ** 2 for x in r) / 3
    assert expected_bench_return_pct(r, 5) == pytest.approx(
        (math.exp((mu + var / 2) * 5) - 1) * 100
    )
    assert expected_bench_return_pct([0.0, 0.0], 10) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        expected_bench_return_pct([0.01], 3)


def test_expiry_buckets():
    assert expiry_bucket("kr", "15:34") == "kr_1530_regular_close"
    assert expiry_bucket("kr", "15:33") == "kr_1530_regular_close"
    assert expiry_bucket("kr", "20:03") == "kr_2000_nxt_close"
    assert expiry_bucket("us", "08:50") == "us_0850_kst"
    assert expiry_bucket("us", None) is None


# ---------------------------------------------------------------- excess hit


def _hit(**kw):
    base = {
        "direction": "at_or_above",
        "base": 100.0,
        "target": 105.0,
        "highs": [103.0, 106.0],
        "lows": [99.0, 101.0],
        "closes": [102.0, 104.0],
        "bench_base": 1000.0,
        "mode": "touch",
    }
    base.update(kw)
    return excess_hit(**base)


def test_flat_benchmark_reduces_to_original_touch_rule():
    assert _hit(bench_closes=[1000.0, 1000.0]) is True
    assert _hit(highs=[103.0, 104.99], bench_closes=[1000.0, 1000.0]) is False


def test_exact_threshold_is_a_hit():
    assert _hit(highs=[103.0, 105.0], bench_closes=[1000.0, 1000.0]) is True


def test_benchmark_rally_removes_the_hit():
    # day 2: stock +6% vs bench +2% -> excess 4% < required 5%.
    assert _hit(bench_closes=[1000.0, 1020.0]) is False
    # day 2 bench -1% -> excess 7% >= 5%.
    assert _hit(bench_closes=[1000.0, 990.0]) is True


def test_at_or_below_uses_lows_and_le():
    kw = {"direction": "at_or_below", "target": 97.0, "lows": [99.0, 97.5]}
    assert _hit(**kw, bench_closes=[1000.0, 1000.0]) is False
    # bench +1% makes the -2.5% low an excess of -3.5% <= -3%.
    assert _hit(**kw, bench_closes=[1000.0, 1010.0]) is True


def test_end_mode_uses_last_close_only():
    # last close 104 vs bench flat -> +4% < 5%, although high touched 106.
    assert _hit(mode="end", bench_closes=[1000.0, 1000.0]) is False
    assert _hit(mode="end", bench_closes=[1000.0, 985.0]) is True


def test_excess_hit_rejects_misaligned_window():
    with pytest.raises(ValueError):
        _hit(bench_closes=[1000.0])
    with pytest.raises(ValueError):
        _hit(direction="up", bench_closes=[1000.0, 1000.0])


# ------------------------------------------------------------- Wilson / CIs


def test_wilson_reference_values():
    lo, hi = wilson_ci(5, 10)
    assert lo == pytest.approx(0.23659, abs=1e-4)
    assert hi == pytest.approx(0.76341, abs=1e-4)
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.27753, abs=1e-4)
    lo, hi = wilson_ci(10, 10)
    assert hi == pytest.approx(1.0) and lo == pytest.approx(0.72247, abs=1e-4)


def test_wilson_empty_cell_is_nan_not_zero():
    lo, hi = wilson_ci(0, 0)
    assert math.isnan(lo) and math.isnan(hi)
    with pytest.raises(ValueError):
        wilson_ci(3, 2)


def test_cluster_bootstrap_keeps_symbols_together_and_is_deterministic():
    values = [1.0] * 9 + [0.0]
    clusters = ["A"] * 9 + ["B"]
    first = cluster_bootstrap_ci(values, clusters)
    assert first == cluster_bootstrap_ci(values, clusters)
    # Resampling 2 clusters can only yield pooled means {0, 0.9, 1}: the
    # interval must reach 0, which an observation bootstrap never would.
    assert first[0] == pytest.approx(0.0)
    assert first[1] == pytest.approx(1.0)
    assert cluster_bootstrap_ci([1.0, 0.0], ["A", "A"]) == (0.5, 0.5)


def test_proportion_row_counts():
    row = proportion_row([True, False, True, True], ["A", "A", "B", "C"])
    assert (row["n"], row["k"], row["n_clusters"]) == (4, 3, 3)
    assert row["rate"] == pytest.approx(0.75)


# ------------------------------------------------------------- gate / ticks


def test_tick_floor_per_market():
    assert tick_floor("kr", 327272) == 327000
    assert tick_floor("kr", 15723) == 15720
    assert tick_floor("us", 12.349) == pytest.approx(12.34)
    assert tick_floor("us", 336.0) == pytest.approx(336.0)
    assert tick_floor("crypto", 68.77) == pytest.approx(68.7)
    assert tick_floor("crypto", 3_366_233) == pytest.approx(3_366_000)


def _lvl(price, strength, families):
    return SupportLevel(price, strength, families, ("fib_0", "bb_lower")[:families])


def test_near_moderate_support_fails_baseline_passes_v4():
    # moderate 2-family support -2.31% below the live price.
    supports = [_lvl(764.14, "moderate", 2), _lvl(698.99, "weak", 1)]
    base = evaluate_support_gate("us", supports, 783.31, build_variant("baseline"))
    v4 = evaluate_support_gate("us", supports, 783.31, build_variant("V4_deep_upper_0"))
    assert base.passed is False
    assert v4.passed is True and v4.anchor == pytest.approx(764.14)


def test_deep_band_edges():
    # anchor exactly -3.00% passes baseline (inclusive), -12% passes, -12.01% fails
    assert evaluate_support_gate(
        "us", [_lvl(97.0, "moderate", 2)], 100.0, build_variant("V3_within_12")
    ).passed
    assert evaluate_support_gate(
        "us", [_lvl(88.0, "moderate", 2)], 100.0, build_variant("V3_within_12")
    ).passed
    assert not evaluate_support_gate(
        "us", [_lvl(87.99, "moderate", 2)], 100.0, build_variant("V3_within_12")
    ).passed


def test_within_8_vs_v3():
    supports = [_lvl(90.0, "moderate", 2)]  # -10%
    assert not evaluate_support_gate(
        "us", supports, 100.0, build_variant("baseline")
    ).passed
    assert evaluate_support_gate(
        "us", supports, 100.0, build_variant("V3_within_12")
    ).passed


def test_weak_single_family_needs_v1_and_v2_together():
    supports = [_lvl(95.0, "weak", 1)]  # weak == 1 source => <= 1 family
    assert not evaluate_support_gate(
        "us", supports, 100.0, build_variant("V2_strength_weak")
    ).passed
    assert not evaluate_support_gate(
        "us", supports, 100.0, build_variant("V1_families_1")
    ).passed
    assert evaluate_support_gate(
        "us", supports, 100.0, build_variant("V1+V2+V3")
    ).passed


def test_strongest_then_nearest_selection():
    supports = [
        _lvl(96.0, "moderate", 2),
        SupportLevel(94.0, "strong", 3, ("a", "b", "c")),
        _lvl(95.0, "moderate", 2),
    ]
    got = evaluate_support_gate("us", supports, 100.0, build_variant("baseline"))
    assert got.level.price == 94.0
    supports = [_lvl(95.0, "moderate", 2), _lvl(96.0, "moderate", 2)]
    assert (
        evaluate_support_gate(
            "us", supports, 100.0, build_variant("baseline")
        ).level.price
        == 96.0
    )


def test_variant_names_closed():
    assert len(VARIANT_NAMES) == 7
    with pytest.raises(ValueError):
        build_variant("V5")


# ------------------------------------------------------------ reason text

EN_OUTSIDE = (
    "Rejected: only support 512.30 moderate [bb_lower, fib_78.6] sits −2.31% from live 524.41, "
    "outside buy.deep_limit_pct_range [−12,−3]; next support 468.20 is weak. RSI 42.5 and upside 46% pass."
)
KO_BAND = "기각 사유: 지지 88.20 moderate(bb_lower·fib_0)가 라이브 90.46 대비 −2.50%로 deep_limit 밴드 [−12,−3] 밖."
KO_NEAR = (
    "기각 사유: 유일 지지 201.10 moderate[fib_0, bb_lower] −0.06%로 밴드[-12,-3] 밖, 그 아래 명명 지지 없음. "
    "RSI 42.23·upside 56.3%는 통과."
)
EN_CLOSER = "no entry. Support is closer than buy.deep_limit_pct_range -3%. All resistances weak single-source."
EN_INSIDE = (
    "buy.winner_pullback_add not proposed: 3 of 4 conditions met (lot +7.37%, strong support 1,385.52 "
    "above avg 1,335.59, -3.42% inside the deep band) but thesis_reconfirmed fails"
)
KO_WEAK = "기각: 지지 41.00 weak [fib_0] 단일(−1.79%)뿐 — moderate 2패밀리 지지 없음. RSI 30.21·upside 95.4% 통과."


@pytest.mark.parametrize("reason", [EN_OUTSIDE, KO_BAND, KO_NEAR, EN_CLOSER])
def test_deep_limit_out_positive(reason):
    assert classify_deep_limit_out(reason) is True


@pytest.mark.parametrize("reason", [EN_INSIDE, KO_WEAK, "", None])
def test_deep_limit_out_negative(reason):
    assert classify_deep_limit_out(reason) is False


def test_parse_reason_rsi():
    assert parse_reason_rsi(KO_WEAK) == pytest.approx(30.21)
    assert parse_reason_rsi("RSI14 50.22 > screen.rsi_max 45") == pytest.approx(50.22)
    assert parse_reason_rsi("RSI-14 34.53 is the second-lowest") == pytest.approx(34.53)
    assert parse_reason_rsi("no oscillator mentioned") is None


# ------------------------------------------------- point-in-time / hourly


def _kr_hourly(day: date, lows: list[float], closes: list[float]) -> pd.DataFrame:
    # yfinance KR 60m shape: 6 bars 09..14 KST, the last one runs to 15:30.
    starts = [
        pd.Timestamp(datetime(day.year, day.month, day.day, h, 0), tz="Asia/Seoul")
        for h in range(9, 15)
    ]
    raw = pd.DataFrame(
        {
            "start_ts": starts,
            "open": [c + 0.5 for c in closes],
            "high": [c + 1 for c in closes],
            "low": lows,
            "close": closes,
            "volume": [10.0] * 6,
        }
    )
    return finalise_hourly("kr", raw, datetime(2026, 10, 17, tzinfo=UTC))


def test_finalise_hourly_last_kr_bar_ends_at_close():
    h = _kr_hourly(date(2026, 10, 16), [100.0] * 6, [101.0] * 6)
    assert h["end_ts"].iloc[0] == pd.Timestamp("2026-10-16T01:00:00Z")  # 10:00 KST
    assert h["end_ts"].iloc[-1] == pd.Timestamp("2026-10-16T06:30:00Z")  # 15:30 KST


def test_point_in_time_appends_partial_bar_from_completed_hours_only():
    bars = _bars("kr", KR_DAYS)
    h = _kr_hourly(KR_DAYS[4], [99, 95, 97, 90, 98, 98], [100, 96, 98, 91, 99, 99])
    at = datetime(2026, 10, 16, 2, 37, tzinfo=UTC)  # 11:37 KST: 09,10 hours done
    pit = point_in_time(bars, h, at)
    assert pit.price_basis == "intraday_partial"
    assert len(pit.history) == 5  # 4 completed days + 1 partial
    last = pit.history.iloc[-1]
    assert last["close"] == 96 and last["low"] == 95  # 12:00 low 90 not yet seen
    assert pit.session_pos == 4


def test_point_in_time_before_first_hour_falls_back_to_prior_close():
    bars = _bars("kr", KR_DAYS)
    h = _kr_hourly(KR_DAYS[4], [99] * 6, [100] * 6)
    at = datetime(2026, 10, 16, 0, 20, tzinfo=UTC)  # 09:20 KST
    pit = point_in_time(bars, h, at)
    assert pit.price_basis == "prior_close_no_hourly" and len(pit.history) == 4
    pre_open = point_in_time(bars, h, datetime(2026, 10, 15, 23, 0, tzinfo=UTC))
    assert pre_open.price_basis == "prior_close"


def test_low_after_excludes_containing_hour_unless_inclusive():
    bars = _bars("kr", KR_DAYS, lows=[100, 100, 100, 100, 88])
    h = _kr_hourly(KR_DAYS[4], [88, 95, 97, 96, 98, 98], [90, 96, 98, 97, 99, 99])
    at = datetime(2026, 10, 16, 0, 14, tzinfo=UTC)  # 09:14 KST, inside the 09:00 hour
    assert low_after(bars, h, 4, at) == (95.0, "hourly")
    assert low_after(bars, h, 4, at, inclusive=True) == (88.0, "hourly")
    # Starting before the open and running past the close -> exact daily low.
    assert low_after(bars, h, 4, datetime(2026, 10, 15, 23, 0, tzinfo=UTC)) == (
        88.0,
        "daily",
    )
    # No hourly data -> daily low, flagged.
    assert low_after(bars, None, 4, at) == (88.0, "daily_fallback")
    # Optimistic window keeps every overlapping hour, including one that
    # straddles the end (an order filled inside its first hour).
    fill_at = datetime(2026, 10, 16, 0, 40, tzinfo=UTC)  # 09:40 KST
    assert low_after(bars, h, 4, at, fill_at, inclusive=True) == (88.0, "hourly")
    assert low_after(bars, h, 4, at, fill_at) == (None, "hourly_empty")
    # Decision in the last hour -> nothing tradable left (conservative).
    late = datetime(2026, 10, 16, 5, 10, tzinfo=UTC)  # 14:10 KST
    assert low_after(bars, h, 4, late) == (None, "hourly_empty")


def test_first_touch_skips_sessions_with_no_tradable_time():
    assert first_touch([None, 99.0], 100.0) == 1


# ------------------------------------------------------- gate decomposition

KO_SHALLOW = (
    "기각: 52.40 moderate [bb_lower, VAL] −1.0%는 buy.deep_limit_pct_range [−12,−3] 밖, "
    "그 아래 50.79는 weak 단일 패밀리. RSI 43.58·upside 80.2% 통과."
)
KO_UPSIDE = (
    "기각 사유: honest upside 39.83% < screen.upside_min_pct 40 — 0.17pp 차이의 경계 탈락이다. "
    "RSI 43.58·지지 120.84 strong(3패밀리, −10.61%)은 통과했으므로 상방 게이트 단 하나로 기각했다."
)
EN_RECOVERY = (
    "Rejected: every support within screen.support_within_pct 8 is weak single-source "
    "(VAL 18.86 -1.26%, fib_78.6 18.15 -4.97%) -> fails screen.support_strength_min moderate and "
    "independent_support_source_count_min 2; recovery gate 0/2 or hold -> m=0. RSI 37.5"
)
RSI_FAIL = "기각 사유: RSI14 50.22 > screen.rsi_max 45 (upside 45.29·strong 지지 98.33 −2.03%는 통과)"


def test_level_failures_each_sub_gate():
    base = build_variant("baseline")
    assert level_failures("us", _lvl(97.69, "moderate", 2), 100.0, base) == {
        "deep_limit_band_shallow"
    }
    assert level_failures("us", _lvl(95.0, "weak", 1), 100.0, base) == {
        "support_strength",
        "independent_sources",
    }
    assert level_failures("us", _lvl(90.0, "moderate", 2), 100.0, base) == {
        "support_within_pct"
    }
    assert level_failures("us", _lvl(87.0, "moderate", 2), 100.0, base) == {
        "support_within_pct",
        "deep_limit_band_deep",
    }
    assert level_failures("us", _lvl(95.0, "moderate", 2), 100.0, base) == frozenset()


def test_recomputed_failures_pick_closest_to_passing_level():
    base = build_variant("baseline")
    supports = [_lvl(99.0, "moderate", 2), _lvl(95.0, "weak", 1)]
    fails, level = recomputed_support_failures("us", supports, 100.0, base)
    assert fails == {"deep_limit_band_shallow"} and level.price == 99.0
    fails, level = recomputed_support_failures("us", [], 100.0, base)
    assert fails == {"no_support_below"} and level is None


def test_text_gates_shallow_is_deep_limit_and_single_family():
    gates, _ = parse_text_gates(KO_SHALLOW)
    assert "deep_limit_band" in gates
    assert "independent_sources" in gates
    assert (
        "upside_min" not in gates and "rsi_max" not in gates
    )  # "통과" is not a failure


def test_text_gates_upside_only():
    gates, _ = parse_text_gates(KO_UPSIDE)
    assert gates == {"upside_min"}
    assert text_states_support_pass(KO_UPSIDE) is True


def test_text_gates_recovery_gate_is_other():
    gates, others = parse_text_gates(EN_RECOVERY)
    assert {"support_strength", "independent_sources", "other"} <= gates
    # "every support *within* 8% is weak" is a strength failure, not distance.
    assert "support_within_pct" not in gates
    assert "recovery_gate" in others


def test_text_gates_decimal_inside_clause():
    gates, _ = parse_text_gates(
        "Rejected: honest upside 77.33 = +12.56% < 40 (support passes)"
    )
    assert "upside_min" in gates


def test_text_gates_rsi_value_above_max():
    gates, _ = parse_text_gates(RSI_FAIL)
    assert "rsi_max" in gates


def test_compose_support_only_fail_bases():
    shallow = compose(KO_SHALLOW, frozenset({"deep_limit_band_shallow"}), 41.0)
    assert shallow.support_only_fail and shallow.support_only_basis == "text"
    assert shallow.rsi_source == "text" and shallow.rsi_value == pytest.approx(43.58)
    assert not compose(KO_UPSIDE, frozenset(), 40.0).support_only_fail
    assert not compose(
        EN_RECOVERY, frozenset({"support_strength"}), 37.5
    ).support_only_fail
    assert not compose(
        RSI_FAIL, frozenset({"deep_limit_band_shallow"}), 44.0
    ).support_only_fail
    silent = compose(
        "Same-day catalyst and price at day high.",
        frozenset({"support_within_pct"}),
        30.0,
    )
    # tester r1 B1: upside/data gates are unverified when the text names no
    # gate, so the row is never a primary support-only failure.
    assert not silent.support_only_fail
    assert silent.support_only_basis == "recomputed_text_silent"
    assert not compose(
        "free text", frozenset({"support_within_pct"}), 60.0
    ).support_only_fail
    assert not compose("free text", None, None).support_only_fail


def test_union_folds_deep_limit_detail():
    assert union_gates(
        frozenset({"upside_min"}), frozenset({"deep_limit_band_shallow"})
    ) == {
        "upside_min",
        "deep_limit_band",
    }
    assert union_gates(frozenset({"rsi_max"}), None) == {"rsi_max"}


def _ep(key, symbol, ts, anchor, market="us"):
    return EpisodeInput(key, market, symbol, ts, anchor)


def test_episodes_session_gap_and_anchor_tolerance():
    t0 = datetime(2026, 10, 1, 13, 40, tzinfo=UTC)
    items = [
        _ep(1, "AAA", t0, 160.0),
        _ep(2, "AAA", t0 + timedelta(days=1), 163.0),  # +1.9% anchor, 1 session
        _ep(3, "AAA", t0 + timedelta(days=2), 150.0),  # -6.25% vs first anchor -> new
        _ep(4, "AAA", t0 + timedelta(days=12), 150.5),  # gap > 5 sessions -> new
        _ep(5, "BBB", t0 + timedelta(days=1), 337.0),
    ]

    def sessions(_m, _s, a, b):
        return (b - a).days  # one session per calendar day in this fixture

    ep = assign_episodes(items, sessions)
    assert ep[1] == ep[2]
    assert ep[3] != ep[1]
    assert ep[4] != ep[3]
    assert ep[5] != ep[1]
    assert ep[1] == "us:AAA:20261001T1340Z"


def test_episodes_crypto_72h_and_missing_bars_fallback():
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    items = [
        _ep(1, "KRW-AAA", t0, 120.0, "crypto"),
        _ep(2, "KRW-AAA", t0 + timedelta(hours=72), 121.0, "crypto"),
        _ep(3, "KRW-AAA", t0 + timedelta(hours=144, minutes=1), 121.0, "crypto"),
        _ep(4, "005930", t0, None, "kr"),
        _ep(5, "005930", t0 + timedelta(days=7), None, "kr"),
    ]
    ep = assign_episodes(items, lambda *_: None)
    assert ep[1] == ep[2] and ep[3] != ep[2]
    assert (
        ep[4] == ep[5]
    )  # no bars -> <= 7 calendar days; no anchors -> anchor check skipped


# ---------------------------------------------- Q3 keeps NXT-only originals

from dataclasses import dataclass as _dataclass  # noqa: E402
from dataclasses import field as _field  # noqa: E402

from research.rob1383_buy_drought.inputs import TossOrder  # noqa: E402
from research.rob1383_buy_drought.q2_q3_orders import run as q23_run  # noqa: E402


@_dataclass
class _Store:
    bars_frame: pd.DataFrame
    missing: dict = _field(default_factory=dict)
    split_events: dict = _field(default_factory=dict)

    def bars(self, market, symbol):
        return self.bars_frame

    def hourly(self, market, symbol):
        return None


def test_q3_keeps_order_with_no_krx_regular_overlap():
    # tester r1 B3: placed 16:09 KST, expired 20:04 KST (NXT only) -> Q2 has
    # no lifetime prices, but the next KRX sessions are observable for Q3.
    days = [date(2026, 10, 12) + timedelta(days=i) for i in range(5)]
    bars = _bars("kr", days, lows=[100.0, 100.0, 100.0, 86.0, 100.0])
    order = TossOrder(
        order_id="x",
        market="kr",
        symbol="000000",
        side="BUY",
        status="REJECTED",
        order_type="LIMIT",
        price=87_000.0,
        quantity=1.0,
        ordered_at=datetime(2026, 10, 13, 7, 9, tzinfo=UTC),  # 16:09 KST
        canceled_at=datetime(2026, 10, 13, 11, 4, tzinfo=UTC),  # 20:04 KST
        filled_qty=0.0,
        avg_fill=None,
        filled_at=None,
    )
    # Scale prices into the order's range: lows 100 -> 100,000 etc.
    bars[["open", "high", "low", "close"]] *= 1000.0
    out = q23_run([order], _Store(bars)).iloc[0]
    assert out["missing"] is None
    assert out["q2_missing"] == "no_regular_session_overlap"
    assert out["q3__optimistic__status"] == "filled"
    assert out["q3__optimistic__sessions_to_fill"] == 2  # 10-14 no, 10-15 low 86,000


def test_upside_pass_then_other_gate_failure_is_not_upside():
    # tester r1 B2: "upside 46.98 통과 ... 거리만 미달" is an upside pass.
    text = "RSI 39.73·upside 46.98 통과, strong 3패밀리 — 거리만 미달."
    gates, _ = parse_text_gates(text)
    assert "upside_min" not in gates
    assert "support_distance" in gates
    v = compose(text, None, None)
    assert v.support_only_fail and v.support_only_basis == "text"
    # A real upside failure after another gate's pass still counts.
    gates, _ = parse_text_gates("RSI 41.0 통과, upside 31.2% < 40 미달")
    assert "upside_min" in gates


def test_excess_hit_exact_threshold_with_non_flat_benchmark():
    # tester r1: +20% stock - +10% bench == +10% required -> hit.
    assert excess_hit(
        direction="at_or_above",
        base=100.0,
        target=110.0,
        highs=[120.0],
        lows=[99.0],
        closes=[119.0],
        bench_base=100.0,
        bench_closes=[110.0],
        mode="touch",
    )
