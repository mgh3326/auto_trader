import inspect
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import DummySessionManager


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, name: str, description: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            self.descriptions[name] = description
            return func

        return decorator


@pytest.mark.asyncio
@pytest.mark.integration
async def test_kr_earnings_calendar_reads_market_events(db_session, monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "kr",
            "symbol": "005930",
            "company_name": "삼성전자",
            "title": "삼성전자 2026년 1분기 실적발표 예정",
            "event_date": financials.datetime.date(2026, 5, 13),
            "time_hint": "after_close",
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::005930::2026-05-13::2026::1",
            "source_url": None,
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
        },
        [],
    )
    await db_session.flush()

    monkeypatch.setattr(
        financials,
        "AsyncSessionLocal",
        lambda: DummySessionManager(db_session),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=AssertionError("KR path must not call Finnhub")),
    )

    result = await financials.handle_get_earnings_calendar(
        symbol="A005930",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result["instrument_type"] == "equity_kr"
    assert result["market"] == "kr"
    assert result["source"] == "market_events"
    assert result["sources"] == ["wisefn"]
    assert result["symbol"] == "005930"
    assert result["from_date"] == "2026-05-01"
    assert result["to_date"] == "2026-05-31"
    assert result["count"] == 1
    assert result["earnings"] == [
        {
            "symbol": "005930",
            "company_name": "삼성전자",
            "date": "2026-05-13",
            "hour": "after_close",
            "time_hint": "after_close",
            "eps_estimate": None,
            "eps_actual": None,
            "revenue_estimate": None,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
            "status": "scheduled",
            "source": "wisefn",
            "source_event_id": "wisefn::005930::2026-05-13::2026::1",
            "source_url": None,
            "title": "삼성전자 2026년 1분기 실적발표 예정",
        }
    ]
    assert "KR earnings calendar is backed by market_events" in result["warning"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_calendar_keeps_finnhub_path(monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials

    fake = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": "2026-05-01",
            "to_date": "2026-05-31",
            "count": 0,
            "earnings": [],
        }
    )
    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", fake)

    result = await financials.handle_get_earnings_calendar(
        symbol="AAPL",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result["source"] == "finnhub"
    fake.assert_awaited_once_with("AAPL", "2026-05-01", "2026-05-31")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_six_letter_us_symbol_routes_to_finnhub_path(monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials

    fake = AsyncMock(
        return_value={
            "symbol": "ABCDEF",
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": "2026-05-01",
            "to_date": "2026-05-31",
            "count": 0,
            "earnings": [],
        }
    )
    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", fake)
    monkeypatch.setattr(
        financials,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(
            AssertionError("US symbols must not open the KR market_events path")
        ),
    )

    result = await financials.handle_get_earnings_calendar(
        symbol="ABCDEF",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result["source"] == "finnhub"
    fake.assert_awaited_once_with("ABCDEF", "2026-05-01", "2026-05-31")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_calendar_defaults_to_from_date_plus_30(monkeypatch):
    from app.mcp_server.tooling.fundamentals import _financials as financials

    fake = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "source": "finnhub",
            "from_date": "2026-05-01",
            "to_date": "2026-05-31",
            "count": 0,
            "earnings": [],
        }
    )
    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", fake)

    result = await financials.handle_get_earnings_calendar(
        symbol="AAPL",
        from_date="2026-05-01",
    )

    assert result["source"] == "finnhub"
    fake.assert_awaited_once_with("AAPL", "2026-05-01", "2026-05-31")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_explicit_us_rejects_korean_symbol():
    from app.mcp_server.tooling.fundamentals import _financials as financials

    with pytest.raises(ValueError, match="Use market='kr' for Korean equities"):
        await financials.handle_get_earnings_calendar(
            symbol="005930",
            from_date="2026-05-01",
            to_date="2026-05-31",
            market="us",
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_crypto_symbol_still_rejected():
    from app.mcp_server.tooling.fundamentals import _financials as financials

    with pytest.raises(ValueError, match="not available for cryptocurrencies"):
        await financials.handle_get_earnings_calendar(symbol="KRW-BTC")


def test_registers_earnings_calendar_market_parameter() -> None:
    from app.mcp_server.tooling.fundamentals_registration import (
        register_fundamentals_tools,
    )

    mcp = DummyMCP()
    register_fundamentals_tools(cast(Any, mcp))

    tool = mcp.tools["get_earnings_calendar"]
    signature = inspect.signature(tool)

    assert list(signature.parameters) == ["symbol", "from_date", "to_date", "market"]
    assert signature.parameters["symbol"].default is None
    assert signature.parameters["from_date"].default is None
    assert signature.parameters["to_date"].default is None
    assert signature.parameters["market"].default is None
    assert "Korean" in mcp.descriptions["get_earnings_calendar"]
