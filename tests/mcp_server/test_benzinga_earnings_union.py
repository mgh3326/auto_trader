"""US earnings reader: Benzinga rows are a union, Finnhub stays authoritative."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.fundamentals import _financials as financials

START = datetime.date(2026, 10, 1)
END = datetime.date(2026, 11, 30)

FINNHUB_PAYLOAD = {
    "symbol": None,
    "instrument_type": "equity_us",
    "source": "finnhub",
    "from_date": START.isoformat(),
    "to_date": END.isoformat(),
    "count": 1,
    "earnings": [
        {
            "symbol": "AAPL",
            "date": "2026-10-29",
            "hour": "amc",
            "eps_estimate": 1.5,
            "eps_actual": None,
            "revenue_estimate": None,
            "revenue_actual": None,
            "quarter": 4,
            "year": 2026,
        }
    ],
}


def _event(symbol: str, event_date: datetime.date, *, hint: str = "after_close"):
    return SimpleNamespace(
        symbol=symbol,
        company_name=None,
        title=f"{symbol} earnings release",
        event_date=event_date,
        time_hint=hint,
        status="released",
        source="benzinga",
        source_event_id=f"benzinga:earnings:{symbol}",
        source_url=None,
        fiscal_year=2026,
        fiscal_quarter=4,
        values=[
            SimpleNamespace(
                metric_name="eps",
                actual=2.18,
                forecast=2.10,
                previous=None,
                revised_previous=None,
                unit="USD",
                surprise=None,
                surprise_pct=None,
                released_at=None,
            )
        ],
    )


class _FakeQueryService:
    def __init__(self, events=None, error=None) -> None:
        self._events = events or []
        self._error = error
        self.calls: list[dict] = []

    def __call__(self, _db):
        return self

    async def list_for_range(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return SimpleNamespace(events=self._events)


@pytest.fixture
def no_db(monkeypatch):
    """Record any attempt to open a session and fail at teardown.

    The merge helper is fail-soft — it catches store errors and returns the
    Finnhub payload — so raising inside the call would be swallowed and the
    test would pass anyway. Counting attempts and asserting after the call
    makes a gate regression visible.
    """
    attempts: list[tuple] = []

    def _open(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("a database session was opened")

    monkeypatch.setattr(financials, "AsyncSessionLocal", _open)
    yield attempts
    assert attempts == [], "a database session was opened with the gate off"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_returns_the_finnhub_payload_unchanged(monkeypatch, no_db):
    monkeypatch.delenv("BENZINGA_CALENDARS_ENABLED", raising=False)
    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)
    assert out == FINNHUB_PAYLOAD
    assert "benzinga_union" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_keeps_the_tool_output_identical(monkeypatch, no_db):
    monkeypatch.delenv("BENZINGA_CALENDARS_ENABLED", raising=False)

    async def _fake_fetch(symbol, from_date, to_date):
        return dict(FINNHUB_PAYLOAD)

    monkeypatch.setattr(financials, "_fetch_earnings_calendar_finnhub", _fake_fetch)
    out = await financials.handle_get_earnings_calendar()
    assert out == FINNHUB_PAYLOAD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_with_no_stored_rows_is_unchanged(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    service = _FakeQueryService(events=[])
    monkeypatch.setattr(financials, "MarketEventsQueryService", service)
    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda *a, **k: _AsyncNull())
    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)
    assert out == FINNHUB_PAYLOAD
    assert len(service.calls) == 1
    assert service.calls[0]["kwargs"]["source"] == "benzinga"


class _AsyncNull:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_on_unions_rows_finnhub_does_not_cover(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    service = _FakeQueryService(
        events=[
            _event("AAPL", datetime.date(2026, 10, 29)),  # Finnhub wins
            _event("NVDA", datetime.date(2026, 11, 19)),  # added
        ]
    )
    monkeypatch.setattr(financials, "MarketEventsQueryService", service)
    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda *a, **k: _AsyncNull())

    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)

    assert out["count"] == 2
    symbols = [(row["symbol"], row["date"]) for row in out["earnings"]]
    assert symbols == [("AAPL", "2026-10-29"), ("NVDA", "2026-11-19")]
    # The Finnhub row is kept as it was (no Benzinga overlay).
    assert "source" not in out["earnings"][0]
    added = out["earnings"][1]
    assert added["source"] == "benzinga"
    assert added["eps_actual"] == 2.18
    assert set(out["sources"]) == {"finnhub", "benzinga"}
    assert out["benzinga_union"]["added_rows"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_read_failure_leaves_the_finnhub_answer(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    service = _FakeQueryService(error=RuntimeError("db down"))
    monkeypatch.setattr(financials, "MarketEventsQueryService", service)
    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda *a, **k: _AsyncNull())
    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)
    assert out == FINNHUB_PAYLOAD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_added_rows_carry_the_provider_hour_code(monkeypatch):
    """``hour`` uses the provider vocabulary; ``time_hint`` keeps ours.

    The earnings-context consumer maps only bmo/amc/dmh, so an added row whose
    ``hour`` was ``after_close`` reported unknown release timing.
    """
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    service = _FakeQueryService(
        events=[
            _event("NVDA", datetime.date(2026, 11, 19), hint="after_close"),
            _event("AMD", datetime.date(2026, 11, 20), hint="before_open"),
            _event("SMH", datetime.date(2026, 11, 21), hint="during_market"),
            _event("QQQ", datetime.date(2026, 11, 22), hint="unknown"),
        ]
    )
    monkeypatch.setattr(financials, "MarketEventsQueryService", service)
    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda *a, **k: _AsyncNull())

    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)
    added = [row for row in out["earnings"] if row.get("source") == "benzinga"]
    assert {row["symbol"]: row["hour"] for row in added} == {
        "NVDA": "amc",
        "AMD": "bmo",
        "SMH": "dmh",
        "QQQ": "unknown",
    }
    assert {row["symbol"]: row["time_hint"] for row in added} == {
        "NVDA": "after_close",
        "AMD": "before_open",
        "SMH": "during_market",
        "QQQ": "unknown",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_revised_event_reads_the_value_for_its_current_fiscal_period(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    event = _event("NVDA", datetime.date(2026, 11, 19))
    event.fiscal_year = 2027
    event.fiscal_quarter = 2
    event.values = [
        SimpleNamespace(
            metric_name="eps",
            period="Q1-2027",
            actual=1.25,
            forecast=1.20,
        ),
        SimpleNamespace(
            metric_name="eps",
            period="Q2-2027",
            actual=2.30,
            forecast=2.10,
        ),
    ]
    service = _FakeQueryService(events=[event])
    monkeypatch.setattr(financials, "MarketEventsQueryService", service)
    monkeypatch.setattr(financials, "AsyncSessionLocal", lambda *a, **k: _AsyncNull())

    out = await financials._merge_benzinga_earnings(FINNHUB_PAYLOAD, START, END, None)

    revised = out["earnings"][-1]
    assert revised["quarter"] == 2
    assert revised["year"] == 2027
    assert revised["eps_actual"] == 2.30
    assert revised["eps_estimate"] == 2.10

    # Conflicting same-period rows are ordered by their value fields rather
    # than whatever order the query happened to return.
    event.values = [
        SimpleNamespace(
            metric_name="eps",
            period="Q2-2027",
            actual=2.10,
            forecast=2.00,
        ),
        SimpleNamespace(
            metric_name="eps",
            period="Q2-2027",
            actual=2.30,
            forecast=2.10,
        ),
    ]
    assert financials._metric_value(event, "eps", "actual") == 2.30
    event.values.reverse()
    assert financials._metric_value(event, "eps", "actual") == 2.30

    event.values = [
        SimpleNamespace(
            metric_name="eps",
            period="Q2-2027",
            actual=2.30,
            forecast=None,
        ),
        SimpleNamespace(
            metric_name="eps",
            period="Q2-2027",
            actual=None,
            forecast=2.30,
        ),
    ]
    selected = financials._metric_value(event, "eps", "actual")
    event.values.reverse()
    assert financials._metric_value(event, "eps", "actual") == selected
