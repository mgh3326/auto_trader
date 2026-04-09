"""Tests for portfolio_rotation_service."""

from __future__ import annotations

import pytest

from app.services.portfolio_rotation_service import PortfolioRotationService


class TestBuildRotationPlan:
    """Tests for PortfolioRotationService.build_rotation_plan."""

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    async def test_unsupported_market_returns_not_supported(
        self, service: PortfolioRotationService
    ):
        result = await service.build_rotation_plan(market="kr")
        assert result["supported"] is False
        assert result["market"] == "kr"
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_unsupported_market_us(
        self, service: PortfolioRotationService
    ):
        result = await service.build_rotation_plan(market="us")
        assert result["supported"] is False
