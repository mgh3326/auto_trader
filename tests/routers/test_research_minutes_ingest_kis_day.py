"""hk 7044: KIS_DAY bars through the ingest router use the calendar-only reader.

Pure and DB-free: the session is a recording fake and the real router and writer
run. A KIS_DAY bar is labelled ``US_DAY`` only inside the Toss US dayMarket window
of its KST date; any failure to read that calendar refuses the bar as
``calendar_unavailable`` (rows are insert-only, so no guessed label is stored).
Other US venues never read the calendar and keep their KIS-window labels.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.research_minutes_ingest import router as ingest_router
from app.services.brokers.toss.market_calendar import parse_us_market_calendar
from app.services.research_candles import us_day_calendar

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_INGEST_PATH = "/trading/api/research-minutes/ingest"
_HEADER = "X-Research-Minutes-Ingest-Token"
_TOKEN = "minutes-secret"
_DATE = "2026-11-25"


def _window(start: str, end: str) -> dict[str, str]:
    return {
        "startTime": f"{_DATE}T{start}:00+09:00",
        "endTime": f"{_DATE}T{end}:00+09:00",
    }


def _calendar():
    """Toss US calendar of one listed day: dayMarket 10:00-17:50 KST."""
    return parse_us_market_calendar(
        {
            "today": {
                "date": _DATE,
                "dayMarket": _window("10:00", "17:50"),
                "preMarket": None,
                "regularMarket": None,
                "afterMarket": None,
            }
        }
    )


class _Outcome:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Savepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.keys: set[tuple[Any, ...]] = set()

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, statement: object, params: dict[str, Any]) -> _Outcome:
        key = (params["time_utc"], params["symbol"], params["venue"])
        if key in self.keys:
            return _Outcome(0)
        self.keys.add(key)
        self.rows.append(dict(params))
        return _Outcome(1)

    async def commit(self) -> None:
        return None


def _us_bar(
    *, venue: str = "KIS_DAY", time_utc: str = f"{_DATE}T01:00:00Z"
) -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "venue": venue,
        "time_utc": time_utc,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "10",
        "value": "1000",
        # Two minutes after the bar starts: the bar is closed (start + 1 min + 10 s).
        "received_at": (
            datetime.fromisoformat(time_utc.replace("Z", "+00:00"))
            + timedelta(minutes=2)
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "KIS",
    }


def _app(db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _post(bars: list[dict[str, Any]], db: Any):
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="https://test"
    ) as client:
        return await client.post(
            _INGEST_PATH,
            json={"market": "us", "bars": bars},
            headers={_HEADER: _TOKEN},
        )


@pytest.fixture(autouse=True)
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_ENABLED", True, raising=False
    )
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN", _TOKEN, raising=False
    )
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_TOKEN_HEADER", _HEADER, raising=False
    )


class _Reader:
    def __init__(self, calendar: Any | None = None, error: Exception | None = None):
        self.calendar = calendar
        self.error = error
        self.days: list[date] = []

    async def __call__(self, day: date) -> Any | None:
        self.days.append(day)
        if self.error is not None:
            raise self.error
        return self.calendar


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> _Reader:
    fake = _Reader(calendar=_calendar())
    monkeypatch.setattr(us_day_calendar, "_reader", fake)
    return fake


async def test_kis_day_inside_the_day_market_window_is_us_day(reader: _Reader) -> None:
    session = _FakeSession()
    response = (await _post([_us_bar()], session)).json()

    assert response["inserted"] == 1
    assert session.rows[0]["session_segment"] == "US_DAY"
    assert session.rows[0]["venue"] == "KIS_DAY"
    assert reader.days == [date(2026, 11, 25)]


async def test_kis_day_outside_every_window_is_unknown_never_guessed(
    reader: _Reader,
) -> None:
    session = _FakeSession()
    # 23:00 UTC is 08:00 KST the next day: outside the dayMarket window.
    response = (await _post([_us_bar(time_utc="2026-11-25T23:00:00Z")], session)).json()

    assert response["inserted"] == 1
    assert session.rows[0]["session_segment"] == "UNKNOWN"


async def test_two_kis_day_bars_on_one_kst_date_read_the_calendar_once(
    reader: _Reader,
) -> None:
    session = _FakeSession()
    bars = [
        _us_bar(time_utc="2026-11-25T01:00:00Z"),
        _us_bar(time_utc="2026-11-25T01:01:00Z"),
    ]
    response = (await _post(bars, session)).json()

    assert response["inserted"] == 2
    assert reader.days == [date(2026, 11, 25)]


@pytest.mark.parametrize(
    "fake",
    [
        _Reader(error=RuntimeError("calendar read failed")),
        _Reader(calendar=None),
    ],
    ids=["reader_raises", "reader_returns_none"],
)
async def test_calendar_failure_refuses_kis_day_as_calendar_unavailable(
    monkeypatch: pytest.MonkeyPatch, fake: _Reader
) -> None:
    monkeypatch.setattr(us_day_calendar, "_reader", fake)
    session = _FakeSession()
    response = (await _post([_us_bar()], session)).json()

    assert response["inserted"] == 0
    assert response["rejected"] == 1
    assert response["results"] == [
        {"status": "rejected", "reason": "calendar_unavailable"}
    ]
    assert session.rows == []


async def test_no_registered_reader_refuses_kis_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(us_day_calendar, "_reader", None)
    session = _FakeSession()
    response = (await _post([_us_bar()], session)).json()

    assert response["results"] == [
        {"status": "rejected", "reason": "calendar_unavailable"}
    ]
    assert session.rows == []


async def test_a_batch_without_kis_day_never_reads_the_calendar(
    reader: _Reader,
) -> None:
    session = _FakeSession()
    # NASD inside the dayMarket window keeps its KIS-window label, not US_DAY.
    response = (await _post([_us_bar(venue="NASD")], session)).json()

    assert response["inserted"] == 1
    assert reader.days == []
    assert session.rows[0]["session_segment"] != "US_DAY"


async def test_an_overflowing_kis_day_start_is_refused_per_item(
    reader: _Reader,
) -> None:
    """hk 7044 review: one overflowing start never fails the batch.

    ``9999-12-31T23:00Z`` is a valid UTC instant but its KST conversion
    overflows. The bar is refused as ``bar_time_invalid`` for that item only, and
    the valid bar beside it is still stored. Before the fix the batch scan raised
    OverflowError (a 500 for the whole request).
    """
    session = _FakeSession()
    overflowing = _us_bar(time_utc="9999-12-31T23:00:00Z")
    overflowing["received_at"] = "9999-12-31T23:02:00Z"
    response = await _post(
        [overflowing, _us_bar(time_utc=f"{_DATE}T01:00:00Z")], session
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0] == {"status": "rejected", "reason": "bar_time_invalid"}
    assert body["results"][1]["status"] == "inserted"
    assert body["inserted"] == 1
    assert [row["symbol"] for row in session.rows] == ["AAPL"]
    assert reader.days == [date(2026, 11, 25)]
