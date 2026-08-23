"""§144차 — /invest 매수 계획 router contract.

The route is read-only and the response is a funding aid, so what is pinned
here is the surface: GET-only, authenticated, market filter validated, and the
provenance fields the UI renders its disclaimer from.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.invest_buy_plan import (
    BuyPlanFunding,
    BuyPlanResponse,
    CurrencyReconciliation,
    PolicyStamp,
    SupportNetTier,
)

NOW = dt.datetime(2026, 8, 23, 3, 0, tzinfo=dt.UTC)


class _StubBuyPlanService:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def build(
        self, *, user_id: int, market: str = "all", now: dt.datetime | None = None
    ) -> BuyPlanResponse:
        self.calls.append((user_id, market))
        return BuyPlanResponse(
            as_of=NOW,
            policy=PolicyStamp(version="2026-08-23.1", content_hash="abc123"),
            cache_ttl_seconds=180,
            approximation_notice="정책 산식의 표시용 근사입니다.",
            market=market,  # type: ignore[arg-type]
            averaging_triggers=[],
            support_net=SupportNetTier(
                policy_key="buy.held_majors_support_net",
                enabled=True,
                currency="KRW",
                placed_notional=Decimal("0"),
            ),
            active_buy_watches=[],
            discovery_gates=[],
            funding=BuyPlanFunding(
                accounts=[],
                currencies=[
                    CurrencyReconciliation(
                        currency="KRW",
                        available_cash=Decimal("500000"),
                        required_averaging_adds=Decimal("120000"),
                        required_support_net=Decimal("0"),
                        required_active_watches=Decimal("0"),
                        required_total=Decimal("120000"),
                        verdict="sufficient",
                        shortfall=Decimal("0"),
                    )
                ],
            ),
        )


def _make_client(service: _StubBuyPlanService) -> TestClient:
    from app.routers import invest_buy_plan
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_buy_plan.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=7)
    app.dependency_overrides[invest_buy_plan.get_buy_plan_service] = lambda: service
    return TestClient(app)


@pytest.mark.unit
def test_buy_plan_defaults_to_all_markets() -> None:
    service = _StubBuyPlanService()
    response = _make_client(service).get("/trading/api/invest/buy-plan")

    assert response.status_code == 200
    assert response.json()["market"] == "all"
    assert service.calls == [(7, "all")]


@pytest.mark.unit
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_buy_plan_accepts_each_market_filter(market: str) -> None:
    service = _StubBuyPlanService()
    response = _make_client(service).get(
        f"/trading/api/invest/buy-plan?market={market}"
    )

    assert response.status_code == 200
    assert service.calls == [(7, market)]


@pytest.mark.unit
def test_buy_plan_rejects_an_unknown_market() -> None:
    response = _make_client(_StubBuyPlanService()).get(
        "/trading/api/invest/buy-plan?market=forex"
    )
    assert response.status_code == 422


@pytest.mark.unit
def test_buy_plan_is_read_only() -> None:
    """No mutating verb is exposed on this prefix."""

    client = _make_client(_StubBuyPlanService())
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/trading/api/invest/buy-plan")
        assert response.status_code == 405


@pytest.mark.unit
def test_buy_plan_requires_authentication() -> None:
    from app.routers import invest_buy_plan

    app = FastAPI()
    app.include_router(invest_buy_plan.router)
    app.dependency_overrides[invest_buy_plan.get_buy_plan_service] = lambda: (
        _StubBuyPlanService()
    )
    # get_authenticated_user is deliberately NOT overridden.
    response = TestClient(app).get("/trading/api/invest/buy-plan")
    assert response.status_code in (401, 403)


@pytest.mark.unit
def test_response_exposes_provenance_and_exact_decimal_strings() -> None:
    """Money is serialised as an exact string, never a lossy JSON float."""

    payload = (
        _make_client(_StubBuyPlanService()).get("/trading/api/invest/buy-plan").json()
    )

    assert payload["policy"] == {"version": "2026-08-23.1", "content_hash": "abc123"}
    assert payload["cache_ttl_seconds"] == 180
    assert "근사" in payload["approximation_notice"]
    krw = payload["funding"]["currencies"][0]
    assert krw["available_cash"] == "500000"
    assert krw["required_total"] == "120000"
    assert krw["verdict"] == "sufficient"
