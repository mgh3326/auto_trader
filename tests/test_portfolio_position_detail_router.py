from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import portfolio


class _FakeDetailService:
    def __init__(self) -> None:
        self.get_page_payload = AsyncMock(
            return_value={
                "summary": {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA Corp.",
                    "current_price": 132.0,
                    "quantity": 3.0,
                    "avg_price": 120.0,
                    "profit_loss": 36.0,
                    "profit_rate": 0.1,
                    "evaluation": 396.0,
                    "account_count": 2,
                    "target_distance_pct": 9.85,
                    "stop_distance_pct": -10.61,
                },
                "components": [],
                "journal": {"strategy": "trend"},
            }
        )


def _create_client() -> tuple[TestClient, _FakeDetailService]:
    app = FastAPI()
    detail = _FakeDetailService()
    app.include_router(portfolio.router)
    app.dependency_overrides[portfolio.get_authenticated_user] = lambda: (
        SimpleNamespace(id=7)
    )
    app.dependency_overrides[portfolio.get_portfolio_position_detail_service] = lambda: (
        detail
    )
    return TestClient(app), detail


@pytest.mark.unit
def test_position_detail_page_renders_summary_shell() -> None:
    client, detail = _create_client()
    response = client.get("/portfolio/positions/us/NVDA")

    assert response.status_code == 200
    body = response.text
    assert 'id="position-detail-page"' in body
    assert "Trade Journal" in body
    assert "최근 뉴스" in body
    assert "애널리스트 의견" in body
    detail.get_page_payload.assert_awaited_once_with(
        user_id=7, market_type="us", symbol="NVDA"
    )


@pytest.mark.unit
def test_position_detail_page_returns_404_when_symbol_missing() -> None:
    client, detail = _create_client()
    detail.get_page_payload.side_effect = (
        portfolio.PortfolioPositionDetailNotFoundError("NVDA")
    )

    response = client.get("/portfolio/positions/us/NVDA")

    assert response.status_code == 404
