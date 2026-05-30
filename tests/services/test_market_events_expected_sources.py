"""Unit tests for app.services.market_events.expected_sources."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.expected_sources import (
    EXPECTED_SOURCES,
    expected_sources_for_date,
)


@pytest.mark.unit
def test_expected_sources_includes_finnhub_dart_forexfactory_on_weekday() -> None:
    triples = expected_sources_for_date(date(2026, 5, 11))  # Monday
    assert ("finnhub", "earnings", "us") in triples
    assert ("dart", "disclosure", "kr") in triples
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_expected_sources_drops_finnhub_on_us_weekend() -> None:
    triples = expected_sources_for_date(date(2026, 5, 9))  # Saturday
    assert ("finnhub", "earnings", "us") not in triples
    # ForexFactory still publishes weekend data (rarely), so we still expect it.
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_expected_sources_drops_dart_on_kr_weekend() -> None:
    triples = expected_sources_for_date(date(2026, 5, 10))  # Sunday in KST
    assert ("dart", "disclosure", "kr") not in triples


@pytest.mark.unit
def test_expected_sources_constant_matches_per_day_union() -> None:
    # Every triple yielded by expected_sources_for_date must be a member of EXPECTED_SOURCES.
    for d in (date(2026, 5, 11), date(2026, 5, 9), date(2026, 5, 10)):
        for triple in expected_sources_for_date(d):
            assert triple in EXPECTED_SOURCES


@pytest.mark.unit
def test_expected_sources_includes_wisefn_constant():
    assert ("wisefn", "earnings", "kr") in EXPECTED_SOURCES


@pytest.mark.unit
def test_expected_sources_includes_wisefn_on_kr_weekday():
    triples = expected_sources_for_date(date(2026, 5, 11))  # Monday
    assert ("wisefn", "earnings", "kr") in triples


@pytest.mark.unit
def test_expected_sources_drops_wisefn_on_kr_weekend():
    triples = expected_sources_for_date(date(2026, 5, 10))  # Sunday
    assert ("wisefn", "earnings", "kr") not in triples


@pytest.mark.unit
def test_expected_sources_holiday_aware_us_drops_finnhub_keeps_global():
    # 2025-07-04 Independence Day — XNYS closed (a Friday, not a weekend).
    triples = expected_sources_for_date(date(2025, 7, 4))
    assert ("finnhub", "earnings", "us") not in triples
    assert ("forexfactory", "economic", "global") in triples


@pytest.mark.unit
def test_expected_sources_holiday_aware_kr_drops_kr_sources():
    # 2025-01-01 New Year — XKRX closed (a Wednesday, not a weekend).
    triples = expected_sources_for_date(date(2025, 1, 1))
    assert ("dart", "disclosure", "kr") not in triples
    assert ("wisefn", "earnings", "kr") not in triples


@pytest.mark.unit
def test_expected_sources_regular_session_day_keeps_all():
    # 2025-07-07 Monday — both XNYS and XKRX open.
    triples = expected_sources_for_date(date(2025, 7, 7))
    assert ("finnhub", "earnings", "us") in triples
    assert ("dart", "disclosure", "kr") in triples
    assert ("wisefn", "earnings", "kr") in triples
