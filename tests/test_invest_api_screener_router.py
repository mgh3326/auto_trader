"""ROB-147 — router tests for /invest/api/screener/{presets,results}."""

from __future__ import annotations

import datetime as dt
from datetime import UTC
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import (
    get_invest_home_service,
    get_screener_service_dep,
)
from app.routers.invest_api import (
    router as invest_api_router,
)
from app.schemas.invest_home import (
    InvestHomeResponse,
    InvestHomeResponseMeta,
)
from app.services.invest_home_service import build_grouped_holdings, build_home_summary


class _StubHomeService:
    async def get_home(self, *, user_id: int, **kwargs) -> InvestHomeResponse:
        return InvestHomeResponse(
            homeSummary=build_home_summary([]),
            accounts=[],
            holdings=[],
            groupedHoldings=build_grouped_holdings([]),
            meta=InvestHomeResponseMeta(warnings=[]),
        )


class _StubScreening:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._payload = payload or {
            "results": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "market": "kr",
                    "sector": "반도체",
                    "market_cap_krw": 478_000_000_000_000,
                    "close": 80_000,
                    "change_rate": 1.23,
                    "change_amount": 970,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
        self.list_screening = AsyncMock(side_effect=self._list)

    async def _list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._payload


def _build_app(stub_screening: _StubScreening | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubHomeService()
    if stub_screening is not None:
        app.dependency_overrides[get_screener_service_dep] = lambda: stub_screening
    return app


@pytest.mark.unit
def test_screener_presets_endpoint_returns_catalog() -> None:
    client = TestClient(_build_app())
    r = client.get("/invest/api/screener/presets")
    assert r.status_code == 200
    body = r.json()
    assert len(body["presets"]) >= 6
    assert body["selectedPresetId"] == "consecutive_gainers"
    assert any(p["id"] == "consecutive_gainers" for p in body["presets"])
    # ROB-359 Scope B — catalog provenance flows through the API contract.
    by_id = {p["id"]: p for p in body["presets"]}
    assert by_id["consecutive_gainers"]["presetOrigin"] == "toss_parity"
    assert by_id["consecutive_gainers"]["parityStatus"] == "full"
    assert by_id["kr_high_volume_surge"]["presetOrigin"] == "auto_trader_original"
    assert by_id["oversold_recovery"]["parityStatus"] == "mismatch"
    assert by_id["oversold_recovery"]["parityNote"]


@pytest.mark.unit
def test_screener_results_endpoint_happy_path() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=consecutive_gainers")
    assert r.status_code == 200
    body = r.json()
    assert body["presetId"] == "consecutive_gainers"
    assert body["title"] == "연속 상승세"
    assert len(body["results"]) == 1
    assert body["results"][0]["symbol"] == "005930"
    assert stub.calls and stub.calls[0]["market"] == "kr"


@pytest.mark.unit
def test_screener_results_endpoint_normalizes_code_only_row() -> None:
    stub = _StubScreening(
        payload={
            "results": [
                {
                    "code": "005930",
                    "name": "삼성전자",
                    "market": "kr",
                    "sector": "반도체",
                    "market_cap_krw": 478_000_000_000_000,
                    "close": 80_000,
                    "change_rate": 1.23,
                    "change_amount": 970,
                    "volume": 12_345_678,
                }
            ],
            "warnings": [],
        }
    )
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=consecutive_gainers")
    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["symbol"] == "005930"
    assert body["results"][0]["marketCapLabel"] == "478.0조원"


@pytest.mark.unit
def test_screener_results_endpoint_unknown_preset_returns_empty_with_warning() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=__unknown__")
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert body["warnings"]
    assert stub.calls == []


@pytest.mark.unit
def test_screener_results_endpoint_requires_preset_param() -> None:
    stub = _StubScreening()
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results")
    assert r.status_code == 422  # missing required query param


@pytest.mark.unit
def test_screener_results_endpoint_forwards_market_query() -> None:
    stub = _StubScreening(
        payload={
            "results": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "market": "us",
                    "sector": "Technology",
                    "market_cap_usd": 3_200_000_000_000,
                    "current_price": 210.4,
                    "change_rate": 1.5,
                    "change_amount": 3.1,
                    "volume": 50_000_000,
                    "per": 32.1,
                }
            ],
            "warnings": [],
        }
    )
    client = TestClient(_build_app(stub_screening=stub))
    r = client.get("/invest/api/screener/results?preset=cheap_value&market=us")

    assert r.status_code == 200
    body = r.json()
    assert body["results"][0]["market"] == "us"
    assert body["results"][0]["marketCapLabel"] == "$3.20T"
    assert stub.calls and stub.calls[0]["market"] == "us"


