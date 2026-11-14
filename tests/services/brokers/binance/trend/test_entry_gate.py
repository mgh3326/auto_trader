"""Pure reservation decisions and kill switches (no DB, no network)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.services.brokers.binance.trend.constants import LOCKED_LIMITS
from app.services.brokers.binance.trend.entry_gate import (
    ActiveSlot,
    EntryBook,
    EntryRefusal,
    conflict_stop_tripped,
    decide_entry,
    max_consecutive_hard_stops,
    utc_day_start,
    utc_week_start,
)

pytestmark = pytest.mark.unit


def book(
    *,
    active: tuple[tuple[str, str], ...] = (),
    uncertain: tuple[str, ...] = (),
    stops: int = 0,
    tripped: bool = False,
) -> EntryBook:
    return EntryBook(
        active=tuple(ActiveSlot(symbol, D(notional)) for symbol, notional in active),
        uncertain_symbols=frozenset(uncertain),
        max_consecutive_hard_stops_today=stops,
        conflict_stop_tripped=tripped,
    )


def reasons(b: EntryBook, symbol: str = "BTCUSDT", notional: str = "60") -> set[str]:
    return set(decide_entry(book=b, symbol=symbol, notional_usdt=D(notional)).reasons)


def test_empty_book_allows_a_full_leg() -> None:
    decision = decide_entry(book=book(), symbol="BTCUSDT", notional_usdt=D("60"))
    assert decision.allowed and decision.reasons == ()


def test_four_distinct_symbols_fill_the_book_exactly_to_the_gross_cap() -> None:
    held = (("BTCUSDT", "60"), ("ETHUSDT", "60"), ("SOLUSDT", "60"))
    assert reasons(book(active=held), "XRPUSDT") == set()


def test_fifth_position_is_refused() -> None:
    held = tuple((s, "60") for s in LOCKED_LIMITS.universe)
    assert EntryRefusal.MAX_CONCURRENT_POSITIONS_REACHED in reasons(
        book(active=held), "BTCUSDT"
    )
    # A fifth symbol does not exist in the locked universe; the count gate is
    # what stops a re-entry once four slots are used.
    assert EntryRefusal.MAX_CONCURRENT_POSITIONS_REACHED in reasons(
        book(active=held), "XRPUSDT"
    )


def test_second_position_on_the_same_symbol_is_refused() -> None:
    assert EntryRefusal.SYMBOL_ALREADY_ACTIVE in reasons(
        book(active=(("BTCUSDT", "60"),)), "BTCUSDT"
    )
    assert reasons(book(active=(("BTCUSDT", "60"),)), "ETHUSDT") == set()


def test_gross_over_240_is_refused() -> None:
    # Fills may drift above the leg cap, so the gross sum can bind even with
    # three slots used: 70 + 70 + 70 + 60 = 270 > 240.
    held = (("BTCUSDT", "70"), ("ETHUSDT", "70"), ("SOLUSDT", "70"))
    assert reasons(book(active=held), "XRPUSDT") == {
        EntryRefusal.GROSS_NOTIONAL_CAP_EXCEEDED
    }
    held_ok = (("BTCUSDT", "60"), ("ETHUSDT", "60"), ("SOLUSDT", "60"))
    assert reasons(book(active=held_ok), "XRPUSDT") == set()
    over = (("BTCUSDT", "60"), ("ETHUSDT", "60"), ("SOLUSDT", "60.01"))
    assert EntryRefusal.GROSS_NOTIONAL_CAP_EXCEEDED in reasons(
        book(active=over), "XRPUSDT"
    )


@pytest.mark.parametrize("notional", ["60.01", "61", "1000"])
def test_leg_over_the_cap_is_refused(notional: str) -> None:
    assert EntryRefusal.LEG_NOTIONAL_CAP_EXCEEDED in reasons(book(), notional=notional)


@pytest.mark.parametrize("notional", ["0", "-5", "NaN", "Infinity"])
def test_bad_notional_is_refused(notional: str) -> None:
    assert EntryRefusal.INVALID_NOTIONAL in reasons(book(), notional=notional)


def test_symbol_outside_the_universe_is_refused() -> None:
    assert EntryRefusal.SYMBOL_NOT_IN_UNIVERSE in reasons(book(), "DOGEUSDT")


def test_two_consecutive_hard_stops_in_a_utc_day_stop_new_entries() -> None:
    assert reasons(book(stops=1)) == set()
    assert reasons(book(stops=2)) == {EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED}
    assert EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED in reasons(book(stops=5))


def test_a_tripped_conflict_stop_blocks_new_entries() -> None:
    assert reasons(book(tripped=False)) == set()
    assert reasons(book(tripped=True)) == {EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED}


def test_an_uncertain_intent_blocks_only_its_symbol() -> None:
    b = book(uncertain=("BTCUSDT",))
    assert EntryRefusal.UNCERTAIN_INTENT_UNRESOLVED in reasons(b, "BTCUSDT")
    assert reasons(b, "ETHUSDT") == set()


def test_every_tripped_reason_is_reported() -> None:
    held = tuple((s, "70") for s in LOCKED_LIMITS.universe)
    b = book(active=held, uncertain=("BTCUSDT",), stops=2, tripped=True)
    assert reasons(b, "BTCUSDT", "61") == {
        EntryRefusal.LEG_NOTIONAL_CAP_EXCEEDED,
        EntryRefusal.MAX_CONCURRENT_POSITIONS_REACHED,
        EntryRefusal.SYMBOL_ALREADY_ACTIVE,
        EntryRefusal.UNCERTAIN_INTENT_UNRESOLVED,
        EntryRefusal.GROSS_NOTIONAL_CAP_EXCEEDED,
        EntryRefusal.CONSECUTIVE_HARD_STOP_LIMIT_REACHED,
        EntryRefusal.CONFLICT_WEEK_LIMIT_REACHED,
    }


def test_hard_stop_stop_is_a_daily_latch_not_a_leading_run() -> None:
    assert max_consecutive_hard_stops([]) == 0
    assert max_consecutive_hard_stops(["hard_stop", "hard_stop"]) == 2
    # A later exit of another position must not re-open the day.
    assert (
        max_consecutive_hard_stops(["hard_stop", "hard_stop", "signal_reversal"]) == 2
    )
    assert max_consecutive_hard_stops(["hard_stop", "time_exit", "hard_stop"]) == 1
    assert max_consecutive_hard_stops([None, "hard_stop"]) == 1
    assert max_consecutive_hard_stops(["hard_stop", "hard_stop", "hard_stop"]) == 3


def test_conflict_stop_latches_per_week_and_survives_the_boundary() -> None:
    def at(day: int, hour: int = 1) -> dt.datetime:
        return dt.datetime(2026, 11, day, hour, tzinfo=dt.UTC)

    assert not conflict_stop_tripped([], limit=3)
    assert not conflict_stop_tripped([at(9), at(10)], limit=3)
    assert conflict_stop_tripped([at(9), at(10), at(15, 23)], limit=3)
    # One per week for three weeks never trips.
    assert not conflict_stop_tripped([at(2), at(9), at(16)], limit=3)
    # Three on Sunday still trip after Monday: the input is the unresolved
    # records, not a window ending now.
    assert conflict_stop_tripped([at(15, 20), at(15, 21), at(15, 22)], limit=3)


def test_utc_windows() -> None:
    # 2026-11-14 is a Saturday; the UTC week starts Monday 2026-11-09.
    now = dt.datetime(2026, 11, 14, 23, 59, tzinfo=dt.UTC)
    assert utc_day_start(now) == dt.datetime(2026, 11, 14, tzinfo=dt.UTC)
    assert utc_week_start(now) == dt.datetime(2026, 11, 9, tzinfo=dt.UTC)
    # KST 2026-11-15 08:00 is still Saturday 23:00 UTC.
    kst = dt.timezone(dt.timedelta(hours=9))
    assert utc_day_start(dt.datetime(2026, 11, 15, 8, tzinfo=kst)) == dt.datetime(
        2026, 11, 14, tzinfo=dt.UTC
    )
    monday = dt.datetime(2026, 11, 9, tzinfo=dt.UTC)
    assert utc_week_start(monday) == monday
