"""hk 6424: the futures market on the research minute ingest, without a DB.

The real router, auth middleware and futures writer run; the session is a recording
fake and the futures master listing is a fixed value (no network). Acceptance 2: a
bar whose roll_state or session_date disagrees is refused with a closed reason while
the other bars of the same batch still insert.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.research_minutes_ingest import router as ingest_router
from app.services.research_candles import futures_listing
from app.services.research_candles import minute_ingest as mi

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_INGEST_PATH = "/trading/api/research-minutes/ingest"
_HEADER = "X-Research-Minutes-Ingest-Token"
_TOKEN = "minutes-secret"
#: 2026-12-02 01:00 UTC; the router clock is pinned to it below.
_NOW = datetime(2026, 12, 2, 1, 0, tzinfo=UTC)
_LISTING: dict[str, tuple[str, ...]] = {"MF200": ("2026-11", "2026-12", "2027-03")}


class _Outcome:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Savepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Keys by the table's primary key, like Postgres: ``ON CONFLICT DO NOTHING``."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.keys: set[tuple[Any, ...]] = set()
        self.commits = 0

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, statement: object, params: dict[str, Any]) -> _Outcome:
        # Futures rows are keyed (time_utc, symbol); stock rows add venue.
        key = (params["time_utc"], params["symbol"], params.get("venue"))
        if key in self.keys:
            return _Outcome(0)
        self.keys.add(key)
        self.rows.append(dict(params))
        return _Outcome(1)

    async def commit(self) -> None:
        self.commits += 1


class _StaticListing:
    def __init__(self, listing: dict[str, tuple[str, ...]] | None) -> None:
        self.listing = listing

    async def listed_months_by_product(self) -> dict[str, tuple[str, ...]] | None:
        return self.listing


class _Clock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # noqa: N805
        return _NOW if tz is None else _NOW.astimezone(tz)


def _futures_bar(symbol: str, minute: int, **extra: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "venue": "KRX",
        "time_utc": f"2026-12-01T00:{minute:02d}:00Z",
        "open": "400.00",
        "high": "400.20",
        "low": "399.90",
        "close": "400.10",
        "volume": "5",
        # One minute after the bar start: closed (start + 70 s) and before _NOW.
        "received_at": f"2026-12-01T00:{minute + 1:02d}:30Z",
        "source": "KIS",
        **extra,
    }


def _build_app(db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _post(body: dict[str, Any], db: Any, token: str = _TOKEN):
    app = _build_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(_INGEST_PATH, json=body, headers={_HEADER: token})


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
    monkeypatch.setattr("app.routers.research_minutes_ingest.datetime", _Clock)


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> _StaticListing:
    static = _StaticListing(_LISTING)
    monkeypatch.setattr(futures_listing, "FUTURES_LISTING", static)
    return static


async def test_a_refused_derived_field_does_not_discard_the_rest_of_the_batch(
    listing: _StaticListing,
) -> None:
    session = _FakeSession()
    body = {
        "market": "futures",
        "batch_id": "futures-poll-1",
        "bars": [
            _futures_bar("MF200.2703", 0, roll_state="next", session_date="2026-12-01"),
            _futures_bar("MF200.2703", 1, roll_state="front"),  # 2027-03 is "next"
            _futures_bar("MF200.2703", 2, session_date="2026-11-30"),
            _futures_bar("MF200.2703", 3, product="MF200", contract_month="2703"),
        ],
    }

    reply = await _post(body, session)

    assert reply.status_code == 200
    payload = reply.json()
    assert payload["market"] == "futures"
    assert payload["batch_id"] == "futures-poll-1"
    assert (payload["inserted"], payload["already_present"], payload["rejected"]) == (
        2,
        0,
        2,
    )
    assert payload["results"] == [
        {"status": "inserted", "reason": None},
        {"status": "rejected", "reason": mi.REASON_ROLL_STATE_MISMATCH},
        {"status": "rejected", "reason": mi.REASON_SESSION_DATE_MISMATCH},
        {"status": "inserted", "reason": None},
    ]
    assert [row["time_utc"].minute for row in session.rows] == [0, 3]
    assert {row["roll_state"] for row in session.rows} == {"next"}
    assert {row["session_segment"] for row in session.rows} == {"FUT_DAY"}
    assert {row["session_date"] for row in session.rows} == {date(2026, 12, 1)}
    assert {row["product"] for row in session.rows} == {"MF200"}
    assert {row["contract_month"] for row in session.rows} == {"2703"}
    # The first receipt is the producer's received_at, never a later server clock.
    assert [row["retrieved_at"].minute for row in session.rows] == [1, 4]
    assert session.commits == 1


async def test_a_replay_inserts_nothing_and_keeps_the_first_receipt(
    listing: _StaticListing,
) -> None:
    session = _FakeSession()
    body = {
        "market": "futures",
        "bars": [_futures_bar("MF200.2703", 0)],
    }

    first = await _post(body, session)
    # The replay claims a later receipt; the stored first receipt must not move.
    replay_body = {
        "market": "futures",
        "bars": [_futures_bar("MF200.2703", 0, received_at="2026-12-01T00:09:00Z")],
    }
    replay = await _post(replay_body, session)

    assert first.json()["inserted"] == 1
    assert replay.json()["inserted"] == 0
    assert replay.json()["already_present"] == 1
    assert [row["retrieved_at"].minute for row in session.rows] == [1]


async def test_an_unreadable_master_refuses_every_futures_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static = _StaticListing(None)
    monkeypatch.setattr(futures_listing, "FUTURES_LISTING", static)
    session = _FakeSession()
    body = {"market": "futures", "bars": [_futures_bar("MF200.2703", 0)]}

    reply = await _post(body, session)

    assert reply.status_code == 200
    assert reply.json()["results"] == [
        {"status": "rejected", "reason": mi.REASON_MASTER_UNAVAILABLE}
    ]
    assert session.rows == []


async def test_a_stock_market_item_carrying_a_futures_field_is_refused(
    listing: _StaticListing,
) -> None:
    session = _FakeSession()
    stock = {
        "symbol": "005930",
        "venue": "KRX",
        "time_utc": "2026-12-01T00:00:00Z",
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "10",
        "received_at": "2026-12-01T00:02:00Z",
        "source": "KIS",
    }
    body = {
        "market": "kr",
        "bars": [
            {**stock, "roll_state": "next"},
            {
                **stock,
                "time_utc": "2026-12-01T00:01:00Z",
                "received_at": "2026-12-01T00:03:00Z",
            },
        ],
    }

    reply = await _post(body, session)

    assert reply.status_code == 200
    assert [r["reason"] for r in reply.json()["results"]] == [
        mi.REASON_FIELD_NOT_APPLICABLE,
        None,
    ]
    assert reply.json()["results"][1]["status"] == "inserted"
    assert [row["symbol"] for row in session.rows] == ["005930"]
    # Listing is only read for futures batches.
    assert listing.listing == _LISTING


async def test_a_futures_bar_with_a_value_is_refused(listing: _StaticListing) -> None:
    session = _FakeSession()
    body = {"market": "futures", "bars": [_futures_bar("MF200.2703", 0, value="1000")]}

    reply = await _post(body, session)

    assert reply.json()["results"] == [
        {"status": "rejected", "reason": mi.REASON_FIELD_NOT_APPLICABLE}
    ]
    assert session.rows == []


async def test_a_futures_batch_without_a_batch_id_gets_a_futures_prefix(
    listing: _StaticListing,
) -> None:
    session = _FakeSession()
    reply = await _post(
        {"market": "futures", "bars": [_futures_bar("MF200.2703", 0)]}, session
    )
    assert reply.json()["batch_id"].startswith("minutes-ingest-futures-")


async def test_a_wrong_token_is_refused_before_the_ingest_runs(
    listing: _StaticListing,
) -> None:
    session = _FakeSession()
    reply = await _post(
        {"market": "futures", "bars": [_futures_bar("MF200.2703", 0)]},
        session,
        token="not-the-token",
    )
    assert reply.status_code == 401
    assert session.rows == []
