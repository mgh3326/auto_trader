"""Core arithmetic of the ROB-1458 evidence pack, on hand-built inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from research.strategy_evidence.common import INSUFFICIENT, retire_verdict, summarize
from research.strategy_evidence.losscut import (
    evaluate,
    first_touches,
    overlay_trades,
)
from research.strategy_evidence.lots import all_paths, state_before, symbol_path
from research.strategy_evidence.overlay import apply_overlay, max_drawdown
from research.strategy_evidence.parking import breakeven_days, distributions
from research.strategy_evidence.rulesets import Variant, portfolio, simulate
from research.trade_retro.prices import Bars, PriceBook
from tests.research.trade_retro.retro_fixtures import weekdays

pytestmark = pytest.mark.unit

CUTOVER = datetime(2026, 5, 10, tzinfo=UTC)
DAYS = weekdays(date(2026, 6, 1), 40)


def _stamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def _fills(*items, symbol: str = "AAA") -> pd.DataFrame:
    """Each item is (day, side, qty, price[, source])."""
    rows = []
    for number, (day, side, qty, price, *rest) in enumerate(items):
        source = rest[0] if rest else "reconciler"
        seeded = source in ("manual_import", "implausible_price")
        rows.append(
            {
                "alias": "acct",
                "market": "us",
                "symbol": symbol,
                "date": day,
                "ts": CUTOVER if source == "manual_import" else _stamp(day),
                "side": side,
                "qty": float(qty),
                "price": float(price),
                "seed": seeded,
                "source": source,
                "key": None if seeded else f"k{number}",
            }
        )
    return pd.DataFrame(rows)


def _bars(closes, opens=None) -> Bars:
    opens = opens if opens is not None else closes
    return Bars.from_rows(
        [
            (DAYS[i], float(o), float(c))
            for i, (o, c) in enumerate(zip(opens, closes, strict=True))
        ]
    )


def _book(bars: Bars, market: str = "us", symbol: str = "AAA") -> PriceBook:
    return PriceBook(
        bars={(market, symbol): bars}, benchmarks={market: bars}, missing={}
    )


# ------------------------------------------------------------------ lots
def test_average_cost_moves_on_buys_and_holds_on_sells():
    fills = _fills(
        (DAYS[0], "buy", 10, 100),
        (DAYS[1], "buy", 10, 80),
        (DAYS[2], "sell", 5, 90),
    )
    path = symbol_path(fills, _bars([100, 80, 90, 72]), CUTOVER)
    assert path["avg"].tolist() == [100.0, 90.0, 90.0, 90.0]
    assert path["qty"].tolist() == [10.0, 20.0, 15.0, 15.0]
    assert path["unrealized"].iloc[-1] == pytest.approx(72 / 90 - 1)


def test_seed_resets_quantity_and_cost_to_the_broker_figures():
    fills = _fills(
        (date(2026, 5, 9), "buy", 7, 120, "manual_import"),
        (DAYS[0], "buy", 3, 60),
    )
    state = state_before(fills, CUTOVER, _stamp(DAYS[1]))
    assert state.qty == 10
    assert state.avg == pytest.approx((7 * 120 + 3 * 60) / 10)
    assert state.known


def test_a_sell_of_unseen_shares_makes_the_cost_unknown_until_they_are_gone():
    fills = _fills(
        (DAYS[0], "buy", 10, 100),
        (DAYS[1], "sell", 14, 110),  # 4 shares older than the ledger
        (DAYS[2], "buy", 5, 90),
    )
    path = symbol_path(fills, _bars([100, 110, 90]), CUTOVER)
    # While the 4 older shares are held the cost is unknown. The sell takes them
    # first and then the 10 known ones, which leaves the position flat, and the
    # next buy starts a new episode at a known cost.
    assert path["known"].tolist() == [False, False, True]
    assert path["qty"].tolist() == [10.0, 0.0, 5.0]
    assert path["episode"].iloc[2] == path["episode"].iloc[0] + 1


def test_episode_counts_returns_to_flat():
    fills = _fills(
        (DAYS[0], "buy", 1, 100),
        (DAYS[1], "sell", 1, 100),
        (DAYS[2], "buy", 1, 50),
    )
    path = symbol_path(fills, _bars([100, 100, 50]), CUTOVER)
    assert path["episode"].tolist() == [1, 1, 2]
    assert path["avg"].iloc[2] == 50


# ------------------------------------------------------------------ rule
def test_retire_rule_boundaries_are_the_pre_registered_ones():
    assert retire_verdict(summarize([0.01] * 4)) == INSUFFICIENT
    assert retire_verdict(summarize([0.0, 0.0, 0.0, 0.0, 0.0])) == "PASS (keep)"
    # Lower quartile exactly -8% fails: the rule is strictly greater than -8%.
    assert retire_verdict(summarize([-0.08] * 3 + [0.2] * 3)) == "FAIL (retire)"
    assert retire_verdict(summarize([-0.01] * 5)) == "FAIL (retire)"
    assert summarize([None, float("nan"), 0.1]).n == 1


# ------------------------------------------------------------------ cut
def test_first_touch_is_the_first_close_at_or_below_the_threshold():
    fills = _fills((DAYS[0], "buy", 10, 100))
    bars = _bars([100, 95, 92, 80, 70, 90])
    paths = all_paths(fills, _book(bars), CUTOVER)
    [touch] = first_touches(paths, 0.08)
    assert (touch.day, touch.price, touch.qty, touch.crossed) == (
        DAYS[2],
        92.0,
        10.0,
        True,
    )
    [deep] = first_touches(paths, 0.30)
    assert deep.day == DAYS[4]


def test_already_below_at_the_first_observed_day_is_not_a_crossing():
    seed = _fills((date(2026, 5, 9), "buy", 10, 200, "manual_import"))
    bars = _bars([100, 90])
    paths = all_paths(seed, _book(bars), CUTOVER)
    [touch] = first_touches(paths, 0.08)
    assert not touch.crossed


def test_actual_outcome_takes_later_sells_first_and_marks_the_rest():
    fills = _fills((DAYS[0], "buy", 10, 100), (DAYS[3], "sell", 4, 85))
    bars = _bars([100, 95, 90, 85, 80, 75])
    paths = all_paths(fills, _book(bars), CUTOVER)
    [touch] = first_touches(paths, 0.08)  # close 90 on DAYS[2]
    sells = fills[fills["side"] == "sell"]
    result = evaluate(touch, bars, sells, None)
    value = 4 * 85 + 6 * 75
    assert result["actual"] == pytest.approx(value / (10 * 90) - 1)
    assert result["hold"] == pytest.approx(75 / 90 - 1)
    assert result["cut_minus_actual"] == pytest.approx(-0.0045 - result["actual"])
    assert result["sold_share"] == pytest.approx(0.4)
    assert evaluate(touch, bars, sells, 20) is None  # censored, never zero


def test_cut_overlay_sells_at_the_touch_and_undoes_the_later_sell():
    fills = _fills((DAYS[0], "buy", 10, 100), (DAYS[3], "sell", 4, 85))
    bars = _bars([100, 95, 90, 85, 80, 75])
    paths = all_paths(fills, _book(bars), CUTOVER)
    trades = overlay_trades(paths, fills, _book(bars), 0.08)
    assert trades[["day", "qty", "price"]].values.tolist() == [
        [DAYS[2], -10.0, 90.0],
        [DAYS[3], 4.0, 85.0],
    ]


# ------------------------------------------------------------------ overlay
def test_overlay_cash_extra_cash_and_drawdown():
    bars = _bars([10, 10, 5, 5])
    frame = pd.DataFrame(
        {
            "day": DAYS[:4],
            "cash": [100.0] * 4,
            "risk": [0.0] * 4,
            "parking": [0.0] * 4,
            "nav": [100.0] * 4,
            "index": [1.0] * 4,
        }
    )
    trades = pd.DataFrame(
        [
            {
                "alias": "a",
                "market": "us",
                "symbol": "AAA",
                "day": DAYS[1],
                "qty": 15.0,
                "price": 10.0,
            }
        ]
    )
    out = apply_overlay(frame, trades, _book(bars), "us")
    # Buying 150 with 100 of cash needs 50 more from day one.
    assert out["extra_cash_pct_avg_nav"] == pytest.approx(0.5)
    # NAV with the extra cash: 150, 150, 75, 75.
    assert out["max_drawdown"] == pytest.approx(-0.5)
    assert out["pnl_change_pct_avg_nav"] == pytest.approx(-0.75)
    assert max_drawdown(np.array([1.0, 2.0, 1.0, 3.0])) == pytest.approx(-0.5)


# ------------------------------------------------------------------ parking
def test_distributions_count_ex_dates_inside_the_hold_and_breakeven():
    dividends = {("us", "SGOV"): [(DAYS[0], 0.3), (DAYS[5], 0.3), (DAYS[9], 0.3)]}
    assert distributions(dividends, "us", "SGOV", DAYS[0], DAYS[9]) == pytest.approx(
        0.6
    )
    assert breakeven_days(0.04, 0.0, 0.005) == pytest.approx(0.005 / 0.04 * 365)
    assert breakeven_days(0.01, 0.02, 0.005) == float("inf")


# ------------------------------------------------------------------ rule sets
def test_stop_and_target_exit_on_the_first_close_beyond_them():
    stop_first = _bars([100, 95, 89, 120], opens=[100, 100, 100, 100])
    a = Variant("A", "A", loss=0.10, target=0.15, time_cap=60)
    units, exit_i, price, reason = simulate(stop_first, 0, a)
    assert (exit_i, price, reason) == (2, 89.0, "loss-cut")
    target_first = _bars([100, 116], opens=[100, 100])
    assert simulate(target_first, 0, a)[3] == "target"
    flat = _bars([100] * 5)
    assert simulate(flat, 0, Variant("A", "A", loss=0.1, target=0.1, time_cap=2))[
        1:
    ] == (
        2,
        100.0,
        "time",
    )
    assert simulate(flat, 0, a)[3] == "open at end"


def test_averaging_down_adds_at_each_step_and_exits_on_the_average_cost():
    bars = _bars([100, 90, 80, 80, 98, 99], opens=[100] * 6)
    b = Variant("B", "B", target=0.10, adds=(0.10, 0.20), target_on_avg=True)
    units, exit_i, price, reason = simulate(bars, 0, b)
    assert [u.price for u in units] == [100.0, 90.0, 80.0]
    avg = 3 / (1 / 100 + 1 / 90 + 1 / 80)  # equal notional per unit, 89.26
    # 98 is below avg x 1.10 = 98.18, so the exit is the next close, 99.
    assert (exit_i, price, reason) == (5, 99.0, "target")
    assert 98 < avg * 1.10 <= 99


def test_portfolio_capital_is_peak_units_and_drawdown_is_of_equity():
    bars = _bars([100, 50, 100, 100], opens=[100] * 4)
    book = _book(bars)
    trade = simulate(bars, 0, Variant("C", "C"))
    from research.strategy_evidence.rulesets import Trade

    t = Trade("us", "AAA", *trade)
    p = portfolio([t], book, "us")
    assert p["capital_units"] == 1.0
    cost = 0.009
    # Equity 1 + pnl: day0 1-0.009, day1 0.5-0.009, then back near 1.
    assert p["max_drawdown"] == pytest.approx((0.5 - cost) / (1 - cost) - 1)
    assert p["return_per_unit"] == pytest.approx(-cost)
