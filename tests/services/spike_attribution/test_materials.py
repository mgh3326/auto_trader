"""Clock-registry behaviour: the reader may never guess a timezone."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services.spike_attribution.attribute import build_attribution
from app.services.spike_attribution.contract import MaterialAvailability, SpikeMaterials
from app.services.spike_attribution.materials import (
    DAILY_TABLE_BY_MARKET,
    FEED_CLOCKS,
    feed_clock,
)
from tests.services.spike_attribution.test_attribute import make_event


def test_kr_aggregate_feed_is_a_confirmed_exact_kst_clock() -> None:
    clock = feed_clock("http_naver_stock_aggregate")
    assert clock.confirmed is True
    assert clock.precision == "exact"
    assert str(clock.tz) == "Asia/Seoul"
    assert clock.basis


@pytest.mark.parametrize(
    "feed",
    [
        "browser_naver_research",
        "browser_naver_research_company",
        "browser_naver_research_industry",
        "naver_item_news",
    ],
)
def test_date_only_kr_feeds_are_never_treated_as_eligible(feed: str) -> None:
    clock = feed_clock(feed)
    assert clock.precision == "date_only"
    assert clock.confirmed is False


@pytest.mark.parametrize(
    "feed",
    ["rss_yahoo_finance_topstories", "rss_cnbc_us_markets", "finnhub_company_news"],
)
def test_us_feed_clocks_stay_unconfirmed_until_someone_verifies_them(
    feed: str,
) -> None:
    clock = feed_clock(feed)
    assert clock.confirmed is False
    assert clock.tz is None


def test_an_unregistered_feed_fails_closed() -> None:
    clock = feed_clock("some_new_feed_nobody_registered")
    assert clock.confirmed is False
    assert clock.tz is None
    assert clock.basis == "feed_source_not_in_clock_registry"


def test_missing_feed_source_fails_closed() -> None:
    assert feed_clock(None).confirmed is False


def test_every_registered_clock_declares_its_basis() -> None:
    for feed, clock in FEED_CLOCKS.items():
        assert clock.basis, feed
        if clock.confirmed:
            assert clock.tz is not None, feed
            assert clock.precision == "exact", feed


def test_daily_table_mapping_is_a_closed_literal_set() -> None:
    # The table name is interpolated into SQL, so it must never come from input.
    assert set(DAILY_TABLE_BY_MARKET) == {"kr", "us"}
    for table in DAILY_TABLE_BY_MARKET.values():
        assert table.replace("_", "").isalnum()


def test_zero_row_material_reads_differently_from_unreadable_material() -> None:
    record = build_attribution(
        event=make_event(),
        materials=SpikeMaterials(
            evidence=(),
            availability=(
                MaterialAvailability(
                    material="news", available=True, detail={"rows": 0}
                ),
                MaterialAvailability(
                    material="flow", available=False, reason="unavailable_t_plus_1"
                ),
            ),
        ),
    )
    reason = record.unattributed_reason or ""
    assert "조회됐으나 행 0" in reason
    assert "news" in reason
    assert "조회 불가 재료 flow" in reason


def test_lookahead_covers_the_longest_pre_registered_window() -> None:
    from app.services.spike_attribution.scoring import WINDOWS_TRADING_DAYS
    from scripts.attribute_daily_spikes import LOOKAHEAD_DAYS

    # Calendar days must comfortably exceed trading days for the longest window.
    assert LOOKAHEAD_DAYS > max(WINDOWS_TRADING_DAYS) * 1.5
    assert isinstance(dt.timedelta(days=LOOKAHEAD_DAYS), dt.timedelta)
