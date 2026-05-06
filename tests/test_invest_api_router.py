from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import get_invest_home_service
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_home import (
    Account,
    CashAmounts,
    Holding,
    InvestHomeResponse,
    InvestHomeResponseMeta,
    InvestHomeWarning,
)
from app.services.invest_home_service import build_grouped_holdings, build_home_summary


class _StubService:
    async def get_home(self, *, user_id: int) -> InvestHomeResponse:
        accounts = [
            Account(
                accountId="a1",
                displayName="KIS",
                source="kis",
                accountKind="live",
                includedInHome=True,
                valueKrw=10_000_000,
                costBasisKrw=9_000_000,
                pnlKrw=1_000_000,
                pnlRate=1 / 9,
                cashBalances=CashAmounts(krw=100, usd=1),
                buyingPower=CashAmounts(krw=100, usd=1),
            ),
            Account(
                accountId="a2",
                displayName="Mock",
                source="kis_mock",
                accountKind="paper",
                includedInHome=False,
                valueKrw=99,
                costBasisKrw=None,
                pnlKrw=None,
                pnlRate=None,
                cashBalances=CashAmounts(),
                buyingPower=CashAmounts(),
            ),
        ]
        holdings = [
            Holding(
                holdingId="h1",
                accountId="a1",
                source="kis",
                accountKind="live",
                symbol="005930",
                market="KR",
                assetType="equity",
                displayName="삼성전자",
                quantity=10,
                averageCost=70000,
                costBasis=700_000,
                currency="KRW",
                valueNative=720_000,
                valueKrw=720_000,
                pnlKrw=20_000,
                pnlRate=20_000 / 700_000,
            ),
        ]
        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings),
            meta=InvestHomeResponseMeta(
                warnings=[
                    InvestHomeWarning(source="upbit", message="cache only"),
                ]
            ),
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: type(
        "U", (), {"id": 1}
    )()
    app.dependency_overrides[get_invest_home_service] = lambda: _StubService()
    return TestClient(app)


@pytest.mark.unit
def test_get_home_returns_200_with_schema(client: TestClient) -> None:
    r = client.get("/invest/api/home")
    assert r.status_code == 200
    body = r.json()
    assert body["homeSummary"]["totalValueKrw"] == 10_000_000  # mock 제외
    assert "kis_mock" in body["homeSummary"]["excludedSources"]
    assert any(
        a["source"] == "kis_mock" and a["includedInHome"] is False
        for a in body["accounts"]
    )
    assert body["groupedHoldings"][0]["groupId"] == "KR:equity:KRW:005930"
    assert body["meta"]["warnings"][0]["source"] == "upbit"
