"""hk 6968: publish / first-receipt times, symbol states and event keys.

Drives the real ``get_news`` and ``get_disclosures`` handlers. Only the
provider fetchers and the database session are replaced.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.mcp_server.tooling import analysis_tool_handlers as handlers
from app.mcp_server.tooling.fundamentals import _news as news_tool
from app.models.market_events import MarketEvent
from app.services import symbol_news_service
from app.services.naver_finance import NaverNewsFetchResult
from app.services.symbol_news_store import StoredSymbolNews

pytestmark = pytest.mark.unit

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "news_timing"
NOW = datetime.now(UTC)


def _golden(name: str):
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _session(db) -> MagicMock:
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context)


def _patch_store(monkeypatch, stored: list[StoredSymbolNews], excluded: int = 0):
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store, "upsert_feed_articles", AsyncMock()
    )
    monkeypatch.setattr(
        symbol_news_service.symbol_news_store,
        "load_symbol_news",
        AsyncMock(return_value=(stored, excluded)),
    )
    monkeypatch.setattr(symbol_news_service, "AsyncSessionLocal", _session(MagicMock()))


def _patch_no_store(monkeypatch):
    monkeypatch.setattr(
        symbol_news_service,
        "AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )


def _patch_naver(monkeypatch, items=None, *, skipped=None, error=None):
    fetch = AsyncMock(
        return_value=NaverNewsFetchResult(items=items or [], skipped=skipped or {}),
        side_effect=error,
    )
    monkeypatch.setattr(symbol_news_service.naver_finance, "fetch_stock_news", fetch)


def _patch_finnhub(monkeypatch, items=None, *, error=None):
    fetch = AsyncMock(return_value={"news": items or []}, side_effect=error)
    monkeypatch.setattr(symbol_news_service, "fetch_news_finnhub", fetch)


def _row(
    url: str,
    title: str,
    published_at: datetime | None,
    *,
    offset: str | None,
    precision: str | None,
    received: datetime | None,
    acquired: datetime | None = None,
    documents: dict | None = None,
) -> StoredSymbolNews:
    return StoredSymbolNews(
        article_id=abs(hash(url)),
        url=url,
        title=title,
        source="wire",
        published_at=published_at,
        relevance={"status": "pending"},
        fetched_at=(acquired or NOW).replace(tzinfo=None),
        published_offset=offset,
        published_precision=precision,
        first_received_at=received,
        source_document_ids=documents,
    )


def _naver_item(url: str, title: str, raw_time: str) -> dict:
    return {"title": title, "url": url, "datetime_raw": raw_time}


# ---------------------------------------------------------------------------
# Response goldens
# ---------------------------------------------------------------------------

KR_LIVE_URL = "https://n.news.naver.com/mnews/article/001/0000000001"
KR_FOLLOW_URL = "https://n.news.naver.com/mnews/article/015/0000000002"
KR_OLD_URL = "https://n.news.naver.com/mnews/article/009/0000000003"


@pytest.mark.asyncio
async def test_get_news_kr_golden(monkeypatch):
    _patch_naver(
        monkeypatch,
        [
            _naver_item(KR_LIVE_URL, "삼성전자, 2조원 공급계약 체결", "202611270915"),
            _naver_item(
                KR_FOLLOW_URL, "[속보] 삼성전자 2조원 공급계약 체결", "202611270931"
            ),
        ],
    )
    _patch_store(
        monkeypatch,
        [
            _row(
                KR_FOLLOW_URL,
                "[속보] 삼성전자 2조원 공급계약 체결",
                datetime(2026, 11, 27, 0, 31, tzinfo=UTC),
                offset="+09:00",
                precision="minute",
                received=datetime(2026, 11, 27, 0, 32, 5, tzinfo=UTC),
            ),
            _row(
                KR_LIVE_URL,
                "삼성전자, 2조원 공급계약 체결",
                datetime(2026, 11, 27, 0, 15, tzinfo=UTC),
                offset="+09:00",
                precision="minute",
                received=datetime(2026, 11, 27, 0, 16, 40, tzinfo=UTC),
            ),
            # Stored before the receipt clock and offset existed.
            _row(
                KR_OLD_URL,
                "삼성전자 주주총회 일정 안내",
                datetime(2026, 11, 20, 3, 0, tzinfo=UTC),
                offset=None,
                precision=None,
                received=None,
                acquired=NOW - timedelta(days=7),
            ),
        ],
    )

    payload = await news_tool.handle_get_news("005930", "kr")

    assert {
        "symbol_state": payload["symbol_state"],
        "news": payload["news"],
    } == _golden("get_news_kr.json")
    # The old backfill row ages ``data_state`` but not the lookup outcome.
    assert payload["data_state"] == "stale"


@pytest.mark.asyncio
async def test_get_news_us_golden(monkeypatch):
    url = "https://example.com/aapl-1"
    _patch_finnhub(
        monkeypatch,
        [
            {
                "title": "Apple wins large cloud contract",
                "url": url,
                "datetime": "2026-11-27T14:30:00+00:00",
                "datetime_raw": "1795789800",
                "source": "Reuters",
            }
        ],
    )
    _patch_store(
        monkeypatch,
        [
            _row(
                url,
                "Apple wins large cloud contract",
                datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
                offset="+00:00",
                precision="second",
                received=datetime(2026, 11, 27, 14, 33, 12, tzinfo=UTC),
            )
        ],
    )

    payload = await news_tool.handle_get_news("AAPL", "us")

    assert {
        "symbol_state": payload["symbol_state"],
        "news": payload["news"],
    } == _golden("get_news_us.json")


def _event(**values) -> MarketEvent:
    return MarketEvent(
        category="disclosure", market="kr", event_date=date(2026, 11, 26), **values
    )


def _disclosure_db(*results: list[MarketEvent]) -> MagicMock:
    db = MagicMock()
    answers = []
    for rows in results:
        answer = MagicMock()
        answer.scalars.return_value.all.return_value = rows
        answers.append(answer)
    db.execute = AsyncMock(side_effect=answers)
    return db


def _filing(receipt: str, name: str, day: str = "2026-11-26") -> dict:
    return {
        "date": day,
        "report_nm": name,
        "rcp_no": receipt,
        "corp_name": "삼성전자",
        "stock_code": "005930",
    }


@pytest.mark.asyncio
async def test_get_disclosures_golden(monkeypatch):
    receipts = ["20261126000101", "20261126000102", "20261126000103"]
    monkeypatch.setattr(
        handlers,
        "list_filings",
        AsyncMock(
            return_value={
                "success": True,
                "filings": [
                    _filing(receipts[0], "단일판매ㆍ공급계약체결"),
                    _filing(receipts[1], "자기주식취득결정"),
                    _filing(receipts[2], "기업설명회(IR)개최"),
                ],
            }
        ),
    )
    db = _disclosure_db(
        [
            # DART row: receipt time from the day list, our own receipt clock.
            _event(
                source="dart",
                source_event_id=receipts[0],
                symbol="005930",
                raw_payload_json={"rcept_dt": "2026-11-26T17:59:00"},
                first_received_at=datetime(2026, 11, 26, 9, 30, 2, tzinfo=UTC),
            ),
            # Stored before the DART receipt clock existed.
            _event(
                source="dart",
                source_event_id=receipts[1],
                symbol="005930",
                raw_payload_json={"rcept_dt": "20261126"},
            ),
        ],
        [
            # The Naver relay received the first filing earlier than we did via DART.
            _event(
                source="naver_disclosure",
                source_event_id="n-1",
                symbol="005930",
                raw_payload_json={"dart_rcept_no": receipts[0]},
                provider_created_at=datetime(2026, 11, 26, 8, 59, 31, tzinfo=UTC),
                provider_created_raw="2026-11-26T17:59:31",
                first_received_at=datetime(2026, 11, 26, 9, 1, 0, tzinfo=UTC),
            ),
            _event(
                source="naver_disclosure",
                source_event_id="n-2",
                symbol="005930",
                raw_payload_json={"dart_rcept_no": receipts[1]},
                provider_created_at=datetime(2026, 11, 26, 7, 5, 9, tzinfo=UTC),
                provider_created_raw="2026-11-26T16:05:09",
                first_received_at=datetime(2026, 11, 26, 7, 6, 0, tzinfo=UTC),
            ),
        ],
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", _session(db))

    payload = await handlers.get_disclosures_impl("005930")

    assert payload == _golden("get_disclosures.json")
    statements = [
        str(
            call.args[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": False},
            )
        )
        for call in db.execute.await_args_list
    ]
    assert len(statements) == 2
    assert "market_events.symbol IN" in statements[1]
    assert "raw_payload_json ->>" in statements[1]


# ---------------------------------------------------------------------------
# Symbol states
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("market", ["kr", "us"])
async def test_news_fetch_failure_and_empty_result_differ(monkeypatch, market):
    symbol = "005930" if market == "kr" else "AAPL"
    patch = _patch_naver if market == "kr" else _patch_finnhub
    _patch_store(monkeypatch, [])

    patch(monkeypatch, error=TimeoutError())
    failed = await news_tool.handle_get_news(symbol, market)
    patch(monkeypatch)
    empty = await news_tool.handle_get_news(symbol, market)

    assert failed["symbol_state"] == "fetch_failed"
    assert failed["error_code"] == "news_lookup_failed"
    assert "news" not in failed
    assert empty["symbol_state"] == "no_items"
    assert empty["news"] == []
    assert "error_code" not in empty


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "age", "expected"),
    [
        (TimeoutError(), timedelta(minutes=1), "stale_copy"),
        (None, timedelta(hours=9), "stale_copy"),
        (None, timedelta(minutes=1), "ok"),
    ],
)
async def test_news_stored_rows_state(monkeypatch, error, age, expected):
    _patch_naver(monkeypatch, error=error)
    _patch_store(
        monkeypatch,
        [
            _row(
                KR_OLD_URL,
                "삼성전자 주주총회 일정 안내",
                datetime(2026, 11, 20, 3, 0, tzinfo=UTC),
                offset="+09:00",
                precision="minute",
                received=None,
                acquired=NOW - age,
            )
        ],
    )

    payload = await news_tool.handle_get_news("005930", "kr")

    assert payload["symbol_state"] == expected


@pytest.mark.asyncio
async def test_kr_unreadable_rows_are_a_failure_not_no_news(monkeypatch):
    _patch_naver(monkeypatch, skipped={"invalid_datetime": 3})
    _patch_store(monkeypatch, [])

    payload = await news_tool.handle_get_news("005930", "kr")

    assert payload["symbol_state"] == "fetch_failed"
    assert payload["parser_skips"] == {"invalid_datetime": 3}


@pytest.mark.asyncio
async def test_us_unreadable_rows_are_a_failure_not_no_news(monkeypatch):
    _patch_finnhub(monkeypatch, [{"title": "", "url": "https://example.com/x"}])
    _patch_store(monkeypatch, [])

    payload = await news_tool.handle_get_news("AAPL", "us")

    assert payload["symbol_state"] == "fetch_failed"
    assert payload["parser_skips"] == {"missing_title": 1}


@pytest.mark.asyncio
async def test_store_down_keeps_publish_time_and_unknown_receipt(monkeypatch):
    item = _naver_item(KR_LIVE_URL, "삼성전자, 2조원 공급계약 체결", "202611270915")
    item["datetime"] = "2026-11-27T09:15:00+09:00"
    item["rcept_no"] = "20261127000001"
    _patch_naver(monkeypatch, [item])
    _patch_no_store(monkeypatch)

    payload = await news_tool.handle_get_news("005930", "kr")

    [served] = payload["news"]
    assert payload["symbol_state"] == "ok"
    assert served["published_at"] == "2026-11-27T09:15:00+09:00"
    assert served["published_precision"] == "minute"
    assert served["first_received_at"] is None
    assert served["event_key"] == "ev1:kr:005930:doc:dart:20261127000001"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "state"),
    [
        (
            {"success": False, "error": "DART status 020", "filings": []},
            "fetch_failed",
        ),
        ({"success": True, "filings": []}, "no_items"),
    ],
)
async def test_disclosure_failure_and_empty_result_differ(monkeypatch, listing, state):
    monkeypatch.setattr(handlers, "list_filings", AsyncMock(return_value=listing))

    payload = await handlers.get_disclosures_impl("005930")

    assert payload["symbol_state"] == state


@pytest.mark.asyncio
async def test_disclosure_receipt_read_failure_keeps_filings(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "list_filings",
        AsyncMock(
            return_value={
                "success": True,
                "filings": [_filing("20261126000101", "자기주식취득결정")],
            }
        ),
    )
    monkeypatch.setattr(
        "app.core.db.AsyncSessionLocal",
        MagicMock(side_effect=RuntimeError("db down")),
    )

    payload = await handlers.get_disclosures_impl("삼성전자")

    [filing] = payload["filings"]
    assert payload["symbol_state"] == "ok"
    assert payload["receipt_lookup"] == "failed"
    assert filing["published_at"] == "2026-11-26"
    assert filing["published_at_source"] == "dart_list_date"
    assert filing["first_received_at"] is None
    assert filing["event_key"] == "ev1:kr:005930:doc:dart:20261126000101"


@pytest.mark.asyncio
async def test_article_citing_a_receipt_shares_the_filing_key(monkeypatch):
    receipt = "20261126000101"
    _patch_naver(monkeypatch)
    _patch_store(
        monkeypatch,
        [
            _row(
                KR_LIVE_URL,
                "삼성전자, 공급계약 공시",
                datetime(2026, 11, 26, 10, 0, tzinfo=UTC),
                offset="+09:00",
                precision="minute",
                received=None,
                documents={"rcept_no": [receipt]},
            )
        ],
    )
    monkeypatch.setattr(
        handlers,
        "list_filings",
        AsyncMock(
            return_value={
                "success": True,
                "filings": [_filing(receipt, "단일판매ㆍ공급계약체결")],
            }
        ),
    )
    monkeypatch.setattr(
        "app.core.db.AsyncSessionLocal", _session(_disclosure_db([], []))
    )

    news = await news_tool.handle_get_news("005930", "kr")
    disclosures = await handlers.get_disclosures_impl("005930")

    assert news["news"][0]["event_key"] == disclosures["filings"][0]["event_key"]
