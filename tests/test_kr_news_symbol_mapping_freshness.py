from datetime import UTC, datetime, timedelta

import pytest

from app.services.kr_news_symbol_mapping.freshness import derive_freshness

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


@pytest.mark.unit
def test_no_articles_is_unavailable():
    f = derive_freshness([], now=NOW, ttl_hours=24)
    assert f.overall == "unavailable"
    assert f.latest_as_of is None
    assert f.stale_reason == "no_mapped_news"


@pytest.mark.unit
def test_recent_is_fresh():
    f = derive_freshness([NOW - timedelta(hours=2)], now=NOW, ttl_hours=24)
    assert f.overall == "fresh"
    assert f.latest_as_of == NOW - timedelta(hours=2)
    assert f.stale_reason is None


@pytest.mark.unit
def test_older_than_ttl_is_stale():
    f = derive_freshness(
        [NOW - timedelta(hours=30), NOW - timedelta(hours=48)], now=NOW, ttl_hours=24
    )
    assert f.overall == "stale"
    assert f.latest_as_of == NOW - timedelta(hours=30)  # 가장 신선한 것 기준
    assert f.stale_reason == "older_than_ttl"
