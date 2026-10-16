import asyncio
import inspect
from collections.abc import Callable
from types import SimpleNamespace
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


# ---------------------------------------------------------------------------
# ROB-1384 — US Finnhub failure path: degraded market_events fallback +
# closed-reason error payload (never "no earnings" on a failed lookup).
# ---------------------------------------------------------------------------


def _us_fallback_payload(count: int = 0) -> dict[str, Any]:
    row = {
        "symbol": "TMUS",
        "company_name": "T-Mobile US",
        "date": "2026-05-14",
        "hour": "bmo",
        "time_hint": "bmo",
        "eps_estimate": 2.4,
        "eps_actual": None,
        "revenue_estimate": None,
        "revenue_actual": None,
        "quarter": 2,
        "year": 2026,
        "status": "scheduled",
        "source": "finnhub",
        "source_event_id": "finnhub::TMUS::2026-05-14::2026::2",
        "source_url": None,
        "title": "T-Mobile US Q2 2026 earnings",
    }
    return {
        "symbol": "TMUS",
        "instrument_type": "equity_us",
        "market": "us",
        "source": "finnhub",
        "served_by": "market_events",
        "sources": ["finnhub"],
        "from_date": "2026-05-01",
        "to_date": "2026-05-31",
        "count": count,
        "earnings": [row] if count else [],
        "data_state": "degraded",
        "stale": False,
        "data_as_of": "2026-05-01",
    }


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_finnhub_failure_serves_degraded_fallback(monkeypatch):
    """Live fetch fails but stored market_events rows exist → degraded payload."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_market_events_us",
        AsyncMock(return_value=_us_fallback_payload(count=1)),
    )

    result = await financials.handle_get_earnings_calendar(
        "TMUS", "2026-05-01", "2026-05-31"
    )

    assert "error" not in result
    assert result["count"] == 1
    assert result.get("degraded") is True
    assert result.get("fetch_error") == "TimeoutError"
    assert result.get("fallback_source") == "market_events"
    assert result.get("served_by") == "market_events"
    assert result.get("data_state") == "degraded"
    assert result["stale"] is False
    assert result.get("provider_provenance") == [
        {
            "provider": "finnhub",
            "served_by": "market_events",
            "mode": "fallback",
            "status": "error",
            "error_code": "TimeoutError",
        }
    ]
    assert result["earnings"][0]["symbol"] == "TMUS"
    assert "not proof" in result["warning"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_finnhub_failure_empty_store_is_closed_error(
    monkeypatch,
):
    """Live fetch fails + no stored rows → closed error, never count=0."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_market_events_us",
        AsyncMock(return_value=_us_fallback_payload(count=0)),
    )

    result = await financials.handle_get_earnings_calendar(
        "TMUS", "2026-05-01", "2026-05-31"
    )

    assert result.get("error_code") == "earnings_lookup_failed"
    assert result.get("error_class") == "TimeoutError"
    assert result.get("data_state") == "missing"
    assert result["market"] == "us"
    assert result.get("fallback_source") == "market_events"
    assert result.get("fallback_status") == "empty"
    assert "earnings" not in result
    assert "count" not in result
    assert "not evidence" in result.get("suggestion")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_finnhub_failure_store_unavailable(monkeypatch):
    """Live fetch fails + fallback read itself fails → still a closed error."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_market_events_us",
        AsyncMock(return_value=None),
    )

    result = await financials.handle_get_earnings_calendar(
        "TMUS", "2026-05-01", "2026-05-31"
    )

    assert result.get("error_code") == "earnings_lookup_failed"
    assert result.get("error_class") == "TimeoutError"
    assert result.get("data_state") == "missing"
    assert result.get("fallback_status") == "unavailable"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_fallback_loader_raise_still_fail_soft(monkeypatch):
    """A fallback loader that raises must not escape the handler."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_finnhub",
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_market_events_us",
        AsyncMock(side_effect=RuntimeError("db gone")),
    )

    result = await financials.handle_get_earnings_calendar(
        "TMUS", "2026-05-01", "2026-05-31"
    )

    assert result.get("error_code") == "earnings_lookup_failed"
    assert result.get("fallback_status") == "unavailable"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_earnings_finnhub_failure_reads_real_market_events(
    db_session, monkeypatch
):
    """End-to-end: stored US row + failed live fetch → degraded payload."""
    from app.mcp_server.tooling.fundamentals import _financials as financials
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    partition = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=financials.datetime.date(2026, 5, 14),
    )
    await repo.mark_partition_running(partition)
    await repo.mark_partition_succeeded(partition, event_count=1)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "TMUS",
            "company_name": "T-Mobile US",
            "title": "T-Mobile US Q2 2026 earnings",
            "event_date": financials.datetime.date(2026, 5, 14),
            "time_hint": "bmo",
            "status": "scheduled",
            "source": "finnhub",
            "source_event_id": "finnhub::TMUS::2026-05-14::2026::2",
            "source_url": None,
            "fiscal_year": 2026,
            "fiscal_quarter": 2,
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
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )

    result = await financials.handle_get_earnings_calendar(
        symbol="tmus",  # lowercase — fallback filter must normalize
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert "error" not in result
    assert result["count"] == 1
    assert result.get("degraded") is True
    assert result.get("served_by") == "market_events"
    assert result["earnings"][0]["symbol"] == "TMUS"
    assert result["earnings"][0]["source"] == "finnhub"
    assert result.get("data_state") == "degraded"  # fresh partition, not stale
    assert result["stale"] is False
    assert result["data_as_of"] == partition.finished_at.date().isoformat()


# ---------------------------------------------------------------------------
# ROB-1384 round-1 fixes — B1 (empty TimeoutError laundering), B3 (symbol
# aliases into the store), S1 (out-of-window partition freshness).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_empty_timeout_error_message_never_blank(monkeypatch):
    """B1 — wait_for raises TimeoutError with str(exc)==''; the payload's
    error field must still be non-empty so nothing downstream misreads it."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    async def _real_timeout(*_args, **_kwargs):
        await asyncio.wait_for(asyncio.sleep(1), timeout=0.001)

    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", _real_timeout)
    monkeypatch.setattr(
        financials,
        "_fetch_earnings_calendar_market_events_us",
        AsyncMock(return_value=None),
    )

    result = await financials.handle_get_earnings_calendar(
        "BA", "2026-05-01", "2026-05-31"
    )

    assert result["error"]  # non-empty — falsy "" would launder to "live"
    assert result["error_code"] == "earnings_lookup_failed"
    assert result["error_class"] == "TimeoutError"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_earnings_fallback_normalizes_symbol_aliases(monkeypatch):
    """B3 — 'BRK-B'/'BRK/B'/' brk-b ' must reach the store as BRK.B."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    captured: dict[str, Any] = {}

    class FakeSvc:
        def __init__(self, db):
            pass

        async def list_for_range(self, start, end, **kwargs):
            captured["symbol"] = kwargs.get("symbol")
            return SimpleNamespace(events=[])

    class FakeCM:
        async def __aenter__(self):
            return SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
                )
            )

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda: FakeCM())
    monkeypatch.setattr(financials, "MarketEventsQueryService", FakeSvc)

    for alias in ("BRK-B", "BRK/B", " brk-b "):
        out = await financials._fetch_earnings_calendar_market_events_us(
            alias,
            financials.datetime.date(2026, 5, 1),
            financials.datetime.date(2026, 5, 31),
        )
        assert captured["symbol"] == "BRK.B", alias
        assert out is not None and out["count"] == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_us_freshness_query_scoped_to_requested_window(monkeypatch):
    """S1 — the ingestion-freshness SELECT must bound partition_date to the
    requested window so unrelated partitions can't refresh stale rows."""
    from app.mcp_server.tooling.fundamentals import _financials as financials

    captured: dict[str, Any] = {}

    class FakeDB:
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    out = await financials._us_earnings_ingestion_freshness(
        FakeDB(),
        financials.datetime.date(2026, 5, 1),
        financials.datetime.date(2026, 5, 31),
    )

    assert out == (None, True)
    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "partition_date >=" in sql
    assert "partition_date <=" in sql
    assert "2026-05-01" in sql
    assert "2026-05-31" in sql


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_earnings_fallback_resolves_dot_symbol_aliases(
    db_session, monkeypatch
):
    """B3 — stored 'BRK.B' rows must be reachable via 'BRK-B'/'BRK/B'."""
    from app.mcp_server.tooling.fundamentals import _financials as financials
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "BRK.B",
            "company_name": "Berkshire Hathaway",
            "title": "Berkshire Q2 2026 earnings",
            "event_date": financials.datetime.date(2026, 5, 14),
            "time_hint": "amc",
            "status": "scheduled",
            "source": "finnhub",
            "source_event_id": "finnhub::BRK.B::2026-05-14::2026::2",
            "source_url": None,
            "fiscal_year": 2026,
            "fiscal_quarter": 2,
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
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )

    for alias in ("BRK-B", "BRK/B", " brk-b "):
        result = await financials.handle_get_earnings_calendar(
            symbol=alias,
            from_date="2026-05-01",
            to_date="2026-05-31",
        )
        assert result.get("count") == 1, alias
        assert result.get("degraded") is True
        assert result["earnings"][0]["symbol"] == "BRK.B"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_fallback_freshness_ignores_out_of_window_partition(
    db_session, monkeypatch
):
    """S1 — a succeeded partition outside the requested window must not mark
    the stored rows fresh."""
    from app.mcp_server.tooling.fundamentals import _financials as financials
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    # Out-of-window partition: the December partition proves nothing about
    # the October-window rows.
    partition = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=financials.datetime.date(2026, 12, 15),
    )
    await repo.mark_partition_running(partition)
    await repo.mark_partition_succeeded(partition, event_count=0)
    await repo.upsert_event_with_values(
        {
            "category": "earnings",
            "market": "us",
            "symbol": "AAPL",
            "company_name": "Apple",
            "title": "Apple Q3 2026 earnings",
            "event_date": financials.datetime.date(2026, 5, 14),
            "time_hint": "amc",
            "status": "scheduled",
            "source": "finnhub",
            "source_event_id": "finnhub::AAPL::2026-05-14::2026::3",
            "source_url": None,
            "fiscal_year": 2026,
            "fiscal_quarter": 2,
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
        AsyncMock(side_effect=TimeoutError("finnhub stall")),
    )

    result = await financials.handle_get_earnings_calendar(
        symbol="AAPL",
        from_date="2026-05-01",
        to_date="2026-05-31",
    )

    assert result.get("count") == 1
    assert result.get("degraded") is True
    assert result["stale"] is True
    assert result["data_as_of"] is None
