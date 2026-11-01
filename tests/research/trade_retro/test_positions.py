"""FIFO matching, opening lots, unknown-basis sells and the idle-cash bound."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from research.trade_retro.idle import daily_account, opening_quantity, summarize_idle
from research.trade_retro.positions import walk_symbol
from research.trade_retro.prices import Bars, PriceBook
from tests.research.trade_retro.retro_fixtures import weekdays

pytestmark = pytest.mark.unit

CUTOVER = datetime(2026, 5, 10, tzinfo=UTC)


def _stamp(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def _rows(*items) -> pd.DataFrame:
    """Each item is (day, side, qty, price[, source])."""
    rows = []
    for number, item in enumerate(items):
        day, side, qty, price, *rest = item
        source = rest[0] if rest else "reconciler"
        seeded = source in ("manual_import", "implausible_price")
        stamp = CUTOVER if source == "manual_import" else _stamp(day)
        rows.append(
            {
                "alias": "acct",
                "market": "us",
                "symbol": "AAA",
                "date": day,
                "ts": stamp,
                "side": side,
                "qty": float(qty),
                "price": float(price),
                "seed": seeded,
                "source": source,
                "key": None if seeded else f"k{number}",
            }
        )
    return pd.DataFrame(rows)


JUN1, JUN2, JUN3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
MARK = (150.0, date(2026, 10, 30))


def test_fifo_matches_oldest_lot_first_and_marks_the_rest():
    walk = walk_symbol(
        _rows((JUN1, "buy", 10, 100), (JUN2, "buy", 10, 120), (JUN3, "sell", 15, 130)),
        CUTOVER,
        MARK,
    )
    realized = [m for m in walk.matches if m.realized]
    assert [(m.qty, m.entry_price, m.exit_price) for m in realized] == [
        (10, 100, 130),
        (5, 120, 130),
    ]
    still_open = [m for m in walk.matches if not m.realized]
    assert [(m.qty, m.entry_price, m.exit_price, m.exit_day) for m in still_open] == [
        (5, 120, 150.0, MARK[1])
    ]
    assert walk.unknown_sells == []


def test_a_new_buy_after_going_flat_is_a_new_position():
    walk = walk_symbol(
        _rows((JUN1, "buy", 1, 100), (JUN2, "sell", 1, 110), (JUN3, "buy", 1, 90)),
        CUTOVER,
        MARK,
    )
    assert [m.episode for m in walk.matches] == [0, 1]


def test_a_sell_beyond_known_lots_has_unknown_basis_and_no_invented_return():
    walk = walk_symbol(
        _rows((JUN1, "buy", 2, 100), (JUN2, "sell", 5, 110)), CUTOVER, MARK
    )
    assert sum(m.qty for m in walk.matches) == 2
    assert [(u.qty, u.price, u.pre_cutover) for u in walk.unknown_sells] == [
        (3, 110, False)
    ]


def test_older_holdings_a_sell_proves_go_first_so_window_lots_stay_open():
    """Sell 3 with 2 recorded: 3 shares are unknown, and the window lot stays held."""
    walk = walk_symbol(
        _rows((JUN1, "sell", 3, 110), (JUN2, "buy", 2, 100)), CUTOVER, MARK
    )
    assert [(u.qty, u.price) for u in walk.unknown_sells] == [(3, 110)]
    assert [(m.qty, m.realized, m.origin) for m in walk.matches] == [(2, False, "fill")]

    later = walk_symbol(
        _rows((JUN1, "buy", 2, 100), (JUN2, "sell", 3, 110), (JUN3, "buy", 1, 90)),
        CUTOVER,
        MARK,
    )
    assert [(u.qty, u.price) for u in later.unknown_sells] == [(1, 110)]
    assert [(m.qty, m.entry_price, m.realized) for m in later.matches] == [
        (2, 100, True),
        (1, 90, False),
    ]


def test_plain_fifo_sensitivity_lets_old_sells_use_up_window_lots():
    walk = walk_symbol(
        _rows((JUN1, "buy", 2, 100), (JUN2, "sell", 3, 110), (JUN3, "buy", 1, 90)),
        CUTOVER,
        MARK,
        older_first=False,
    )
    assert [(u.qty, u.price) for u in walk.unknown_sells] == [(1, 110)]
    assert [(m.qty, m.entry_price, m.realized) for m in walk.matches] == [
        (2, 100, True),
        (1, 90, False),
    ]
    held_later = walk_symbol(
        _rows((JUN1, "sell", 3, 110), (JUN2, "buy", 2, 100)),
        CUTOVER,
        MARK,
        older_first=False,
    )
    assert [m.realized for m in held_later.matches] == [False]


LATER = date(2026, 9, 29)


def _later_seed(qty, price):
    """A second seed run: its stamp is after the first batch's cutover."""
    return (LATER, "buy", qty, price, "manual_import")


