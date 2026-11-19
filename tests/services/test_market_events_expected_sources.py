"""Unit tests for app.services.market_events.expected_sources."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)

FINNHUB_US = ("finnhub", "earnings", "us")
DART_KR = ("dart", "disclosure", "kr")
FOREXFACTORY_GLOBAL = ("forexfactory", "economic", "global")
WISEFN_KR = ("wisefn", "earnings", "kr")


@pytest.mark.unit
def test_expected_sources_on_weekday_are_finnhub_and_dart_only() -> None:
    triples = expected_sources_for_date(date(2026, 5, 11))  # Monday
    assert triples == frozenset({FINNHUB_US, DART_KR})


@pytest.mark.unit
def test_expected_sources_on_us_weekend_is_empty() -> None:
    # Saturday: XNYS and XKRX both closed; forexfactory is no longer expected.
    assert expected_sources_for_date(date(2026, 5, 9)) == frozenset()


@pytest.mark.unit
def test_expected_sources_drops_dart_on_kr_weekend() -> None:
    triples = expected_sources_for_date(date(2026, 5, 10))  # Sunday in KST
    assert DART_KR not in triples


@pytest.mark.unit
def test_expected_sources_constant_matches_per_day_union() -> None:
    # Every triple yielded by expected_sources_for_date must be a member of EXPECTED_SOURCES.
    for d in (date(2026, 5, 11), date(2026, 5, 9), date(2026, 5, 10)):
        for triple in expected_sources_for_date(d):
            assert triple in EXPECTED_SOURCES


@pytest.mark.unit
def test_expected_sources_constant_is_finnhub_and_dart_only() -> None:
    # forexfactory and wisefn are manual-only: no scheduled ingestion, so they
    # must never be expected (freshness and retro data_gaps would report them
    # as missing forever).
    assert EXPECTED_SOURCES == frozenset({FINNHUB_US, DART_KR})
    assert FOREXFACTORY_GLOBAL not in EXPECTED_SOURCES
    assert WISEFN_KR not in EXPECTED_SOURCES


@pytest.mark.unit
def test_wisefn_is_never_expected_on_any_day() -> None:
    # Weekday (KR open), KR weekend, US-only holiday window.
    for d in (date(2026, 5, 11), date(2026, 5, 10), date(2025, 7, 4)):
        assert WISEFN_KR not in expected_sources_for_date(d)


@pytest.mark.unit
def test_forexfactory_is_never_expected_even_on_closed_days() -> None:
    # The old rule expected forexfactory every day; it must now be absent on
    # weekends and holidays too.
    for d in (date(2026, 5, 9), date(2026, 5, 10), date(2025, 7, 4)):
        assert FOREXFACTORY_GLOBAL not in expected_sources_for_date(d)


@pytest.mark.unit
def test_holiday_aware_us_holiday_drops_finnhub_and_keeps_dart() -> None:
    # 2025-07-04 Independence Day — XNYS closed (a Friday, not a weekend).
    # KRX is open that day, so only the US source drops out.
    triples = expected_sources_for_date(date(2025, 7, 4))
    assert FINNHUB_US not in triples
    assert DART_KR in triples


@pytest.mark.unit
def test_holiday_aware_kr_holiday_drops_dart() -> None:
    # 2025-01-01 New Year — XKRX closed (a Wednesday, not a weekend).
    triples = expected_sources_for_date(date(2025, 1, 1))
    assert DART_KR not in triples


@pytest.mark.unit
def test_regular_session_day_expects_finnhub_and_dart() -> None:
    # 2025-07-07 Monday — both XNYS and XKRX open.
    assert expected_sources_for_date(date(2025, 7, 7)) == frozenset(
        {FINNHUB_US, DART_KR}
    )
