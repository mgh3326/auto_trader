"""hk 6343: per-item ingest behaviour and the targets read.

Acceptance 3 (a refused row never discards the rest of the batch) and 5
(targets returns ``build_targets``' symbols and tiers unchanged) without a DB:
the session is a recording fake, the real router and writer run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.db import get_db
from app.middleware.auth import AuthMiddleware
from app.routers.research_minutes_ingest import router as ingest_router
from app.services.research_candles import minute_ingest as mi
from app.services.research_candles import session_minutes_collector as sc

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_INGEST_PATH = "/trading/api/research-minutes/ingest"
_TARGETS_PATH = "/trading/api/research-minutes/targets"
_HEADER = "X-Research-Minutes-Ingest-Token"
_TOKEN = "minutes-secret"


class _Outcome:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Savepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Records inserts by their primary key and answers rowcount like Postgres."""

    def __init__(self, fail_symbols: set[str] | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self.keys: set[tuple[Any, ...]] = set()
        self.commits = 0
        self.fail_symbols = fail_symbols or set()

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def execute(self, statement: object, params: dict[str, Any]) -> _Outcome:
        if params["symbol"] in self.fail_symbols:
            raise RuntimeError("simulated write failure")
        key = (params["time_utc"], params["symbol"], params["venue"])
        if key in self.keys:
            return _Outcome(0)
        self.keys.add(key)
        self.rows.append(dict(params))
        return _Outcome(1)

    async def commit(self) -> None:
        self.commits += 1


def _bar(symbol: str, *, received_at: str = "2026-11-24T00:02:00Z") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "venue": "KRX",
        "time_utc": "2026-11-24T00:00:00Z",
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": "10",
        "received_at": received_at,
        "source": "KIS",
    }


