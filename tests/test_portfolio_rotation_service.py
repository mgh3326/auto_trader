"""Tests for portfolio_rotation_service."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

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
    async def test_unsupported_market_us(self, service: PortfolioRotationService):
        result = await service.build_rotation_plan(market="us")
        assert result["supported"] is False


def _make_position(
    symbol: str = "KRW-BTC",
    name: str = "비트코인",
    evaluation_amount: float = 100_000,
    profit_rate: float = 5.0,
    current_price: float = 50_000_000,
    strategy_signal: dict | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "account": "upbit",
        "instrument_type": "crypto",
        "market": "crypto",
        "current_price": current_price,
        "evaluation_amount": evaluation_amount,
        "profit_rate": profit_rate,
        "profit_loss": evaluation_amount * profit_rate / 100,
        "avg_buy_price": current_price / (1 + profit_rate / 100),
        "quantity": 0.002,
        "strategy_signal": strategy_signal,
    }


def _make_journal(
    symbol: str = "KRW-BTC",
    strategy: str | None = "coinmoogi_dca",
    status: str = "active",
    hold_until: str | None = "2099-12-31T00:00:00",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "strategy": strategy,
        "status": status,
        "hold_until": hold_until,
    }


class TestClassifyPositions:
    """Tests for position classification logic."""

    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_locked_strategy_classification(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [_make_position(symbol="KRW-BTC", name="비트코인")],
            [],
        )
        mock_journals.return_value = {
            "KRW-BTC": _make_journal(symbol="KRW-BTC", strategy="coinmoogi_dca"),
        }
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert result["supported"] is True
        assert len(result["locked_positions"]) == 1
        assert result["locked_positions"][0]["symbol"] == "KRW-BTC"
        assert result["locked_positions"][0]["lock_reason"] == "locked strategy"

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_dust_position_ignored(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [
                _make_position(
                    symbol="KRW-SHIB", name="시바이누", evaluation_amount=1_200
                )
            ],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["ignored_positions"]) == 1
        assert result["ignored_positions"][0]["symbol"] == "KRW-SHIB"

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_sell_candidate_from_stop_loss(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [
                _make_position(
                    symbol="KRW-WLD",
                    name="월드코인",
                    profit_rate=-8.5,
                    strategy_signal={"action": "sell", "reason": "stop_loss"},
                )
            ],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 1
        cand = result["sell_candidates"][0]
        assert cand["symbol"] == "KRW-WLD"
        assert cand["action"] == "reduce_full"
        assert cand["reduce_pct"] == 100

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_sell_candidate_partial_reduce_dca_oversold(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = (
            [_make_position(symbol="KRW-XRP", name="리플", profit_rate=-5.0)],
            [],
        )
        mock_journals.return_value = {
            "KRW-XRP": _make_journal(
                symbol="KRW-XRP", strategy="dca_oversold", hold_until=None
            ),
        }
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 1
        cand = result["sell_candidates"][0]
        assert cand["action"] == "reduce_partial"
        assert cand["reduce_pct"] == 30

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_healthy_position_not_surfaced(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        """Profitable position with no sell signal goes to none of the buckets."""
        mock_positions.return_value = (
            [_make_position(symbol="KRW-ETH", name="이더리움", profit_rate=12.0)],
            [],
        )
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")
        assert len(result["sell_candidates"]) == 0
        assert len(result["locked_positions"]) == 0
        assert len(result["ignored_positions"]) == 0


class TestBuyCandidates:
    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_buy_candidates_exclude_held(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        """Buy candidates that match held symbols should already be filtered."""
        mock_positions.return_value = (
            [_make_position(symbol="KRW-BTC")],
            [],
        )
        mock_journals.return_value = {}
        # _fetch_buy_candidates receives held_symbols, verify it was called with them
        mock_buy.return_value = [
            {
                "symbol": "KRW-BARD",
                "name": "롬바드",
                "price": 100,
                "trade_amount_24h": 5e9,
                "screen_reason": ["RSI oversold"],
            },
        ]

        result = await service.build_rotation_plan(market="crypto")
        mock_buy.assert_called_once()
        call_args = mock_buy.call_args
        assert "KRW-BTC" in call_args[1].get(
            "held_symbols", call_args[0][0] if call_args[0] else set()
        )
        assert len(result["buy_candidates"]) == 1
        assert result["buy_candidates"][0]["symbol"] == "KRW-BARD"


class TestResponseShape:
    @pytest.fixture()
    def service(self) -> PortfolioRotationService:
        return PortfolioRotationService()

    @pytest.mark.asyncio
    @patch(
        "app.services.portfolio_rotation_service._fetch_crypto_positions",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_active_journals",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service._fetch_buy_candidates",
        new_callable=AsyncMock,
    )
    async def test_response_has_all_required_keys(
        self,
        mock_buy: AsyncMock,
        mock_journals: AsyncMock,
        mock_positions: AsyncMock,
        service: PortfolioRotationService,
    ):
        mock_positions.return_value = ([], [])
        mock_journals.return_value = {}
        mock_buy.return_value = []

        result = await service.build_rotation_plan(market="crypto")

        required_keys = {
            "supported",
            "market",
            "account",
            "generated_at",
            "summary",
            "sell_candidates",
            "buy_candidates",
            "locked_positions",
            "ignored_positions",
            "warnings",
        }
        assert required_keys <= set(result.keys())

        summary_keys = {
            "total_positions",
            "actionable_positions",
            "locked_positions",
            "ignored_positions",
            "buy_candidates",
        }
        assert summary_keys <= set(result["summary"].keys())
        assert result["supported"] is True
        assert result["generated_at"] is not None


class TestAnalyzePortfolioRotation:
    @pytest.mark.asyncio
    @patch(
        "app.mcp_server.tooling.analysis_tool_handlers._run_batch_analysis",
        new_callable=AsyncMock,
    )
    @patch(
        "app.services.portfolio_rotation_service.PortfolioRotationService.build_rotation_plan",
        new_callable=AsyncMock,
    )
    async def test_analyze_portfolio_with_rotation_plan(
        self,
        mock_rotation: AsyncMock,
        mock_batch: AsyncMock,
    ):
        from app.mcp_server.tooling.analysis_tool_handlers import (
            analyze_portfolio_impl,
        )

        mock_batch.return_value = {
            "results": {"KRW-BTC": {"price": 50000000}},
            "summary": {"total_symbols": 1, "successful": 1, "failed": 0, "errors": []},
        }
        mock_rotation.return_value = {
            "supported": True,
            "market": "crypto",
            "sell_candidates": [],
            "buy_candidates": [],
        }

        result = await analyze_portfolio_impl(
            symbols=["KRW-BTC"],
            market="crypto",
            include_rotation_plan=True,
        )
        assert "rotation_plan" in result
        assert result["rotation_plan"]["supported"] is True
        mock_rotation.assert_called_once()

    @pytest.mark.asyncio
    @patch(
        "app.mcp_server.tooling.analysis_tool_handlers._run_batch_analysis",
        new_callable=AsyncMock,
    )
    async def test_analyze_portfolio_without_rotation_unchanged(
        self,
        mock_batch: AsyncMock,
    ):
        from app.mcp_server.tooling.analysis_tool_handlers import (
            analyze_portfolio_impl,
        )

        mock_batch.return_value = {
            "results": {},
            "summary": {"total_symbols": 0, "successful": 0, "failed": 0, "errors": []},
        }

        result = await analyze_portfolio_impl(symbols=[], market="crypto")
        assert "rotation_plan" not in result
