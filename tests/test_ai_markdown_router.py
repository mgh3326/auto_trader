"""Tests for AI Markdown Router"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# create_app()을 사용하여 API 인스턴스 생성
api = create_app()


@pytest.fixture
def client():
    return TestClient(api)


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = 1
    user.username = "testuser"
    return user


@pytest.fixture
def auth_headers(mock_user):
    with patch(
        "app.routers.ai_markdown.get_authenticated_user",
        return_value=mock_user,
    ), patch(
        "app.middleware.auth.AuthMiddleware._load_user",
        new_callable=AsyncMock,
        return_value=mock_user,
    ):
        yield {"Authorization": "Bearer test-token"}


class TestGeneratePortfolioMarkdown:
    @pytest.mark.asyncio
    async def test_success(self, client, auth_headers):
        mock_overview = {
            "success": True,
            "positions": [
                {"symbol": "AAPL", "evaluation": 5000000}
            ],
        }
        mock_markdown = {
            "title": "Test Title",
            "content": "# Test Content",
            "filename": "test.md",
            "metadata": {"position_count": 1},
        }

        with patch(
            "app.routers.ai_markdown.PortfolioOverviewService.get_overview",
            new_callable=AsyncMock,
            return_value=mock_overview,
        ), patch(
            "app.routers.ai_markdown.AIMarkdownService.generate_portfolio_stance_markdown",
            return_value=mock_markdown,
        ):
            response = client.post(
                "/api/ai-markdown/portfolio",
                json={"preset": "portfolio_stance", "include_market": "ALL"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["preset"] == "portfolio_stance"
        assert "Test Title" in data["title"]

    @pytest.mark.asyncio
    async def test_portfolio_fetch_failure(self, client, auth_headers):
        with patch(
            "app.routers.ai_markdown.PortfolioOverviewService.get_overview",
            new_callable=AsyncMock,
            return_value={"success": False},
        ):
            response = client.post(
                "/api/ai-markdown/portfolio",
                json={"preset": "portfolio_stance"},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Failed to fetch" in data["error"]


class TestGenerateStockMarkdown:
    @pytest.mark.asyncio
    async def test_stock_stance_success(self, client, auth_headers):
        mock_payload = {
            "summary": {"symbol": "AAPL", "name": "Apple"},
            "weights": {"portfolio_weight_pct": 10},
        }
        mock_markdown = {
            "title": "AAPL Stance",
            "content": "# AAPL",
            "filename": "stock-AAPL-stance.md",
            "metadata": {"symbol": "AAPL"},
        }

        with patch(
            "app.routers.ai_markdown.PortfolioPositionDetailService.get_page_payload",
            new_callable=AsyncMock,
            return_value=mock_payload,
        ), patch(
            "app.routers.ai_markdown.AIMarkdownService.generate_stock_stance_markdown",
            return_value=mock_markdown,
        ):
            response = client.post(
                "/api/ai-markdown/stock",
                json={
                    "preset": "stock_stance",
                    "symbol": "AAPL",
                    "market_type": "US",
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["preset"] == "stock_stance"

    @pytest.mark.asyncio
    async def test_stock_not_found(self, client, auth_headers):
        from app.services.portfolio_position_detail_service import (
            PortfolioPositionDetailNotFoundError,
        )

        with patch(
            "app.routers.ai_markdown.PortfolioPositionDetailService.get_page_payload",
            new_callable=AsyncMock,
            side_effect=PortfolioPositionDetailNotFoundError("AAPL"),
        ):
            response = client.post(
                "/api/ai-markdown/stock",
                json={
                    "preset": "stock_stance",
                    "symbol": "AAPL",
                    "market_type": "US",
                },
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["error"].lower()