def _build_app(db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(ingest_router)
    app.add_middleware(AuthMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    return app


async def _post(body: dict[str, Any], db: Any):
    app = _build_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.post(_INGEST_PATH, json=body, headers={_HEADER: _TOKEN})


async def _get(path: str, db: Any):
    app = _build_app(db)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        return await client.get(path, headers={_HEADER: _TOKEN})


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


async def test_refused_items_do_not_discard_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the server clock so the future-received_at item is deterministic.
    real_now = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # noqa: N805
            return real_now if tz is None else real_now.astimezone(tz)

    monkeypatch.setattr("app.routers.research_minutes_ingest.datetime", _Clock)

    session = _FakeSession()
    body = {
        "market": "kr",
        "batch_id": "go-kis-poll-1",
        "bars": [
            _bar("005930"),
            _bar("000660"),
            {**_bar("035420"), "source": "UPBIT"},  # not in the closed vocabulary
            _bar("051910", received_at="2026-11-24T05:00:00Z"),  # future receipt
        ],
    }

    reply = await _post(body, session)
    assert reply.status_code == 200
    payload = reply.json()
    assert payload["batch_id"] == "go-kis-poll-1"
    assert payload["received"] == 4
    assert payload["inserted"] == 2
    assert payload["already_present"] == 0
    assert payload["rejected"] == 2
    assert [r["status"] for r in payload["results"]] == [
        "inserted",
        "inserted",
        "rejected",
        "rejected",
    ]
    assert payload["results"][2]["reason"] == mi.REASON_UNKNOWN_SOURCE
    assert payload["results"][3]["reason"] == mi.REASON_RECEIVED_AT_IN_FUTURE

    # Only the two accepted rows reached the writer, with the derived fields the
    # producer does not own.
    assert [row["symbol"] for row in session.rows] == ["005930", "000660"]
    assert {row["session_segment"] for row in session.rows} == {"KRX_REGULAR"}
    assert {row["session_date"] for row in session.rows} == {
        datetime(2026, 11, 24).date()
    }
    assert {row["retrieved_at"] for row in session.rows} == {
        datetime(2026, 11, 24, 0, 2, tzinfo=UTC)
    }
    assert session.commits == 1


async def test_replay_inserts_zero_and_keeps_the_first_retrieved_at() -> None:
    session = _FakeSession()
    body = {"market": "kr", "batch_id": "go-kis-poll-1", "bars": [_bar("005930")]}

    first = (await _post(body, session)).json()
    assert first["inserted"] == 1
    assert session.rows[0]["retrieved_at"] == datetime(2026, 11, 24, 0, 2, tzinfo=UTC)

    # The same bars again, with a later claimed receipt: the row is untouched.
    replay = {
        "market": "kr",
        "batch_id": "go-kis-poll-2",
        "bars": [_bar("005930", received_at="2026-11-24T00:05:00Z")],
    }
    second = (await _post(replay, session)).json()
    assert second["inserted"] == 0
    assert second["already_present"] == 1
    assert second["results"] == [{"status": "already_present", "reason": None}]
    assert len(session.rows) == 1
    assert session.rows[0]["retrieved_at"] == datetime(2026, 11, 24, 0, 2, tzinfo=UTC)
    assert session.rows[0]["batch_id"] == "go-kis-poll-1"


async def test_a_write_failure_is_reported_per_item() -> None:
    session = _FakeSession(fail_symbols={"000660"})
    body = {"market": "kr", "bars": [_bar("005930"), _bar("000660")]}

    payload = (await _post(body, session)).json()
    assert [r["status"] for r in payload["results"]] == ["inserted", "rejected"]
    assert payload["results"][1]["reason"] == "RuntimeError"
    assert [row["symbol"] for row in session.rows] == ["005930"]


def _fake_targets() -> sc.TargetSet:
    return sc.TargetSet(
        symbols=["005930", "000660", "SPY"],
        by_origin={"kis_holdings": 1, "fixed_list": 2},
        errors=[{"origin": "manual_holdings", "error_class": "RuntimeError"}],
        truncated={"screener_snapshot": 4},
        backfill=["000660"],
        tiers=[
            sc.TierReport(
                tier="fixed", budget=50, allowance=50, candidates=3, selected=3, cut=0
            ),
            sc.TierReport(
                tier="screener_snapshot",
                budget=60,
                allowance=60,
                candidates=4,
                selected=0,
                cut=4,
            ),
        ],
        cuts=[
            {"symbol": "035420", "tier": "screener_snapshot", "reason": "budget_cut"}
        ],
    )


async def test_targets_read_returns_the_build_targets_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_targets()

    async def _fake_build_targets(**kwargs: Any) -> sc.TargetSet:
        assert kwargs["market"] == "kr"
        assert kwargs["user_id"] == 42
        assert kwargs["fixed_symbols_raw"] == "069500,229200"
        assert kwargs["max_symbols"] == 150
        assert kwargs["screener_top_n"] == 20
        return fake

    class _FakeKis:
        async def fetch_my_stocks(self) -> list[dict[str, Any]]:
            return []

        async def fetch_my_us_stocks(self) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(mi, "build_targets", _fake_build_targets)
    monkeypatch.setattr("app.services.brokers.kis.client.KISClient", lambda: _FakeKis())
    monkeypatch.setattr("app.routers.research_minutes_ingest.MCP_USER_ID", 42)

    reply = await _get(f"{_TARGETS_PATH}?market=kr", object())
    assert reply.status_code == 200
    payload = reply.json()

    expected = sc.targets_report(fake)
    assert payload["market"] == "kr"
    for key, value in expected.items():
        assert payload[key] == value
    assert payload["errors"] == fake.errors
    assert isinstance(payload["generated_at"], str)


async def test_targets_read_rejects_an_unknown_market() -> None:
    reply = await _get(f"{_TARGETS_PATH}?market=jp", object())
    assert reply.status_code == 422


async def test_the_real_app_mounts_the_surface_and_gate_off_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end on ``app.main``: the route exists, the gate answers first."""
    monkeypatch.setattr(
        settings, "RESEARCH_MINUTES_INGEST_ENABLED", False, raising=False
    )
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        reply = await client.post(_INGEST_PATH, json={"market": "kr", "bars": []})
    assert reply.status_code == 404
    assert reply.json() == {"detail": "Not Found"}
