# tests/services/test_symbol_news_service.py
"""Tests for app.services.symbol_news_service (ROB-423 PR1)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import symbol_news_service


def _stored(article_id: int, url: str, title: str, status: str = "pending"):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="매일경제",
        published_at=datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": status,
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
        fetched_at=datetime(2026, 6, 10, 8, 55),
    )


def _stored_us(article_id: int, url: str, title: str, status: str = "pending"):
    from app.services.symbol_news_store import StoredSymbolNews

    return StoredSymbolNews(
        article_id=article_id,
        url=url,
        title=title,
        source="Reuters",
        published_at=datetime(2026, 6, 10, 9, 0),
        relevance={
            "status": status,
            "relationship": None,
            "relevance": None,
            "price_relevance": None,
            "score": None,
            "reason": None,
            "judged_by": None,
            "judged_at": None,
            "hints": None,
        },
        summary="summary text",
        fetched_at=datetime(2026, 6, 10, 8, 55),
    )


def _patch_store(monkeypatch, *, stored, excluded_count=0):
    upsert = AsyncMock()
    load = AsyncMock(return_value=(stored, excluded_count))
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_feed_articles", upsert
    )
    monkeypatch.setattr(symbol_news_service.symbol_news_store, "load_symbol_news", load)
    # AsyncSessionLocal() 컨텍스트를 가짜 세션으로 대체
    fake_session = MagicMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )
    return upsert, load


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_returns_normalized_articles_with_external_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = [
        {
            "title": "삼성전자 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=123&office_id=001",
            "source": "한국경제",
            "datetime": "2026-05-05T09:30",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    _patch_store(
        monkeypatch,
        stored=[_stored(123, raw[0]["url"], raw[0]["title"])],
    )

    result = await symbol_news_service.fetch_symbol_news("005930", "kr", limit=20)

    assert result.status == "ok"
    assert result.provider == "naver"
    art = result.articles[0]
    assert art.symbol == "005930"
    assert art.market == "kr"
    assert art.title == "삼성전자 호실적"
    assert art.external_article_id == "001:123"
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_persists_then_serves_db_state(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 D2SF 투자",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=1&office_id=009",
            "source": "매일경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    upsert, _ = _patch_store(
        monkeypatch,
        stored=[_stored(1, raw[0]["url"], raw[0]["title"])],
        excluded_count=3,
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.excluded_count == 3
    assert result.degraded is False
    assert result.fetched_at is not None
    assert result.cache_hit is False
    assert result.fallback_source is None
    assert result.provider_provenance == [
        {
            "provider": "naver",
            "served_by": "naver",
            "mode": "live",
            "status": "ok",
            "error_code": None,
        }
    ]
    upsert.assert_awaited_once()
    art = result.articles[0]
    assert art.provider_metadata["relevance"]["status"] == "pending"
    # 현재 fetch 윈도우에 있던 기사는 원본 source_item 보존
    assert art.provider_metadata["source_item"] == raw[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_fetched_at_is_stamped_only_after_provider_success(
    monkeypatch,
) -> None:
    raw = [
        {
            "title": "네이버 원천 시각",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=2&office_id=009",
            "source": "매일경제",
            "datetime": "2026-06-10",
        }
    ]
    acquired_at = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)
    events: list[str] = []

    async def provider_fetch(symbol: str, *, limit: int):
        events.append("provider_returned")
        return raw

    def clock() -> datetime:
        events.append("timestamp_created")
        return acquired_at

    monkeypatch.setattr(symbol_news_service.naver_finance, "fetch_news", provider_fetch)
    monkeypatch.setattr(symbol_news_service, "_utcnow", clock)
    _patch_store(
        monkeypatch,
        stored=[_stored(2, raw[0]["url"], raw[0]["title"])],
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert events == ["provider_returned", "timestamp_created"]
    assert result.fetched_at == acquired_at
    assert result.articles[0].fetched_at == acquired_at


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_row_outside_window_gets_reconstructed_source_item(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    _patch_store(
        monkeypatch, stored=[_stored(7, "https://x/old-article", "지난주 네이버 기사")]
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    item = result.articles[0].provider_metadata["source_item"]
    assert item["title"] == "지난주 네이버 기사"
    assert item["url"] == "https://x/old-article"
    assert "datetime" in item and "source" in item


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_fetch_failure_serves_db_cache_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    monkeypatch.setattr(
        symbol_news_service,
        "_utcnow",
        lambda: pytest.fail("failed provider must not mint fetched_at"),
    )
    _patch_store(monkeypatch, stored=[_stored(1, "https://x/cached", "캐시 기사")])

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"
    assert result.degraded is True
    assert result.fetch_error == "RuntimeError"
    assert result.articles[0].title == "캐시 기사"
    assert result.fetched_at == datetime(2026, 6, 10, 8, 55, tzinfo=UTC)
    assert result.cache_hit is True
    assert result.fallback_source == "news_articles"
    assert result.provider_provenance == [
        {
            "provider": "naver",
            "served_by": "news_articles",
            "mode": "fallback",
            "status": "error",
            "error_code": "RuntimeError",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_db_failure_degrades_to_on_demand_pending(monkeypatch) -> None:
    raw = [
        {
            "title": "네이버 호실적",
            "url": "https://finance.naver.com/item/news_read.naver?article_id=9&office_id=001",
            "source": "한국경제",
            "datetime": "2026-06-10",
        }
    ]
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=raw)
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr", limit=10)

    assert result.status == "ok"  # 도구는 DB 때문에 죽지 않는다
    assert result.articles[0].provider_metadata["relevance"]["status"] == "pending"
    assert result.excluded_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_both_fetch_and_db_down_is_error(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("naver down")),
    )
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )
    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_finnhub_preserves_source_item_and_sentiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "symbol": "AAPL",
        "market": "us",
        "source": "finnhub",
        "count": 1,
        "news": [
            {
                "title": "Apple beats earnings",
                "source": "Reuters",
                "datetime": "2026-05-05T12:00:00",
                "url": "https://x/aapl-1",
                "summary": "strong quarter",
                "sentiment": "positive",
                "related": "AAPL,MSFT",
            }
        ],
    }
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=payload),
    )
    _patch_store(
        monkeypatch,
        stored=[_stored_us(1, "https://x/aapl-1", "Apple beats earnings")],
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.provider == "finnhub"
    art = result.articles[0]
    assert art.external_article_id is not None  # url hash
    assert art.related_symbols == ["AAPL", "MSFT"]
    assert art.provider_metadata["sentiment"] == "positive"
    assert art.provider_metadata["source_item"] == payload["news"][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_provider_result_is_status_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # KR case with empty fetch and empty DB
    monkeypatch.setattr(
        symbol_news_service.naver_finance, "fetch_news", AsyncMock(return_value=[])
    )
    _patch_store(monkeypatch, stored=[])
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "empty"
    assert result.returned_count == 0
    assert result.articles == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_error_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If fetch fails but DB is empty, it's an error status
    monkeypatch.setattr(
        symbol_news_service.naver_finance,
        "fetch_news",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    _patch_store(monkeypatch, stored=[])
    result = await symbol_news_service.fetch_symbol_news("005930", "kr")
    assert result.status == "error"
    assert result.error_code == "RuntimeError"
    assert result.articles == []
    assert result.fetched_at is None
    assert result.cache_hit is False
    assert result.fallback_source is None
    assert result.provider_provenance[0]["mode"] == "none"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsupported_market_is_unavailable() -> None:
    result = await symbol_news_service.fetch_symbol_news("FOO", "jp")
    assert result.status == "unavailable"
    assert result.error_code == "unsupported_market"


# ---------------------------------------------------------------------------
# ROB-506 — async judgment enqueue from the KR persist path
# ---------------------------------------------------------------------------


def _patch_naver(monkeypatch, items):
    async def fake_fetch(symbol, limit=20):
        return items

    monkeypatch.setattr(symbol_news_service.naver_finance, "fetch_news", fake_fetch)


_RAW_ITEM = {
    "title": "네이버 신규 투자",
    "url": "https://x/rob506-enqueue",
    "source": "매일경제",
    "datetime": "2026-06-10T09:00:00",
}


def _patch_store_with_insert_count(monkeypatch, *, stored, new_links: int):
    """upsert가 신규 link 수(int)를 반환하는 ROB-506 계약으로 store를 fake."""

    async def upsert(db, market, symbol, items, **kwargs):
        return new_links

    async def load(db, symbol, market, limit):
        return stored, 0

    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_feed_articles", upsert
    )
    monkeypatch.setattr(symbol_news_service.symbol_news_store, "load_symbol_news", load)
    fake_session = MagicMock()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_new_pending_enqueues_judgment_when_flag_on(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_awaited_once_with(market="kr", symbol="035420", dry_run=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_enqueue_failure_is_fail_open(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock(side_effect=RuntimeError("redis down"))
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"  # enqueue 실패가 get_news를 죽이지 않음
    assert result.returned_count == 1
    kiq.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_enqueue_when_flag_off(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    # flag 기본 off — 명시적으로 고정
    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", False)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"])],
        new_links=1,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_no_enqueue_when_no_new_pending(monkeypatch) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    _patch_naver(monkeypatch, [_RAW_ITEM])
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored(1, _RAW_ITEM["url"], _RAW_ITEM["title"], status="confirmed")],
        new_links=0,  # 전부 기존 link — 신규 pending 없음
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("035420", "kr")
    assert result.status == "ok"
    kiq.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_existing_pending_reenqueues_judgment_when_flag_on(
    monkeypatch,
) -> None:
    from app.core.config import settings
    from app.tasks import news_relevance_judgment_tasks

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    _patch_store_with_insert_count(
        monkeypatch,
        stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")],
        new_links=0,
    )
    kiq = AsyncMock()
    monkeypatch.setattr(
        news_relevance_judgment_tasks.news_relevance_judge_pending, "kiq", kiq
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.articles[0].provider_metadata["relevance"]["status"] == "pending"
    kiq.assert_awaited_once_with(market="us", symbol="AAPL", dry_run=False)


_FINNHUB_RAW = {
    "symbol": "AAPL",
    "market": "us",
    "source": "finnhub",
    "count": 1,
    "news": [
        {
            "title": "Apple beats",
            "source": "Reuters",
            "datetime": "2026-06-10T09:00:00",
            "url": "https://r/apple-beats",
            "summary": "summary text",
            "sentiment": {"score": 0.7},
            "related": "AAPL",
        }
    ],
}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_persists_then_serves_db_state(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    upsert, load = _patch_store(
        monkeypatch,
        stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")],
        excluded_count=2,
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.provider == "finnhub"
    assert result.excluded_count == 2
    assert result.degraded is False
    upsert.assert_awaited_once()
    args, kwargs = upsert.await_args
    assert args[1] == "us"  # market
    assert kwargs["feed_source"] == "finnhub_company_news"
    # 신선 fetch 항목은 sentiment 포함 원본 source_item 보존
    art = result.articles[0]
    assert art.provider_metadata["source_item"]["sentiment"] == {"score": 0.7}
    assert art.provider_metadata["relevance"]["status"] == "pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_fetch_failure_serves_db_cache_degraded(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(side_effect=TimeoutError()),
    )
    _patch_store(
        monkeypatch, stored=[_stored_us(1, "https://r/cached", "Cached headline")]
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    assert result.degraded is True
    assert result.fetch_error == "TimeoutError"
    assert result.articles[0].title == "Cached headline"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_fetch_failure_with_empty_db_is_error(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(side_effect=TimeoutError()),
    )
    _patch_store(monkeypatch, stored=[])

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "error"
    assert result.error_code == "TimeoutError"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_db_failure_degrades_to_on_demand_pending(monkeypatch) -> None:
    monkeypatch.setattr(
        symbol_news_service,
        "fetch_news_finnhub",
        AsyncMock(return_value=_FINNHUB_RAW),
    )
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store,
        "upsert_feed_articles",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    fake_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        symbol_news_service, "AsyncSessionLocal", MagicMock(return_value=fake_cm)
    )

    result = await symbol_news_service.fetch_symbol_news("AAPL", "us", limit=10)

    assert result.status == "ok"
    relevance = result.articles[0].provider_metadata["relevance"]
    assert relevance["status"] == "pending"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_crypto_uses_general_feed_source(monkeypatch) -> None:
    raw = {**_FINNHUB_RAW, "market": "crypto"}
    monkeypatch.setattr(
        symbol_news_service, "fetch_news_finnhub", AsyncMock(return_value=raw)
    )
    upsert, _ = _patch_store(
        monkeypatch, stored=[_stored_us(1, "https://r/apple-beats", "Apple beats")]
    )

    result = await symbol_news_service.fetch_symbol_news("KRW-BTC", "crypto", limit=10)

    assert result.status == "ok"
    _, kwargs = upsert.await_args
    assert kwargs["feed_source"] == "finnhub_general_news"