@pytest.mark.unit
def test_screener_consecutive_gainers_returns_streak_and_freshness() -> None:
    stub_payload = {
        "results": [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "market": "kr",
                "sector": "반도체",
                "market_cap_krw": 478_000_000_000_000,
                "close": 80_000,
                "change_rate": 1.23,
                "change_amount": 970,
                "consecutive_up_days": 6,
                "week_change_rate": 8.50,
                "volume": 12_345_678,
            },
            {
                "symbol": "035720",
                "name": "카카오",
                "market": "kr",
                "sector": "인터넷",
                "market_cap_krw": 20_000_000_000_000,
                "close": 45_000,
                "change_rate": 0.8,
                "change_amount": 360,
                "consecutive_up_days": 5,
                "week_change_rate": 3.20,
                "volume": 3_000_000,
            },
        ],
        "warnings": [],
        "timestamp": "2026-05-10T05:30:00+00:00",
        "cache_hit": False,
    }
    stub = _StubScreening(payload=stub_payload)
    client = TestClient(_build_app(stub_screening=stub))

    r = client.get("/invest/api/screener/results?preset=consecutive_gainers&market=kr")

    assert r.status_code == 200
    body = r.json()
    assert body["presetId"] == "consecutive_gainers"
    # Freshness block must be present and correctly shaped
    freshness = body.get("freshness")
    assert freshness is not None
    assert freshness["asOfLabel"].endswith("기준")
    assert freshness["source"] in ("live", "cached", "previous_session")
    # Metric is now 1-week change rate (Toss-parity primary metric)
    results = body["results"]
    assert len(results) == 2
    for row in results:
        label = row["metricValueLabel"]
        assert "%" in label, f"Expected week_change_rate % label, got: {label}"
    # Verify the Toss-parity preset filters passed to the service
    assert stub.calls
    assert stub.calls[0].get("min_consecutive_up_days") == 5
    assert stub.calls[0].get("min_week_change_rate") == 0.0
    assert stub.calls[0].get("sort_by") == "week_change_rate"
    assert stub.calls[0].get("limit") == 80


# ---------------------------------------------------------------------------
# ROB-277: e2e freshness shape with stale snapshot on the HTTP layer
# ---------------------------------------------------------------------------

# Reuse the _FakeSession / _FakeSnapshot pattern from
# test_invest_view_model_screener_service.py — mirror the idiom exactly.


class _RouterFakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _RouterFakeExecuteResult:
    def __init__(
        self,
        *,
        scalar_rows: list[Any] | None = None,
        rows: list[Any] | None = None,
    ) -> None:
        self._scalar_rows = scalar_rows or []
        self._rows = rows or []

    def scalars(self) -> _RouterFakeScalarResult:
        return _RouterFakeScalarResult(self._scalar_rows)

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any | None:
        return self._scalar_rows[0] if self._scalar_rows else None

    def one(self) -> Any:
        if self._rows:
            return self._rows[0]
        return type("EmptyRow", (), {})()


class _RouterFakeSession:
    """Minimal async session that pops pre-loaded results on each execute()."""

    def __init__(self, results: list[_RouterFakeExecuteResult]) -> None:
        self._results = list(results)
        self._initial_count = len(results)
        self.calls = 0

    async def execute(self, stmt: Any) -> _RouterFakeExecuteResult:  # noqa: ARG002
        self.calls += 1
        if not self._results:
            raise AssertionError(
                "RouterFakeSession exhausted: build_screener_results issued more DB "
                "queries than the test seeded. Expected the snapshot-first path to "
                f"perform exactly {self._initial_count} queries; update the test if "
                "you've intentionally added a new query."
            )
        return self._results.pop(0)


class _RouterFakeSnapshot:
    """Minimal ORM-like snapshot object mirroring InvestScreenerSnapshot attributes."""

    def __init__(self, **kwargs: Any) -> None:
        self.market = kwargs.get("market", "kr")
        self.symbol = kwargs["symbol"]
        self.snapshot_date = kwargs.get("snapshot_date", dt.date(2026, 5, 13))
        self.latest_close = kwargs.get("latest_close", Decimal("80000"))
        self.prev_close = kwargs.get("prev_close", Decimal("79000"))
        self.change_amount = kwargs.get("change_amount", Decimal("1000"))
        self.change_rate = kwargs.get("change_rate", Decimal("1.2658"))
        self.consecutive_up_days = kwargs.get("consecutive_up_days", 6)
        self.week_change_rate = kwargs.get("week_change_rate", Decimal("3.5"))
        self.closes_window = kwargs.get(
            "closes_window", [76000, 77000, 78000, 79000, 80000]
        )
        self.daily_volume = kwargs.get("daily_volume", 1_234_567)
        self.computed_at = kwargs.get(
            "computed_at", dt.datetime(2026, 5, 13, 0, 30, tzinfo=UTC)
        )


def _router_name_row(symbol: str, name: str) -> Any:
    return type("NameRow", (), {"symbol": symbol, "name": name})()


