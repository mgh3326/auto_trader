from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.portfolio_overview_service import PortfolioOverviewService


def _sample_components() -> list[dict[str, object]]:
    return [
        {
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "account_key": "live:kis",
            "broker": "kis",
            "account_name": "KIS 실계좌",
            "source": "live",
            "quantity": 10.0,
            "avg_price": 70000.0,
            "current_price": 75000.0,
            "evaluation": 750000.0,
            "profit_loss": 50000.0,
            "profit_rate": 0.0714,
        },
        {
            "market_type": "KR",
            "symbol": "005930",
            "name": "삼성전자",
            "account_key": "manual:1",
            "broker": "toss",
            "account_name": "토스 계좌",
            "source": "manual",
            "quantity": 5.0,
            "avg_price": 72000.0,
            "current_price": 75000.0,
            "evaluation": 375000.0,
            "profit_loss": 15000.0,
            "profit_rate": 0.0417,
        },
        {
            "market_type": "US",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "account_key": "manual:2",
            "broker": "samsung",
            "account_name": "미국주식",
            "source": "manual",
            "quantity": 2.0,
            "avg_price": 150.0,
            "current_price": 160.0,
            "evaluation": 320.0,
            "profit_loss": 20.0,
            "profit_rate": 0.0667,
        },
        {
            "market_type": "CRYPTO",
            "symbol": "KRW-BTC",
            "name": "KRW-BTC",
            "account_key": "live:upbit",
            "broker": "upbit",
            "account_name": "Upbit 실계좌",
            "source": "live",
            "quantity": 0.1,
            "avg_price": 100000000.0,
            "current_price": 110000000.0,
            "evaluation": 11000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 0.1,
        },
    ]


@pytest.mark.asyncio
async def test_get_overview_filters_by_selected_account_keys() -> None:
    service = PortfolioOverviewService(AsyncMock())
    components = _sample_components()

    service._collect_kis_components = AsyncMock(return_value=components[:1])
    service._collect_upbit_components = AsyncMock(return_value=components[3:])
    service._collect_manual_components = AsyncMock(return_value=components[1:3])
    service._fill_missing_prices = AsyncMock(return_value=None)

    overview = await service.get_overview(
        user_id=1,
        market="ALL",
        account_keys=["live:kis", "manual:1"],
        q=None,
    )

    assert overview["summary"]["total_positions"] == 1
    assert overview["summary"]["by_market"] == {"KR": 1, "US": 0, "CRYPTO": 0}
    position = overview["positions"][0]
    assert position["symbol"] == "005930"
    assert position["quantity"] == 15.0
    assert len(position["components"]) == 2


@pytest.mark.asyncio
async def test_get_overview_applies_market_and_q_filters() -> None:
    service = PortfolioOverviewService(AsyncMock())
    components = _sample_components()

    service._collect_kis_components = AsyncMock(return_value=components[:1])
    service._collect_upbit_components = AsyncMock(return_value=components[3:])
    service._collect_manual_components = AsyncMock(return_value=components[1:3])
    service._fill_missing_prices = AsyncMock(return_value=None)

    overview = await service.get_overview(
        user_id=1,
        market="US",
        account_keys=None,
        q="apple",
    )

    assert overview["filters"]["market"] == "US"
    assert overview["summary"]["total_positions"] == 1
    assert overview["summary"]["by_market"] == {"KR": 0, "US": 1, "CRYPTO": 0}
    assert overview["positions"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_get_overview_includes_deduplicated_warnings() -> None:
    service = PortfolioOverviewService(AsyncMock())

    async def collect_kis(_kis_client, warnings):
        warnings.append("KIS warning")
        warnings.append("KIS warning")
        return []

    async def collect_upbit(warnings):
        warnings.append("Upbit warning")
        return []

    async def collect_manual(_user_id, warnings):
        warnings.append("KIS warning")
        return []

    service._collect_kis_components = collect_kis
    service._collect_upbit_components = collect_upbit
    service._collect_manual_components = collect_manual
    service._fill_missing_prices = AsyncMock(return_value=None)

    overview = await service.get_overview(user_id=1)
    assert overview["warnings"] == ["KIS warning", "Upbit warning"]


def test_aggregate_positions_recalculates_totals_when_some_components_missing_eval() -> None:
    service = PortfolioOverviewService(AsyncMock())

    rows = service._aggregate_positions(
        [
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "live:kis",
                "broker": "kis",
                "account_name": "KIS 실계좌",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 70000.0,
                "current_price": 75000.0,
                "evaluation": 750000.0,
                "profit_loss": 50000.0,
                "profit_rate": 0.0714,
            },
            {
                "market_type": "KR",
                "symbol": "005930",
                "name": "삼성전자",
                "account_key": "manual:1",
                "broker": "toss",
                "account_name": "토스 계좌",
                "source": "manual",
                "quantity": 5.0,
                "avg_price": 72000.0,
                "current_price": None,
                "evaluation": None,
                "profit_loss": None,
                "profit_rate": None,
            },
        ]
    )

    assert len(rows) == 1
    assert rows[0]["quantity"] == 15.0
    # (10 + 5) * 75,000
    assert rows[0]["evaluation"] == 1125000.0
    # 1,125,000 - ((10 * 70,000) + (5 * 72,000))
    assert rows[0]["profit_loss"] == 65000.0
