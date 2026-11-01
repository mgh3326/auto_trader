"""Point-in-time listing intervals (network-free)."""

from __future__ import annotations

from datetime import date

from research.us_universe.pit import (
    build_pit_rows,
    inactive_shares,
    rows_from_csv,
    rows_to_csv,
    universe_as_of,
)
from research.us_universe.symdir import (
    SERIES_NASDAQ,
    SERIES_OTHER,
    ListingRow,
    Observation,
)

D1, D2, D3, D4 = (
    date(2015, 1, 2),
    date(2017, 6, 1),
    date(2020, 3, 2),
    date(2026, 10, 30),
)


def obs(series: str, day: date, *symbols: str) -> Observation:
    rows = tuple(
        ListingRow(s, s, f"{s} Inc. Common Stock", "NASDAQ", False, True)
        for s in sorted(symbols)
    )
    return Observation(series, day, f"{day.isoformat()}T17:03", rows)


OBSERVATIONS = [
    obs(SERIES_NASDAQ, D1, "ANCIENT", "OLD", "DEAD", "KEEP", "GAP"),
    obs(SERIES_NASDAQ, D2, "OLD", "DEAD", "KEEP"),
    obs(SERIES_NASDAQ, D3, "KEEP", "NEW", "GAP"),
    obs(SERIES_NASDAQ, D4, "KEEP", "NEW", "GAP", "MOVE"),
    obs(SERIES_OTHER, D1, "MOVE"),
    obs(SERIES_OTHER, D4, "OTHERONLY"),
]
ROWS = build_pit_rows(OBSERVATIONS)


def by_symbol(symbol: str):
    return [r for r in ROWS if r.symbol == symbol]


def test_universe_at_t_excludes_symbols_listed_after_t() -> None:
    t = date(2018, 1, 1)
    assert "NEW" not in universe_as_of(ROWS, t)  # first seen 2020-03-02
    assert "NEW" in universe_as_of(ROWS, D3)


def test_universe_at_t_includes_symbols_delisted_after_t() -> None:
    # DEAD was last seen 2017-06-01 and is absent from 2020 on.
    assert "DEAD" in universe_as_of(ROWS, date(2016, 1, 1))
    assert "DEAD" in universe_as_of(ROWS, D2)  # listed_to is inclusive
    assert "DEAD" not in universe_as_of(ROWS, date(2018, 1, 1))
    assert "DEAD" not in universe_as_of(ROWS, D4)


def test_inactive_interval_brackets_the_unknown_delisting_date() -> None:
    (dead,) = by_symbol("DEAD")
    assert dead.status == "inactive"
    assert (dead.listed_to, dead.delisted_by) == (D2, D3)
    assert dead.status_source == "nasdaqlisted@2020-03-02"
    assert dead.listed_from_censored is True  # present in the first observation


def test_active_symbol_has_open_interval() -> None:
    (new,) = by_symbol("NEW")
    assert new.status == "active" and new.listed_to is None and new.delisted_by is None
    assert new.listed_from == D3 and new.listed_from_censored is False
    assert new.status_source == "nasdaqlisted@2026-10-30"


def test_absence_splits_intervals_and_relisting_is_a_new_interval() -> None:
    gap = by_symbol("GAP")
    assert [(r.listed_from, r.listed_to, r.status) for r in gap] == [
        (D1, D1, "inactive"),
        (D3, None, "active"),
    ]
    assert "GAP" not in universe_as_of(ROWS, D2)


def test_series_are_independent_and_transfer_keeps_symbol_active() -> None:
    move = by_symbol("MOVE")
    assert {(r.series, r.status) for r in move} == {
        (SERIES_OTHER, "inactive"),
        (SERIES_NASDAQ, "active"),
    }
    # An otherlisted symbol is not "delisted" by a nasdaqlisted observation date.
    (other_only,) = by_symbol("OTHERONLY")
    assert other_only.status == "active"


def test_csv_roundtrip_is_lossless_and_deterministic() -> None:
    data = rows_to_csv(ROWS)
    assert rows_from_csv(data) == ROWS
    assert rows_to_csv(build_pit_rows(list(reversed(OBSERVATIONS)))) == data


def test_inactive_share_is_symbol_level() -> None:
    shares = {
        s.scope: s
        for s in inactive_shares(ROWS, window_start=date(2016, 1, 1), window_end=D4)
    }
    total = shares["all_symbols_all_time"]
    # 8 symbols; inactive: ANCIENT OLD DEAD (GAP relisted, MOVE transferred)
    assert (total.symbols, total.inactive) == (8, 3)
    windowed = shares["all_symbols_listed_in_2016-01-01..2026-10-30"]
    # ANCIENT's only interval ends in 2015, before the window.
    assert (windowed.symbols, windowed.inactive) == (7, 2)