def _build_snapshot_first_app(fake_session: _RouterFakeSession) -> FastAPI:
    """Build a FastAPI test app whose screener_service masquerades as the real
    ScreenerService so _should_use_snapshot_first returns True, and whose db
    dependency is replaced with the supplied fake session."""

    # The real _should_use_snapshot_first gate checks __class__.__name__ and
    # __class__.__module__ — create a stub that passes both checks.
    class _SnapshotFirstScreenerStub:
        __module__ = "app.services.screener_service"

        def __init__(self) -> None:
            self.list_screening = AsyncMock(
                side_effect=AssertionError(
                    "external call should be skipped when snapshot is present"
                )
            )

    _SnapshotFirstScreenerStub.__name__ = "ScreenerService"
    _SnapshotFirstScreenerStub.__qualname__ = "ScreenerService"

    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubHomeService()
    app.dependency_overrides[get_screener_service_dep] = lambda: (
        _SnapshotFirstScreenerStub()
    )

    async def _override_get_db():
        yield fake_session

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.mark.unit
def test_consecutive_gainers_endpoint_separates_served_from_data_basis() -> None:
    """ROB-277 e2e: with a stale invest_screener_snapshots KR partition,
    /invest/api/screener/results must surface the partition date — not now() —
    in the freshness object.

    The snapshot is dated 2026-05-13 (7 days before today 2026-05-20), which
    classifies as 'stale'.  The endpoint's freshness.primary.snapshotDate must
    reflect that partition date, not the moment the request is served.
    """
    # Stale KR snapshot: dated 2026-05-13 (7 calendar days before 2026-05-20)
    stale_date = dt.date(2026, 5, 13)
    computed = dt.datetime(2026, 5, 13, 0, 30, tzinfo=UTC)
    snap = _RouterFakeSnapshot(
        symbol="005930",
        snapshot_date=stale_date,
        computed_at=computed,
        consecutive_up_days=6,
        week_change_rate=Decimal("3.5"),
    )
    name_row = _router_name_row("005930", "삼성전자")

    # DB call order (see screener_service.build_screener_results + dependencies):
    #  0: build_relation_resolver → watch items → .all()
    #  1: _load_consecutive_gainers_from_snapshots → MAX(snapshot_date) → scalar_one_or_none()
    #  2: _load_consecutive_gainers_from_snapshots → qualifying rows → scalars().all()
    #  3: _load_consecutive_gainers_from_snapshots → KR symbol names (filter) → .all()
    #  4: _async_enrich → repo.get_fresh → scalars().all()
    #  5: Bulk KR names → .all()
    #  6: investor-flow chip hydration → latest_by_symbols → scalars().all()
    fake_session = _RouterFakeSession(
        [
            _RouterFakeExecuteResult(rows=[]),  # 0: watch items
            _RouterFakeExecuteResult(scalar_rows=[stale_date]),  # 1: MAX(snapshot_date)
            _RouterFakeExecuteResult(scalar_rows=[snap]),  # 2: qualifying rows
            _RouterFakeExecuteResult(rows=[name_row]),  # 3: KR symbol names (filter)
            _RouterFakeExecuteResult(scalar_rows=[snap]),  # 4: enrichment get_fresh
            _RouterFakeExecuteResult(rows=[name_row]),  # 5: bulk KR names
            _RouterFakeExecuteResult(scalar_rows=[]),  # 6: no investor-flow chip
        ]
    )

    client = TestClient(_build_snapshot_first_app(fake_session))
    resp = client.get(
        "/invest/api/screener/results",
        params={"preset": "consecutive_gainers", "market": "kr"},
    )
    assert resp.status_code == 200
    body = resp.json()
    f = body["freshness"]

    # D2: top-level source enum unchanged — snapshot still surfaces as "cached"
    assert f["source"] == "cached", f"expected source='cached', got {f['source']!r}"

    # D1.a: primary block carries screener_snapshot kind + partition date
    assert f["primary"] is not None, (
        "freshness.primary must not be None for snapshot path"
    )
    assert f["primary"]["kind"] == "screener_snapshot"
    assert f["primary"]["snapshotDate"] == "2026-05-13", (
        f"primary.snapshotDate must be the seeded partition date, got {f['primary']['snapshotDate']!r}"
    )

    # D1: data-basis label reflects the snapshot partition, NOT the request time
    seeded_dot = "2026.05.13"
    assert seeded_dot in f["primary"]["asOfLabel"], (
        f"primary.asOfLabel should contain {seeded_dot!r}, got {f['primary']['asOfLabel']!r}"
    )
    assert seeded_dot in f["asOfLabel"], (
        f"top-level asOfLabel should mirror primary for snapshot-first, got {f['asOfLabel']!r}"
    )

    # ROB-277: servedAt is the response time (present and non-empty)
    assert f.get("servedAt") is not None, "freshness.servedAt must be present"

    # D1.c: dataState is the alias for overallState
    assert f["dataState"] == f["overallState"], (
        f"dataState ({f['dataState']!r}) must equal overallState ({f['overallState']!r})"
    )
