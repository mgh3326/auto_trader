"""Point-in-time universe: listing/delisting boundaries and fail-closed coverage."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from research.crypto_universe.universe import (
    EPISODE_SPLIT_GAP_DAYS,
    OutsideCoverageError,
    PairListing,
    PitUniverse,
    derive_listing,
)

COVERAGE_END = date(2026, 9, 30)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _listing(symbol: str, start: date, end: date, status: str) -> PairListing:
    return derive_listing(
        venue="binance_usdt_spot",
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        traded_days=_days(start, end),
        exchange_status={"live": "TRADING", "delisted": "BREAK"}[status],
        coverage_end=COVERAGE_END,
        source="test",
    )


@pytest.fixture
def universe() -> PitUniverse:
    return PitUniverse(
        coverage_start=date(2017, 8, 17),
        coverage_end=COVERAGE_END,
        listings=(
            _listing("OLDUSDT", date(2017, 8, 17), COVERAGE_END, "live"),
            # delisted on 2021-06-30 (last trade)
            _listing("DEADUSDT", date(2018, 1, 1), date(2021, 6, 30), "delisted"),
            # listed (first trade) on 2022-03-01
            _listing("NEWUSDT", date(2022, 3, 1), COVERAGE_END, "live"),
        ),
    )


def test_universe_at_t_excludes_pairs_listed_after_t(universe: PitUniverse) -> None:
    t = date(2022, 2, 28)
    assert "NEWUSDT" not in universe.universe_as_of(t)
    assert "NEWUSDT" in universe.universe_as_of(t + timedelta(days=1))


def test_universe_at_t_includes_pairs_delisted_after_t(universe: PitUniverse) -> None:
    t = date(2021, 6, 30)  # last trade day of DEADUSDT
    assert "DEADUSDT" in universe.universe_as_of(t)
    assert "DEADUSDT" in universe.universe_as_of(date(2019, 5, 1))
    assert "DEADUSDT" not in universe.universe_as_of(t + timedelta(days=1))


def test_universe_full_membership_on_known_days(universe: PitUniverse) -> None:
    assert universe.universe_as_of(date(2017, 8, 16)) == frozenset()
    assert universe.universe_as_of(date(2020, 1, 1)) == {"OLDUSDT", "DEADUSDT"}
    assert universe.universe_as_of(date(2023, 1, 1)) == {"OLDUSDT", "NEWUSDT"}
    assert universe.universe_as_of(COVERAGE_END) == {"OLDUSDT", "NEWUSDT"}


def test_query_after_coverage_end_fails_closed(universe: PitUniverse) -> None:
    with pytest.raises(OutsideCoverageError):
        universe.universe_as_of(COVERAGE_END + timedelta(days=1))


def test_relisted_symbol_is_absent_between_episodes() -> None:
    first = _days(date(2020, 8, 21), date(2022, 5, 13))
    second = _days(date(2022, 5, 31), COVERAGE_END)
    listing = derive_listing(
        venue="binance_usdt_spot",
        symbol="LUNAUSDT",
        base_asset="LUNA",
        quote_asset="USDT",
        traded_days=first + second,
        exchange_status="TRADING",
        coverage_end=COVERAGE_END,
        source="test",
    )
    assert listing.episodes == (
        (date(2020, 8, 21), date(2022, 5, 13)),
        (date(2022, 5, 31), COVERAGE_END),
    )
    assert listing.gap_days == 0  # the gap between episodes is not an inner gap
    universe = PitUniverse(date(2020, 1, 1), COVERAGE_END, (listing,))
    assert universe.universe_as_of(date(2022, 5, 13)) == {"LUNAUSDT"}
    assert universe.universe_as_of(date(2022, 5, 20)) == frozenset()
    assert universe.universe_as_of(date(2022, 6, 1)) == {"LUNAUSDT"}


def test_short_gap_stays_inside_one_episode() -> None:
    days = _days(date(2021, 1, 1), date(2021, 1, 10))
    resume = date(2021, 1, 10) + timedelta(days=EPISODE_SPLIT_GAP_DAYS + 1)
    listing = derive_listing(
        venue="upbit_krw",
        symbol="KRW-ABC",
        base_asset="ABC",
        quote_asset="KRW",
        traded_days=[*days, resume],
        exchange_status="ACTIVE",
        coverage_end=COVERAGE_END,
        source="test",
    )
    assert len(listing.episodes) == 1
    assert listing.gap_days == EPISODE_SPLIT_GAP_DAYS


def test_derive_listing_records_last_trade_and_censoring() -> None:
    dead = _listing("DEADUSDT", date(2018, 1, 1), date(2021, 6, 30), "delisted")
    assert dead.status == "delisted"
    assert dead.last_trade_date == date(2021, 6, 30)
    assert dead.last_trade_censored is False
    live = _listing("OLDUSDT", date(2018, 1, 1), COVERAGE_END, "live")
    assert live.last_trade_censored is True
    none = derive_listing(
        venue="binance_usdt_spot",
        symbol="ZEROUSDT",
        base_asset="ZERO",
        quote_asset="USDT",
        traded_days=[],
        exchange_status="BREAK",
        coverage_end=COVERAGE_END,
        source="test",
    )
    assert none.status == "no_trades"
    universe = PitUniverse(date(2017, 1, 1), COVERAGE_END, (none,))
    assert universe.universe_as_of(date(2020, 1, 1)) == frozenset()


def test_unknown_exchange_state_is_not_treated_as_live() -> None:
    listing = derive_listing(
        venue="binance_usdt_spot",
        symbol="HALTUSDT",
        base_asset="HALT",
        quote_asset="USDT",
        traded_days=_days(date(2020, 1, 1), date(2020, 3, 1)),
        exchange_status="HALT",
        coverage_end=COVERAGE_END,
        source="test",
    )
    assert listing.status == "not_trading_other"
    universe = PitUniverse(date(2020, 1, 1), COVERAGE_END, (listing,))
    assert universe.universe_as_of(date(2020, 3, 2)) == frozenset()


def test_csv_round_trip_is_lossless(universe: PitUniverse) -> None:
    text = universe.to_csv()
    restored = PitUniverse(
        universe.coverage_start,
        universe.coverage_end,
        PitUniverse.listings_from_csv(text),
    )
    assert restored.to_csv() == text
    assert restored.sha256() == universe.sha256()
    assert set(restored.listings) == set(universe.listings)


def test_listing_after_coverage_end_is_rejected() -> None:
    with pytest.raises(ValueError, match="after coverage_end"):
        derive_listing(
            venue="binance_usdt_spot",
            symbol="FUTUREUSDT",
            base_asset="FUTURE",
            quote_asset="USDT",
            traded_days=[COVERAGE_END + timedelta(days=1)],
            exchange_status="TRADING",
            coverage_end=COVERAGE_END,
            source="test",
        )


def test_leveraged_and_stable_flags() -> None:
    up = _listing("BTCUPUSDT", date(2020, 5, 14), date(2024, 2, 28), "delisted")
    busd = _listing("BUSDUSDT", date(2019, 9, 1), date(2024, 1, 1), "delisted")
    btc = _listing("BTCUSDT", date(2017, 8, 17), COVERAGE_END, "live")
    assert up.is_leveraged_token and not up.is_stable_base
    assert busd.is_stable_base and not busd.is_leveraged_token
    assert not btc.is_leveraged_token and not btc.is_stable_base


@pytest.mark.parametrize("base", ["JUP", "SYRUP", "SUPER", "BURGER"])
def test_assets_ending_in_up_are_not_leveraged_tokens(base: str) -> None:
    listing = _listing(f"{base}USDT", date(2024, 1, 1), COVERAGE_END, "live")
    assert not listing.is_leveraged_token
