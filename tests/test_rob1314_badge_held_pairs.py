"""ROB-1314 — badge-driven routes must not build the full home projection.

These routes only need the held-symbol set for relation badges. They must
consume `InvestHomeService.get_held_pairs` (shared portfolio snapshot / manual
DB key reader, ROB-1310 contract) instead of calling `service.get_home(...)`,
which fans out to every live reader.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import app.routers.invest_api as invest_api
from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import (
    get_invest_home_service,
    get_screener_service_dep,
)
from app.routers.invest_api import router as invest_api_router

ROUTES: list[tuple[str, dict[str, Any]]] = [
    ("/invest/api/crypto/dashboard", {}),
    ("/invest/api/crypto/naver-reference", {"symbol": "BTC"}),
    ("/invest/api/signals", {}),
    ("/invest/api/feed/news", {}),
    ("/invest/api/feed/research", {}),
    ("/invest/api/screener/results", {"preset": "default", "market": "kr"}),
]


class _BadgeOnlyService:
    """get_home raises: badge routes must never reach the full projection."""

    def __init__(self) -> None:
        self.held_pairs_calls = 0

    async def get_home(self, **_kwargs: Any) -> Any:
        raise AssertionError("ROB-1314: badge route called service.get_home()")

    async def get_held_pairs(
        self,
        *,
        user_id: int,
        include_paper: bool = False,
        paper_sources: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        self.held_pairs_calls += 1
        return [("kr", "005930"), ("crypto", "KRW-BTC")]


class _DBStub:
    async def execute(self, *_args: Any, **_kw: Any) -> Any:
        class _R:
            def all(self) -> list[Any]:
                return []

            def scalars(self) -> Any:
                class _S:
                    def all(self) -> list[Any]:
                        return []

                return _S()

        return _R()


def _build_app(service: _BadgeOnlyService) -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: service
    app.dependency_overrides[get_db] = lambda: iter([_DBStub()])

    class _ScreeningStub:
        async def list_results(self, *_a: Any, **_kw: Any) -> list[Any]:
            return []

        async def get_preset(self, *_a: Any, **_kw: Any) -> None:
            return None

    app.dependency_overrides[get_screener_service_dep] = lambda: _ScreeningStub()
    return app


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path,params", ROUTES)
async def test_badge_routes_use_held_pairs_and_never_call_get_home(
    monkeypatch: pytest.MonkeyPatch, path: str, params: dict[str, Any]
) -> None:
    from app.schemas.invest_crypto import (
        CryptoDashboardMeta,
        CryptoDashboardResponse,
        CryptoInsightsSummary,
        NaverCryptoReferenceResponse,
    )
    from app.schemas.invest_feed_news import FeedNewsResponse
    from app.schemas.invest_feed_research import FeedResearchMeta, FeedResearchResponse
    from app.schemas.invest_screener import ScreenerResultsResponse
    from app.schemas.invest_signals import SignalsResponse

    service = _BadgeOnlyService()

    async def _resolver(db: Any, *, user_id: int, held_pairs: Any) -> object:
        return object()

    async def _crypto_dashboard(**_kw: Any) -> CryptoDashboardResponse:
        return CryptoDashboardResponse(
            asOf=datetime.now(UTC),
            cards=[],
            holdings=None,
            pendingOrders=None,
            insights=CryptoInsightsSummary(notes=[]),
            meta=CryptoDashboardMeta(warnings=[], sources=[]),
        )

    async def _naver_reference(**_kw: Any) -> NaverCryptoReferenceResponse:
        return NaverCryptoReferenceResponse(
            asOf=datetime.now(UTC),
            symbol="KRW-BTC",
            rank=[],
            profile=None,
            news=None,
            kimchiPremium=None,
            sources=[],
            warnings=[],
        )

    async def _signals(**_kw: Any) -> SignalsResponse:
        return SignalsResponse(tab="mine", asOf=datetime.now(UTC), items=[])

    async def _feed_news(**_kw: Any) -> FeedNewsResponse:
        return FeedNewsResponse(tab="top", asOf=datetime.now(UTC))

    async def _feed_research(**_kw: Any) -> FeedResearchResponse:
        return FeedResearchResponse(
            tab="top",
            asOf=datetime.now(UTC),
            items=[],
            nextCursor=None,
            meta=FeedResearchMeta(limit=30, appliedFilters={}),
        )

    async def _screener_results(**_kw: Any) -> ScreenerResultsResponse:
        return ScreenerResultsResponse(
            presetId="default",
            title="t",
            description="d",
            filterChips=[],
            metricLabel="m",
            results=[],
            freshness={
                "fetchedAt": "2026-08-23T00:00:00Z",
                "asOfLabel": "now",
                "relativeLabel": "now",
                "cacheHit": False,
                "source": "live",
            },
        )

    monkeypatch.setattr(invest_api, "build_relation_resolver", _resolver)
    monkeypatch.setattr(invest_api, "build_crypto_dashboard", _crypto_dashboard)
    monkeypatch.setattr(invest_api, "build_naver_crypto_reference", _naver_reference)
    monkeypatch.setattr(invest_api, "build_signals", _signals)
    monkeypatch.setattr(invest_api, "build_feed_news", _feed_news)
    monkeypatch.setattr(invest_api, "build_feed_research", _feed_research)
    monkeypatch.setattr(invest_api, "build_screener_results", _screener_results)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app(service), raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(path, params=params)

    assert response.status_code == 200, response.text
    assert service.held_pairs_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_badge_routes_surface_typed_503_on_cold_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold snapshot must fail closed with the ROB-1310 503 contract."""
    from app.services.invest_home_service import PortfolioSnapshotUnavailableError

    class _ColdService(_BadgeOnlyService):
        async def get_held_pairs(self, **_kwargs: Any) -> list[tuple[str, str]]:
            raise PortfolioSnapshotUnavailableError("held_key_projection_missing")

    async def _resolver(db: Any, *, user_id: int, held_pairs: Any) -> object:
        return object()

    async def _signals(**_kw: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("builder ran after 503")

    monkeypatch.setattr(invest_api, "build_relation_resolver", _resolver)
    monkeypatch.setattr(invest_api, "build_signals", _signals)

    async with AsyncClient(
        transport=ASGITransport(app=_build_app(_ColdService())),
        base_url="http://test",
    ) as client:
        response = await client.get("/invest/api/signals")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["source"] == "portfolio_snapshot"
    assert detail["error_code"] == "portfolio_snapshot_unavailable"
    assert detail["unavailable_reason"] == "held_key_projection_missing"
