from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.portfolio_position_detail_service import (
    PortfolioPositionDetailNotFoundError,
    PortfolioPositionDetailService,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_returns_summary_components_and_journal() -> None:
    overview_service = MagicMock()
    overview_service.get_position_detail_base = AsyncMock(
        return_value={
            "market_type": "US",
            "symbol": "NVDA",
            "name": "NVIDIA Corp.",
            "quantity": 3.0,
            "avg_price": 120.0,
            "current_price": 132.0,
            "evaluation": 396.0,
            "profit_loss": 36.0,
            "profit_rate": 0.1,
            "components": [
                {
                    "broker": "kis",
                    "account_name": "ISA",
                    "source": "live",
                    "quantity": 2.0,
                    "avg_price": 118.0,
                    "current_price": 132.0,
                    "evaluation": 264.0,
                    "profit_loss": 28.0,
                    "profit_rate": 0.1186,
                },
                {
                    "broker": "toss",
                    "account_name": "미니스탁",
                    "source": "manual",
                    "quantity": 1.0,
                    "avg_price": 124.0,
                    "current_price": 132.0,
                    "evaluation": 132.0,
                    "profit_loss": 8.0,
                    "profit_rate": 0.0645,
                },
            ],
        }
    )
    dashboard_service = MagicMock()
    dashboard_service.get_latest_journal_snapshot = AsyncMock(
        return_value={
            "symbol": "NVDA",
            "strategy": "trend",
            "thesis": "AI capex leader",
            "target_price": 145.0,
            "stop_loss": 118.0,
            "target_distance_pct": 9.85,
            "stop_distance_pct": -10.61,
        }
    )

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )

    payload = await service.get_page_payload(user_id=7, market_type="us", symbol="NVDA")

    assert payload["summary"]["symbol"] == "NVDA"
    assert payload["summary"]["account_count"] == 2
    assert payload["journal"]["strategy"] == "trend"
    assert payload["summary"]["target_distance_pct"] == 9.85


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_payload_raises_when_position_missing() -> None:
    overview_service = MagicMock()
    overview_service.get_position_detail_base = AsyncMock(return_value=None)

    service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=MagicMock(),
    )

    with pytest.raises(PortfolioPositionDetailNotFoundError):
        await service.get_page_payload(user_id=7, market_type="kr", symbol="035720")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_opinions_payload_returns_crypto_fallback() -> None:
    service = PortfolioPositionDetailService(
        overview_service=MagicMock(),
        dashboard_service=MagicMock(),
    )

    payload = await service.get_opinions_payload(market_type="crypto", symbol="KRW-BTC")

    assert payload["supported"] is False
    assert payload["message"] == "애널리스트 의견이 제공되지 않는 시장입니다."