def _stamp_later_seed(frame):
    frame.loc[frame["date"] == LATER, "ts"] = datetime(2026, 9, 29, tzinfo=UTC)
    return frame


def test_a_later_seed_below_the_ledger_resets_the_holding_with_no_return_on_the_excess():
    walk = walk_symbol(
        _stamp_later_seed(_rows((JUN1, "buy", 10, 100), _later_seed(3, 60))),
        CUTOVER,
        MARK,
        seed_base={LATER: (110.0, LATER)},
    )
    assert [(m.qty, m.origin, m.realized) for m in walk.matches] == [(3, "fill", False)]
    assert walk.resets == [
        {"alias": "acct", "market": "us", "symbol": "AAA", "ledger": 10.0, "seed": 3.0}
    ]


def test_a_later_seed_above_the_ledger_adds_only_the_difference_at_its_price():
    walk = walk_symbol(
        _stamp_later_seed(_rows((JUN1, "buy", 10, 100), _later_seed(14, 60))),
        CUTOVER,
        MARK,
        seed_base={LATER: (110.0, LATER)},
    )
    assert sorted(
        (m.qty, m.origin, m.entry_price, m.base_price) for m in walk.matches
    ) == [
        (4, "seed", 60, 110.0),
        (10, "fill", 100, 100),
    ]


def test_on_top_sensitivity_adds_the_whole_later_seed():
    walk = walk_symbol(
        _stamp_later_seed(_rows((JUN1, "buy", 10, 100), _later_seed(3, 60))),
        CUTOVER,
        MARK,
        later_seeds_reset=False,
    )
    assert sum(m.qty for m in walk.matches) == 13
    assert walk.resets == []


def test_a_buy_before_the_cutover_is_not_embedded_in_a_later_seed():
    walk = walk_symbol(
        _stamp_later_seed(_rows((date(2026, 5, 4), "buy", 3, 90), _later_seed(3, 60))),
        CUTOVER,
        MARK,
    )
    assert walk.embedded_buys == 0
    assert [(m.qty, m.origin) for m in walk.matches] == [(3, "fill")]


def test_a_later_seed_goes_through_cash_so_nav_does_not_jump():
    days = weekdays(date(2026, 9, 28), 3)
    assert LATER in days
    stock = Bars.from_rows([(d, 100.0, 100.0) for d in days])
    index = Bars.from_rows([(d, 1000.0, 1000.0) for d in days])
    book = PriceBook(bars={("us", "AAA"): stock}, benchmarks={"us": index}, missing={})
    fills = _stamp_later_seed(_rows((days[0], "buy", 10, 100), _later_seed(3, 60)))

    frame, _ = daily_account(fills, book, "acct", "us", CUTOVER)
    assert frame["risk"].tolist() == pytest.approx([1000.0, 300.0, 300.0])
    assert frame["cash"].tolist() == pytest.approx([0.0, 700.0, 700.0])
    assert frame["nav"].tolist() == pytest.approx([1000.0] * 3)

    on_top, _ = daily_account(
        fills, book, "acct", "us", CUTOVER, later_seeds_reset=False
    )
    assert on_top["risk"].tolist() == pytest.approx([1300.0] * 3)


def test_cash_from_a_fill_on_a_non_session_day_lands_on_the_next_session():
    days = weekdays(date(2026, 6, 1), 7)  # Monday 06-01 to Tuesday 06-09
    stock = Bars.from_rows([(d, 100.0, 100.0) for d in days])
    index = Bars.from_rows([(d, 1000.0, 1000.0) for d in days])
    book = PriceBook(bars={("us", "AAA"): stock}, benchmarks={"us": index}, missing={})
    saturday = date(2026, 6, 6)
    fills = _rows((days[0], "buy", 10, 100), (saturday, "sell", 10, 100))

    frame, _ = daily_account(fills, book, "acct", "us", CUTOVER)
    monday = days.index(date(2026, 6, 8))
    assert frame["cash"].tolist()[monday - 1] == pytest.approx(0.0)
    assert frame["cash"].tolist()[monday] == pytest.approx(1000.0)
    assert frame["nav"].tolist() == pytest.approx([1000.0] * len(days))


def test_each_piece_of_a_lot_keeps_the_lots_identity():
    walk = walk_symbol(
        _rows((JUN1, "buy", 10, 100), (JUN2, "sell", 4, 110), (JUN3, "sell", 2, 120)),
        CUTOVER,
        MARK,
    )
    assert [m.qty for m in walk.matches] == [4, 2, 4]
    assert len({m.lot for m in walk.matches}) == 1


def test_seed_lot_returns_from_the_seed_date_close_and_keeps_broker_cost():
    seed_day = date(2026, 5, 10)
    walk = walk_symbol(
        _rows((seed_day, "buy", 4, 60, "manual_import"), (JUN1, "sell", 4, 110)),
        CUTOVER,
        MARK,
        seed_base={seed_day: (100.0, seed_day)},
    )
    match = walk.matches[0]
    assert (match.origin, match.entry_price, match.base_price) == ("seed", 60, 100.0)


