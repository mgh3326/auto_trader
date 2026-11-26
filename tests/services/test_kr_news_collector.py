from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services import kr_news_collector as collector
from app.services.naver_news_urls import canonicalize_naver_news_url

FIXTURE = Path("tests/fixtures/kr_news/naver_aggregate_home_response.json")


def test_kst_timestamp_raw_and_precision() -> None:
    parsed = collector.parse_publish_time("2026-11-25 09:41")
    assert parsed.raw == "2026-11-25 09:41"
    assert parsed.value == datetime(2026, 11, 25, 0, 41, tzinfo=UTC)
    assert parsed.precision == "minute"
    assert parsed.reason is None


def test_date_only_and_unparseable_precision() -> None:
    date_only = collector.parse_publish_time("2026-11-25")
    assert date_only.value == datetime(2026, 11, 24, 15, 0, tzinfo=UTC)
    assert date_only.precision == "date"
    invalid = collector.parse_publish_time("not-a-date")
    assert invalid.raw == "not-a-date"
    assert invalid.value is None
    assert invalid.reason == "unparseable"


def test_naver_article_url_variants_share_canonical_identity() -> None:
    assert (
        canonicalize_naver_news_url(
            "https://n.news.naver.com/mnews/article/001/0001234?sid=101"
        )
        == "https://n.news.naver.com/article/001/0001234"
    )


@pytest.mark.asyncio
async def test_aggregate_parser_keeps_all_sections_and_canonical_url_dedupes() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    request = {}

    async def get_json(url, params):
        request.update(url=url, params=params)
        return payload

    rows = await collector.fetch_aggregate_news(get_json=get_json)
    assert [row["section"] for row in rows] == [
        "flashNews",
        "mainNews",
        "rankingNews",
        "newsFocus",
    ]
    assert len(rows) == 4
    assert rows[0]["published"].precision == "second"
    assert rows[2]["published"].reason == "unparseable"
    assert rows[3]["published"].precision == "second"
    assert rows[3]["published"].value == datetime(2026, 6, 10, 22, 43, 8, tzinfo=UTC)
    assert rows[3]["url"] == "https://n.news.naver.com/article/009/0005692236"
    assert request["url"] == collector.AGGREGATE_URL
    assert request["params"] == {
        "flashNewsSize": 100,
        "mainNewsSize": 100,
        "rankingNewsSize": 100,
        "focusSize": 100,
    }


def test_symbol_set_unions_and_reports_cut_list() -> None:
    result = collector.build_symbol_set(
        holdings=["005930", "000660"],
        watch=["000660", "035420"],
        first_captures=["035420", "051910"],
        cap=3,
    )
    assert result.symbols == ("005930", "000660", "035420")
    assert result.origins["000660"] == ("holding", "watch")
    assert result.cut_list == ({"symbol": "051910", "origin": "first_capture"},)


@pytest.mark.asyncio
async def test_failed_aggregate_is_error_and_item_source_still_stores(
    monkeypatch,
) -> None:
    stored = []

    async def broken_aggregate():
        raise TimeoutError

    async def item_fetcher(_symbol, _limit):
        return [
            {
                "url": "https://n.news.naver.com/article/001/9",
                "title": "삼성전자 소식",
                "source": "연합뉴스",
                "datetime": "2026-11-25T09:41:00+09:00",
                "datetime_raw": "202611250941",
            }
        ]

    async def fake_store(_db, **kwargs):
        stored.extend(kwargs["items"])
        return 1, 1

    monkeypatch.setattr(collector, "_store_items", fake_store)
    report = await collector.collect_kr_news(
        db=object(),
        now=datetime(2026, 11, 25, 1, tzinfo=UTC),
        aggregate_fetcher=broken_aggregate,
        item_fetcher=item_fetcher,
        explicit_symbols=["005930"],
    )
    by_source = {row.source: row for row in report.sources}
    assert by_source[collector.AGGREGATE_FEED_SOURCE].status == "error"
    assert by_source["naver_item_news:005930"].new == 1
    assert stored[0]["published"].raw == "202611250941"


def test_migration_is_additive_and_does_not_backfill() -> None:
    migration = Path("alembic/versions/20261126_hk6674_kr_news_timing.py").read_text()
    assert "op.add_column(" in migration
    assert '"news_articles"' in migration
    assert "DateTime(timezone=True)" in migration
    assert "UPDATE news_articles" not in migration
    assert "first_received_at" in migration
