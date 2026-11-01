"""Markouts on a small fixture whose answers are worked out by hand."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from research.trade_retro.markouts import cluster_ci, index_excess, markout
from research.trade_retro.prices import Bars, PriceBook
from tests.research.trade_retro.retro_fixtures import weekdays

pytestmark = pytest.mark.unit

DAYS = weekdays(date(2026, 6, 1), 25)  # 2026-06-01 is a Monday
CLOSES = [100.0 + i for i in range(25)]  # session i closes at 100 + i
BARS = Bars.from_rows([(d, c, c) for d, c in zip(DAYS, CLOSES, strict=True)])


def test_buy_markouts_at_1_5_and_20_sessions():
    assert markout(BARS, DAYS[0], 100.0, "buy", 1) == pytest.approx(0.01)
    assert markout(BARS, DAYS[0], 100.0, "buy", 5) == pytest.approx(0.05)
    assert markout(BARS, DAYS[0], 100.0, "buy", 20) == pytest.approx(0.20)


def test_markout_runs_from_the_fill_price_not_the_close():
    assert markout(BARS, DAYS[0], 80.0, "buy", 20) == pytest.approx(120.0 / 80.0 - 1.0)


def test_sell_markout_is_positive_when_the_price_falls_after_the_sale():
    falling = Bars.from_rows([(d, 100.0 - i, 100.0 - i) for i, d in enumerate(DAYS)])
    assert markout(falling, DAYS[0], 100.0, "sell", 5) == pytest.approx(0.05)
    assert markout(BARS, DAYS[0], 100.0, "sell", 5) == pytest.approx(-0.05)


def test_a_fill_on_a_non_session_day_starts_from_the_next_session():
    saturday = date(2026, 6, 6)
    monday_index = DAYS.index(date(2026, 6, 8))
    assert markout(BARS, saturday, 100.0, "buy", 1) == pytest.approx(
        CLOSES[monday_index + 1] / 100.0 - 1.0
    )


def test_a_horizon_past_the_last_bar_is_censored_not_zero():
    assert markout(BARS, DAYS[5], 100.0, "buy", 20) is None
    assert markout(BARS, DAYS[4], 100.0, "buy", 20) == pytest.approx(
        CLOSES[24] / 100.0 - 1.0
    )
    assert markout(BARS, date(2026, 12, 1), 100.0, "buy", 1) is None


def test_index_excess_uses_the_same_dates_and_flips_for_a_sell():
    index = Bars.from_rows(
        [(d, 1000.0 + 10 * i, 1000.0 + 10 * i) for i, d in enumerate(DAYS)]
    )
    book = PriceBook(bars={}, benchmarks={"us": index}, missing={})
    index_move = 1050.0 / 1000.0 - 1.0

    buy = markout(BARS, DAYS[0], 100.0, "buy", 5)
    assert index_excess(buy, book, "us", BARS, DAYS[0], "buy", 5) == pytest.approx(
        0.05 - index_move
    )

    sell = markout(BARS, DAYS[0], 100.0, "sell", 5)
    assert index_excess(sell, book, "us", BARS, DAYS[0], "sell", 5) == pytest.approx(
        -0.05 + index_move
    )
    assert index_excess(None, book, "us", BARS, DAYS[0], "buy", 5) is None


def test_split_factor_covers_only_splits_after_the_day():
    bars = Bars.from_rows(
        [(d, 1.0, 1.0) for d in DAYS], splits=[(DAYS[3], 2.0), (DAYS[10], 1.5)]
    )
    assert bars.split_factor_after(DAYS[0]) == pytest.approx(3.0)
    assert bars.split_factor_after(DAYS[3]) == pytest.approx(1.5)
    assert bars.split_factor_after(DAYS[10]) == pytest.approx(1.0)


def test_cluster_ci_is_deterministic_and_needs_three_symbols():
    values = np.array([0.1, 0.2, -0.1, 0.0, 0.3, -0.2])
    clusters = np.array(["a", "a", "b", "b", "c", "c"])
    first = cluster_ci(values, clusters)
    assert first == cluster_ci(values, clusters)
    assert first[0] <= values.mean() <= first[1]
    assert cluster_ci(values[:4], clusters[:4]) is None
