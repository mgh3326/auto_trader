"""ROB-226 router tests for read-only /invest/api/crypto endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.invest_api as invest_api
from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import get_invest_home_service
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_crypto import (
    CryptoDashboardMeta,
    CryptoDashboardResponse,
    CryptoInsightsSummary,
    NaverCryptoReferenceResponse,
)
from app.schemas.invest_home import InvestHomeResponse, InvestHomeResponseMeta
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


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 7}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubHomeService()

    async def _db_override():
        yield object()

    app.dependency_overrides[get_db] = _db_override
    return app


@pytest.mark.unit
def test_crypto_dashboard_endpoint_uses_read_only_view_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_relation_resolver(
        db: Any, *, user_id: int, held_pairs: set[tuple[str, str]]
    ):
        calls["relation"] = {"db": db, "user_id": user_id, "held_pairs": held_pairs}
        return object()

    async def fake_dashboard(**kwargs: Any) -> CryptoDashboardResponse:
        calls["dashboard"] = kwargs
        return CryptoDashboardResponse(
            asOf=datetime(2026, 5, 13, 12, tzinfo=UTC),
            cards=[],
            holdings=None,
            pendingOrders=None,
            insights=CryptoInsightsSummary(notes=["read-only"]),
            meta=CryptoDashboardMeta(warnings=[], sources=[]),
        )

    monkeypatch.setattr(invest_api, "build_relation_resolver", fake_relation_resolver)
    monkeypatch.setattr(invest_api, "build_crypto_dashboard", fake_dashboard)

    response = TestClient(_build_app()).get("/invest/api/crypto/dashboard?limit=3")

    assert response.status_code == 200
    assert response.json()["market"] == "crypto"
    assert calls["relation"]["user_id"] == 7
    assert calls["dashboard"]["user_id"] == 7
    assert calls["dashboard"]["limit"] == 3
    assert calls["dashboard"]["resolver"] is not None


@pytest.mark.unit
def test_crypto_naver_reference_endpoint_uses_read_only_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_relation_resolver(
        db: Any, *, user_id: int, held_pairs: set[tuple[str, str]]
    ):
        calls["relation"] = {"db": db, "user_id": user_id, "held_pairs": held_pairs}
        return object()

    async def fake_reference(**kwargs: Any) -> NaverCryptoReferenceResponse:
        calls["reference"] = kwargs
        return NaverCryptoReferenceResponse(
            asOf=datetime(2026, 5, 14, 12, tzinfo=UTC),
            symbol="KRW-BTC",
            rank=[],
            profile=None,
            news=None,
            kimchiPremium=None,
            sources=[],
            warnings=["read_only_no_order_watch_or_broker_mutation"],
        )

    monkeypatch.setattr(invest_api, "build_relation_resolver", fake_relation_resolver)
    monkeypatch.setattr(invest_api, "build_naver_crypto_reference", fake_reference)

    response = TestClient(_build_app()).get(
        "/invest/api/crypto/naver-reference?symbol=BTC&limit=4"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["market"] == "crypto"
    assert payload["symbol"] == "KRW-BTC"
    assert "read_only_no_order_watch_or_broker_mutation" in payload["warnings"]
    assert calls["relation"]["user_id"] == 7
    assert calls["reference"]["symbol"] == "BTC"
    assert calls["reference"]["limit"] == 4
    assert calls["reference"]["resolver"] is not None
