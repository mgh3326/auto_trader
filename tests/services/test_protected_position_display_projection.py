"""#728 read-only protection field propagation for portfolio display models."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.manual_holdings import MarketType
from app.schemas.invest_home import Holding
from app.services.invest_home_service import build_grouped_holdings
from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.portfolio_data_collector import PortfolioDataCollector
from app.services.portfolio_overview_service import PortfolioOverviewService


def _holding(**overrides):
    values = {
        "holdingId": "h1",
        "accountId": "kis-main",
        "source": "kis",
        "accountKind": "live",
        "symbol": "005930",
        "market": "KR",
        "assetType": "equity",
        "assetCategory": "kr_stock",
        "displayName": "삼성전자",
        "quantity": 10.0,
        "currency": "KRW",
        "sellableQuantity": 8.0,
        "sellableObserved": True,
        "brokerSellableQuantity": 8.0,
        "protectedQuantity": 3.0,
        "tacticalSellableQuantity": 5.0,
        "protectionState": "covered",
    }
    values.update(overrides)
    return Holding(**values)


def test_grouped_holdings_keep_raw_s_tactical_headroom_and_account_components() -> None:
    grouped = build_grouped_holdings(
        [
            _holding(),
            _holding(
                holdingId="h2",
                accountId="upbit-main",
                source="upbit",
                symbol="KRW-BTC",
                market="CRYPTO",
                assetType="crypto",
                assetCategory="crypto",
                displayName="비트코인",
                quantity=2.0,
                brokerSellableQuantity=None,
                tacticalSellableQuantity=None,
                protectedQuantity=1.0,
                protectionState="unverified",
            ),
        ]
    )
    kr = next(item for item in grouped if item.symbol == "005930")
    crypto = next(item for item in grouped if item.symbol == "KRW-BTC")
    assert kr.brokerSellableQuantity == 8.0
    assert kr.tacticalSellableQuantity == 5.0
    assert kr.protectedQuantity == 3.0
    assert kr.sourceBreakdown[0].tacticalSellableQuantity == 5.0
    assert crypto.brokerSellableQuantity is None
    assert crypto.tacticalSellableQuantity is None
    assert crypto.protectionState == "unverified"


@pytest.mark.asyncio
async def test_collector_preserves_missing_kis_sellable_as_unverified_evidence() -> (
    None
):
    collector = PortfolioDataCollector(MagicMock())
    client = AsyncMock()
    client.fetch_my_stocks.return_value = [
        {
            "hldg_qty": "10",
            "pchs_avg_pric": "70000",
            "prpr": "71000",
            "evlu_amt": "710000",
            "evlu_pfls_amt": "10000",
            "evlu_pfls_rt": "1",
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "ord_psbl_qty": "8",
        },
        {
            "hldg_qty": "3",
            "pchs_avg_pric": "10000",
            "prpr": "11000",
            "evlu_amt": "33000",
            "evlu_pfls_amt": "3000",
            "evlu_pfls_rt": "1",
            "pdno": "000660",
            "prdt_name": "SK",
            "ord_psbl_qty": "",
        },
    ]
    components, warnings = await collector._collect_kis_kr_components(client, [])
    assert warnings == []
    assert components[0]["broker_sellable_quantity"] == 8.0
    assert components[0]["sellable_observed"] is True
    assert components[1]["broker_sellable_quantity"] is None
    assert components[1]["sellable_observed"] is False


def test_portfolio_overview_components_and_aggregate_preserve_protection_fields() -> (
    None
):
    service = PortfolioOverviewService(MagicMock())
    components = [
        {
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "account_key": "live:kis",
            "broker": "kis",
            "account_name": "KIS",
            "source": "live",
            "quantity": 10.0,
            "avg_price": 70000.0,
            "current_price": 71000.0,
            "evaluation": 710000.0,
            "profit_loss": 10000.0,
            "profit_rate": 0.01,
            "broker_sellable_quantity": 8.0,
            "sellable_observed": True,
            "protected_quantity": 3.0,
            "tactical_sellable_quantity": 5.0,
            "protection_state": "encroached",
        }
    ]
    rows = service._aggregate_positions(components)
    assert rows[0]["broker_sellable_quantity"] == 8.0
    assert rows[0]["protected_quantity"] == 3.0
    assert rows[0]["tactical_sellable_quantity"] == 5.0
    assert rows[0]["protection_state"] == "encroached"
    assert rows[0]["components"][0]["broker_sellable_quantity"] == 8.0
    assert rows[0]["components"][0]["tactical_sellable_quantity"] == 5.0


@pytest.mark.asyncio
async def test_merged_holding_keeps_raw_and_tactical_fields_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MergedPortfolioService(MagicMock())
    merged = {}
    service._apply_kis_holdings(
        merged,
        [
            {
                "pdno": "005930",
                "prdt_name": "삼성전자",
                "hldg_qty": "10",
                "pchs_avg_pric": "70000",
                "prpr": "71000",
                "evlu_amt": "710000",
                "evlu_pfls_amt": "10000",
                "evlu_pfls_rt": "1",
                "ord_psbl_qty": "8",
            }
        ],
        market_type=MarketType.KR,
    )

    async def project(_position, **_kwargs):
        return {
            "broker_sellable_quantity": 8.0,
            "protected_quantity": 3.0,
            "tactical_sellable_quantity": 5.0,
            "protection_state": "covered",
        }

    import app.services.protected_quantity_service as policy

    monkeypatch.setattr(policy, "apply_position_protection", project)
    await service._apply_protection_projection(merged)
    holding = merged["005930"]
    assert holding.broker_sellable_quantity == 8.0
    assert holding.protected_quantity == 3.0
    assert holding.tactical_sellable_quantity == 5.0
    assert holding.holdings[0].broker_sellable_quantity == 8.0
    assert holding.holdings[0].tactical_sellable_quantity == 5.0
