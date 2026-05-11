from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.dependencies import get_authenticated_user
import app.routers.invest_api as invest_api
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
            Account(
                accountId="a3",
                displayName="Toss manual",
                source="toss_manual",
                accountKind="manual",
                includedInHome=True,
                valueKrw=280_000,
                costBasisKrw=260_000,
                pnlKrw=20_000,
                pnlRate=20_000 / 260_000,
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
                assetCategory="kr_stock",
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
            Holding(
                holdingId="h2",
                accountId="a3",
                source="toss_manual",
                accountKind="manual",
                symbol="005930",
                market="KR",
                assetType="equity",
                assetCategory="kr_stock",
                displayName="삼성전자",
                quantity=4,
                averageCost=65_000,
                costBasis=260_000,
                currency="KRW",
                valueNative=280_000,
                valueKrw=280_000,
                pnlKrw=20_000,
                pnlRate=20_000 / 260_000,
            ),
        ]
        return InvestHomeResponse(
            homeSummary=build_home_summary(accounts),
            accounts=accounts,
            holdings=holdings,
            groupedHoldings=build_grouped_holdings(holdings, accounts=accounts),
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
    assert body["homeSummary"]["totalValueKrw"] == 10_280_000  # mock 제외
    assert "kis_mock" in body["homeSummary"]["excludedSources"]
    assert any(
        a["source"] == "kis_mock" and a["includedInHome"] is False
        for a in body["accounts"]
    )
    assert body["groupedHoldings"][0]["groupId"] == "KR:equity:KRW:005930"
    assert {row["source"] for row in body["groupedHoldings"][0]["sourceBreakdown"]} == {"kis", "toss_manual"}
    assert body["meta"]["warnings"][0]["source"] == "upbit"



@pytest.mark.unit
def test_stock_detail_route_reuses_home_grouped_holdings_source_breakdown(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_build_stock_detail(**kwargs):
        home = await kwargs["holding_provider"](
            kwargs["user_id"], kwargs["market"], kwargs["symbol"], kwargs["db"]
        )
        grouped = home.groupedHoldings[0]
        captured["sources"] = [row.source for row in grouped.sourceBreakdown]
        captured["accounts"] = [row.accountName for row in grouped.sourceBreakdown]
        return {
            "symbol": kwargs["symbol"],
            "market": kwargs["market"],
            "displayName": "삼성전자",
            "exchange": "KRX",
            "instrumentType": "stock",
            "currency": "KRW",
            "assetType": "equity",
            "assetCategory": "kr_stock",
            "quote": None,
            "screenerSnapshot": None,
            "valuation": None,
            "holding": {
                "totalQuantity": grouped.totalQuantity,
                "averageCost": grouped.averageCost,
                "costBasis": grouped.costBasis,
                "valueNative": grouped.valueNative,
                "valueKrw": grouped.valueKrw,
                "pnlKrw": grouped.pnlKrw,
                "pnlRate": grouped.pnlRate,
                "includedSources": grouped.includedSources,
                "sourceBreakdown": [
                    {
                        "source": row.source,
                        "accountName": row.accountName,
                        "quantity": row.quantity,
                        "averageCost": row.averageCost,
                        "costBasis": row.costBasis,
                        "valueNative": row.valueNative,
                        "valueKrw": row.valueKrw,
                    }
                    for row in grouped.sourceBreakdown
                ],
                "priceState": grouped.priceState,
            },
            "latestAnalysis": None,
            "orderbookSupport": {"supported": False, "reason": "kr_unavailable"},
            "orderbook": None,
            "capabilities": {
                "candles": {"supported": True, "intradaySupported": True},
                "orderbook": {"supported": False, "reason": "kr_unavailable"},
                "news": {"supported": True, "reason": None},
                "orders": {"supported": True, "reason": None},
                "liveStreaming": {"supported": False, "reason": "out_of_mvp_scope"},
                "execution": {"supported": False, "reason": "read_only_mvp"},
                "options": {"supported": False, "reason": "out_of_mvp_scope"},
            },
            "meta": {
                "computedAt": "2026-05-11T00:00:00Z",
                "warnings": [],
                "blockStates": {
                    "quote": "missing",
                    "screenerSnapshot": "missing",
                    "valuation": "missing",
                    "holding": "fresh",
                    "latestAnalysis": "missing",
                    "orderbook": "unsupported",
                },
            },
        }

    monkeypatch.setattr(invest_api, "build_stock_detail", fake_build_stock_detail)

    r = client.get("/invest/api/stock-detail/kr/005930")

    assert r.status_code == 200
    body = r.json()
    assert captured == {
        "sources": ["kis", "toss_manual"],
        "accounts": ["KIS", "Toss manual"],
    }
    assert [row["source"] for row in body["holding"]["sourceBreakdown"]] == [
        "kis",
        "toss_manual",
    ]
