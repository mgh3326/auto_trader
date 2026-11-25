"""DB-free ASGI tests for the Benzinga Pro capture route."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.benzinga_pro_ingest import router
from app.schemas.benzinga_pro_ingest import MAX_BODY_BYTES

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PATH = "/trading/api/research/benzinga-pro/ingest"
HEADER = "X-Benzinga-Pro-Ingest-Token"
TOKEN = "test-machine-token"


@pytest.fixture(autouse=True)
def authorized(monkeypatch):
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_ENABLED", True)
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_TOKEN_HEADER", HEADER)


@pytest.fixture
def capture():
    # hk:doc fixtures/2026-11-25/benzinga-pro-scanner-premarket.
    # AFL has the provider's -100 sentinel; AREC is a genuine down move.
    def mover(
        symbol,
        rank,
        change_pct,
        volume,
        price,
        previous_close,
        market_cap,
        excluded_reason=None,
    ):
        return {
            "symbol": symbol,
            "rank": rank,
            "change_pct": change_pct,
            "volume": volume,
            "price": price,
            "previous_close": previous_close,
            "market_cap": market_cap,
            "sector": None,
            "excluded_reason": excluded_reason,
        }

    return {
        "schema": "benzinga-pro-capture/v1",
        "captured_at": "2026-11-25T14:12:11.334Z",
        "session": "us_pre",
        "scanner_request": {
            "fields": "symbol,preMarketChangePercent,preMarketVolume,price,previousClose,marketCap,sector",
            "filter": "preMarketVolume > 50000",
            "sort_field": "preMarketChangePercent",
            "page_size": 25,
            "pages": 1,
        },
        "movers": {
            "up": [mover("NRSN", 1, "149.09", "30254723", "2.74", "1.1", "6831354")],
            "down": [
                mover(
                    "AFL",
                    1,
                    "-100",
                    "59693",
                    "124.301",
                    "125",
                    "63459389530",
                    "no_session_price",
                ),
                mover("AREC", 10, "-29.91", "536038", "0.75", "1.07", "80250324"),
            ],
        },
        "breaking_events": [
            {
                "provider_id": "test-news-1",
                "headline": "Fixture headline",
                "symbols": ["AREC", "BRK-B"],
                "created_raw": None,
                "url": None,
            }
        ],
    }


def _reject_db():
    raise AssertionError("denied request resolved DB dependency")


def _app(db=None):
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_db] = _reject_db if db is None else lambda: db
    return app


async def _post(body, *, token=TOKEN, db=None, header=HEADER):
    async with AsyncClient(
        transport=ASGITransport(app=_app(db)), base_url="https://test"
    ) as client:
        client.cookies.set("session", "fake-session")
        return await client.post(PATH, json=body, headers={header: token})


@pytest.mark.parametrize(
    ("enabled", "configured", "provided", "code"),
    [
        (False, "", "wrong", 404),
        (True, "", "", 403),
        (True, TOKEN, "wrong", 401),
        (True, TOKEN, "", 401),
    ],
)
async def test_gate_and_token_precede_body_and_db(
    monkeypatch, enabled, configured, provided, code
):
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_ENABLED", enabled)
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_TOKEN", configured)
    assert (await _post(None, token=provided)).status_code == code


@pytest.mark.parametrize(
    "value, enabled", [("true", True), ("TRUE", False), ("1", False)]
)
async def test_enabled_setting_requires_exact_true(monkeypatch, value, enabled):
    monkeypatch.setenv("BENZINGA_PRO_INGEST_ENABLED", value)
    assert Settings().BENZINGA_PRO_INGEST_ENABLED is enabled


async def test_root_path_cannot_bypass_machine_token():
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/prefix"),
        base_url="https://test",
    ) as client:
        response = await client.post("/prefix" + PATH)
    assert response.status_code == 401


@pytest.mark.parametrize("field", ["apikey", "key", "unexpected"])
@pytest.mark.parametrize("nested", [False, True])
async def test_unknown_fields_rejected_without_echo(capture, field, nested):
    target = capture["movers"]["up"][0] if nested else capture
    target[field] = "sensitive-input"
    response = await _post(capture)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid capture payload"}


async def test_body_limit():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="https://test"
    ) as client:
        response = await client.post(
            PATH, content=b"x" * (MAX_BODY_BYTES + 1), headers={HEADER: TOKEN}
        )
    assert response.status_code == 413


class _CaptureSession:
    """Record SQLAlchemy statement inputs and simulate INSERT conflict results."""

    def __init__(self):
        self.runs = {}
        self.movers = {}
        self.events = {}
        self.commits = 0

    async def scalar(self, stmt):
        assert "EXISTS" in str(stmt)
        run_id = stmt.compile().params["run_id_1"]
        return any(key[0] == run_id for key in self.runs)

    async def execute(self, stmt):
        if not stmt.is_insert:
            sql = str(stmt)
            if "us_symbol_universe" in sql:
                return SimpleNamespace(all=lambda: [])
            assert "pg_advisory_xact_lock" in sql
            return None
        assert "ON CONFLICT" in str(stmt)
        row = stmt.compile().params
        match stmt.table.name:
            case "invest_momentum_event_runs":
                bucket = self.runs
                key = (row["run_id"], row["surface"])
            case "invest_momentum_event_snapshots":
                bucket = self.movers
                key = (row["surface"], row["snapshot_at"], row["symbol"])
            case "market_events":
                bucket = self.events
                key = (row["source"], row["source_event_id"])
            case _:
                raise AssertionError(stmt.table.name)
        inserted = key not in bucket
        bucket.setdefault(key, row)
        return SimpleNamespace(
            rowcount=int(inserted), scalar_one_or_none=lambda: 1 if inserted else None
        )

    async def commit(self):
        self.commits += 1


async def test_capture_replay_and_excluded_rows(capture, monkeypatch):
    db = _CaptureSession()
    monkeypatch.setattr(settings, "BENZINGA_PRO_INGEST_TOKEN_HEADER", "X-Fixture-Token")
    assert (await _post(capture)).status_code == 401
    reply = await _post(capture, db=db, header="X-Fixture-Token")
    assert reply.status_code == 200
    assert reply.json() == {
        "runs_inserted": 2,
        "movers_inserted": 2,
        "excluded_inserted": 1,
        "events_inserted": 1,
    }
    at = datetime(2026, 11, 25, 14, 12, 11, 334000, tzinfo=UTC)
    up = "us:benzinga_pro:us_pre:up"
    down = "us:benzinga_pro:us_pre:down"
    assert {key[1] for key in db.runs} == {up, down}
    assert {(row["status"], row["reason"]) for row in db.runs.values()} == {
        ("ok", None)
    }
    assert db.movers[up, at, "NRSN"]["change_rate"] == Decimal("149.09")
    assert db.movers[down, at, "AREC"]["order_type"] == "down"
    excluded = db.movers[down, at, "AFL"]
    assert excluded["order_type"] == "excluded"
    assert excluded["change_rate"] is None
    assert excluded["raw_payload"]["excluded_reason"] == "no_session_price"
    event = db.events["benzinga_pro", "test-news-1"]
    assert event["first_received_at"] == at
    assert event["raw_payload_json"] == {"symbols": ["AREC", "BRK.B"]}

    # Same UTC instant reserialized and mislabeled with another session.
    capture["captured_at"] = "2026-11-25T14:12:11.334000+00:00"
    capture["session"] = "us_regular"
    reply = await _post(capture, db=db, header="X-Fixture-Token")
    assert reply.status_code == 200
    assert set(reply.json().values()) == {0}
    assert (len(db.runs), len(db.movers), len(db.events)) == (2, 3, 1)

    # A new capture records both empty sides and retains the event's first receipt.
    capture["captured_at"] = "2026-11-25T15:12:11.334Z"
    capture["movers"] = {"up": [], "down": []}
    reply = await _post(capture, db=db, header="X-Fixture-Token")
    assert reply.json() == {
        "runs_inserted": 2,
        "movers_inserted": 0,
        "excluded_inserted": 0,
        "events_inserted": 0,
    }
    assert {row["reason"] for row in db.runs.values() if row["status"] == "empty"} == {
        "no_rows"
    }
    assert db.events["benzinga_pro", "test-news-1"]["first_received_at"] == at
    assert db.commits == 3
