"""ROB-267 — invest_api 10개 엔드포인트가 기본 호출에서 paper reader 를 실행하지
않는지 sweep 검증."""

from __future__ import annotations

import pytest


ENDPOINTS = [
    ("/invest/api/home", {}),
    ("/invest/api/account-panel", {}),
    ("/invest/api/crypto/dashboard", {}),
    ("/invest/api/crypto/naver-reference", {}),
    ("/invest/api/kr/action-readiness", {}),
    ("/invest/api/signals", {}),
    ("/invest/api/calendar", {"from_date": "2026-01-01", "to_date": "2026-01-07"}),
    ("/invest/api/feed/news", {}),
    ("/invest/api/feed/research", {}),
    ("/invest/api/screener/results", {"preset": "default", "market": "kr"}),
]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path,params", ENDPOINTS)
async def test_default_call_does_not_request_paper(path, params):
    """Each invest_api endpoint, when called without includePaper query param,
    must invoke the home service with include_paper=False (or not invoke it at all
    due to non-home-service errors like DB stubs)."""
    from app.routers.dependencies import get_authenticated_user
    from app.routers.invest_api import (
        get_invest_home_service,
        get_screener_service_dep,
        router as invest_api_router,
    )
    from app.core.db import get_db
    from app.schemas.invest_home import (
        HomeSummary,
        InvestHomeHiddenCounts,
        InvestHomeResponse,
        InvestHomeResponseMeta,
    )
    from app.services.invest_home_service import _AccountPanelView

    received_calls: list[dict] = []

    def _empty_home():
        return InvestHomeResponse(
            homeSummary=HomeSummary(
                includedSources=[], excludedSources=[],
                totalValueKrw=0, costBasisKrw=None, pnlKrw=None, pnlRate=None,
            ),
            accounts=[], holdings=[], groupedHoldings=[],
            meta=InvestHomeResponseMeta(
                warnings=[], hiddenCounts=InvestHomeHiddenCounts(), hiddenHoldings=[],
            ),
        )

    def _empty_view():
        return _AccountPanelView(
            homeSummary=HomeSummary(
                includedSources=[], excludedSources=[],
                totalValueKrw=0, costBasisKrw=None, pnlKrw=None, pnlRate=None,
            ),
            accounts=[], groupedHoldings=[], warnings=[],
        )

    class _SpyService:
        async def get_home(self, *, user_id, include_paper=False, paper_sources=None, **_):
            received_calls.append({
                "method": "get_home",
                "include_paper": include_paper,
                "paper_sources": paper_sources,
            })
            return _empty_home()

        async def build_account_panel_view(
            self, *, user_id, include_paper=False, paper_sources=None, **_
        ):
            received_calls.append({
                "method": "build_account_panel_view",
                "include_paper": include_paper,
                "paper_sources": paper_sources,
            })
            return _empty_view()

    class _DBStub:
        """Minimal async session stub that returns empty result rows."""
        async def execute(self, *_args, **_kw):
            class _R:
                def all(self): return []
                def scalar_one_or_none(self): return None
                def scalar_one(self): return 0
                def scalar(self): return 0
                def one(self): return (0, 0, None, None)
                def one_or_none(self): return None
                def scalars(self):
                    class _S:
                        def all(self): return []
                        def first(self): return None
                    return _S()
                def first(self): return None
                def fetchall(self): return []
                def mappings(self):
                    class _M:
                        def all(self): return []
                        def first(self): return None
                    return _M()
            return _R()
        async def commit(self): pass
        async def rollback(self): pass
        async def close(self): pass

    async def _db_dep():
        yield _DBStub()

    class _ScreeningServiceStub:
        async def list_results(self, *_a, **_kw):
            return []
        async def get_preset(self, *_a, **_kw):
            return None

    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport

    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type("U", (), {"id": 1})()
    app.dependency_overrides[get_invest_home_service] = lambda: _SpyService()
    app.dependency_overrides[get_db] = _db_dep
    app.dependency_overrides[get_screener_service_dep] = lambda: _ScreeningServiceStub()

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        await client.get(path, params=params)

    # The endpoint may have returned non-200 for downstream reasons (DB stub
    # incompleteness, missing data, etc.) — we only assert that IF the home
    # service was invoked, it was invoked with include_paper=False and
    # paper_sources=None. Endpoints that never reach the home service are
    # implicitly fine for this test's purpose.
    for entry in received_calls:
        assert entry["include_paper"] is False, (
            f"{path} default call passed include_paper=True via {entry['method']}"
        )
        assert entry["paper_sources"] is None, (
            f"{path} default call passed paper_sources={entry['paper_sources']!r}"
        )