def test_a_buy_before_the_cutover_is_inside_a_seed_and_makes_no_lot():
    early = date(2026, 5, 4)
    seed_day = date(2026, 5, 10)
    walk = walk_symbol(
        _rows((early, "buy", 3, 90), (seed_day, "buy", 5, 95, "manual_import")),
        CUTOVER,
        MARK,
    )
    assert walk.embedded_buys == 1
    assert [(m.qty, m.origin) for m in walk.matches] == [(5, "seed")]


def test_cutover_is_an_instant_so_a_seed_on_another_local_date_still_counts():
    """The same seed instant is 05-09 in New York and 05-10 in Seoul."""
    early = date(2026, 5, 9)
    walk = walk_symbol(
        _rows((early, "buy", 3, 90), (date(2026, 5, 9), "buy", 5, 95, "manual_import")),
        CUTOVER,
        MARK,
    )
    assert walk.embedded_buys == 1


def test_a_buy_before_the_cutover_of_an_unseeded_symbol_makes_a_lot():
    walk = walk_symbol(_rows((date(2026, 5, 4), "buy", 3, 90)), CUTOVER, MARK)
    assert walk.embedded_buys == 0
    assert [m.origin for m in walk.matches] == ["fill"]


def test_an_implausible_fill_is_a_seed_lot_but_does_not_turn_on_the_seed_rule():
    walk = walk_symbol(
        _rows(
            (date(2026, 5, 4), "buy", 3, 90), (JUN1, "buy", 5, 40, "implausible_price")
        ),
        CUTOVER,
        MARK,
        seed_base={JUN1: (100.0, JUN1)},
    )
    assert walk.embedded_buys == 0
    assert sorted(m.origin for m in walk.matches) == ["fill", "seed"]


def test_opening_quantity_uses_the_seed_and_infers_any_remaining_shortfall():
    seeded = _rows(
        (date(2026, 5, 4), "buy", 2, 90),
        (date(2026, 5, 10), "buy", 5, 95, "manual_import"),
        (JUN1, "sell", 1, 100),
    )
    assert opening_quantity(seeded, CUTOVER) == (3.0, 0.0)
    unseeded = _rows((JUN1, "buy", 1, 100), (JUN2, "sell", 4, 110))
    assert opening_quantity(unseeded, CUTOVER) == (3.0, 3.0)


def test_a_later_seed_counts_as_held_from_the_start_with_no_nav_jump():
    days = weekdays(JUN1, 3)
    stock = Bars.from_rows([(d, 100.0, 100.0) for d in days])
    index = Bars.from_rows([(d, 1000.0, 1000.0) for d in days])
    book = PriceBook(bars={("us", "AAA"): stock}, benchmarks={"us": index}, missing={})
    fills = _rows(
        (days[0], "buy", 1, 100), (days[2], "buy", 5, 40, "implausible_price")
    )
    assert opening_quantity(fills, CUTOVER) == (5.0, 0.0)

    frame, _ = daily_account(fills, book, "acct", "us", CUTOVER)
    # Five opening shares plus one bought with the 100 of lower-bound cash: flat at 600.
    assert frame["nav"].tolist() == pytest.approx([600.0] * 3)


def test_idle_cash_lower_bound_and_forgone_return_on_known_numbers():
    days = weekdays(JUN1, 4)
    stock = Bars.from_rows([(d, 100.0, 100.0) for d in days])
    index = Bars.from_rows(
        [(d, c, c) for d, c in zip(days, (1000.0, 1100.0, 1100.0, 1210.0), strict=True)]
    )
    book = PriceBook(bars={("us", "AAA"): stock}, benchmarks={"us": index}, missing={})
    # Buy 10 at 100 on day 0, sell 10 at 100 on day 1. Lowest balance is -1000.
    fills = _rows((days[0], "buy", 10, 100), (days[1], "sell", 10, 100))

    frame, meta = daily_account(fills, book, "acct", "us", CUTOVER)
    assert meta["opening_cash_lower_bound"] == pytest.approx(1000.0)
    assert frame["cash"].tolist() == pytest.approx([0.0, 1000.0, 1000.0, 1000.0])
    assert frame["nav"].tolist() == pytest.approx([1000.0] * 4)

    summary = summarize_idle(frame)
    assert summary["cash_share"] == pytest.approx(0.75)
    # Day 0 cash 0 earns nothing, day 1 +0%, day 2 +10% on 1000.
    assert summary["forgone_cash"] == pytest.approx(100.0)
    assert summary["index_return"] == pytest.approx(0.21)
    assert summary["nav_return"] == pytest.approx(0.0)
