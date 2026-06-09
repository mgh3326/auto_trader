# tests/test_catalyst_query_service.py
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.market_events.catalyst.query_service import CatalystQueryService

KST_NOW = dt.datetime(2026, 6, 2, 9, 0)
TODAY = KST_NOW.date()


def _row(symbol, category, *, days, title="t", raw_payload=None, source="manual"):
    return SimpleNamespace(
        symbol=symbol,
        category=category,
        title=title,
        event_date=TODAY + dt.timedelta(days=days),
        source=source,
        raw_payload_json=raw_payload,
        importance=None,
    )


def _service_with_rows(rows):
    async def reader(*, categories, from_date, to_date, market, symbols):
        out = [
            r
            for r in rows
            if r.category in categories
            and from_date <= r.event_date <= to_date
            and (symbols is None or r.symbol in symbols)
        ]
        return out

    return CatalystQueryService(session=None, reader=reader)


@pytest.mark.asyncio
async def test_within_days_filter_and_days_until_and_polarity():
    svc = _service_with_rows(
        [
            _row("035420", "conference", days=3),  # in range, positive
            _row("005930", "lockup_expiry", days=30),  # out of range (>7)
        ]
    )
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert [e.symbol for e in out.rows] == ["035420"]
    e = out.rows[0]
    assert e.category == "conference"
    assert e.days_until == 3
    assert e.polarity == "positive"
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_raw_payload_polarity_override():
    svc = _service_with_rows(
        [_row("035420", "conference", days=1, raw_payload={"impact_hint": "negative"})]
    )
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert out.rows[0].polarity == "negative"


@pytest.mark.asyncio
async def test_symbols_filter():
    svc = _service_with_rows(
        [
            _row("035420", "conference", days=2),
            _row("005930", "conference", days=2),
        ]
    )
    out = await svc.get_upcoming_catalysts(
        symbols=["035420"], within_days=7, now=KST_NOW
    )
    assert [e.symbol for e in out.rows] == ["035420"]


@pytest.mark.asyncio
async def test_no_rows_unavailable():
    svc = _service_with_rows([])
    out = await svc.get_upcoming_catalysts(within_days=7, now=KST_NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_upcoming_catalysts"


@pytest.mark.asyncio
async def test_default_orm_reader_queries_catalyst_categories():
    # default reader(세션 직접) 커버: AsyncMock 세션이 catalyst 행을 반환.
    row = _row("035420", "conference", days=1)
    scalars = MagicMock()
    scalars.all.return_value = [row]
    result = MagicMock()
    result.scalars.return_value = scalars
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    svc = CatalystQueryService(session=session)  # reader 없음 → default ORM reader
    out = await svc.get_upcoming_catalysts(
        symbols=["035420"], within_days=7, now=KST_NOW
    )
    assert out.rows[0].symbol == "035420"
    assert session.execute.await_count == 1
