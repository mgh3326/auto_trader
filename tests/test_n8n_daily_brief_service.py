from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.timezone import KST


def _fake_pending_result(market: str, orders: list | None = None) -> dict:
    return {
        "success": True,
        "market": market,
        "orders": orders or [],
        "summary": {
            "total": len(orders or []),
            "buy_count": 0,
            "sell_count": 0,
            "total_buy_krw": 0,
            "total_sell_krw": 0,
            "total_buy_fmt": None,
            "total_sell_fmt": None,
            "title": None,
        },
        "errors": [],
    }


def _fake_market_context() -> dict:
    from app.schemas.n8n import (
        N8nFearGreedData,
        N8nMarketContextSummary,
        N8nMarketOverview,
    )

    return {
        "market_overview": N8nMarketOverview(
            fear_greed=N8nFearGreedData(
                value=23, label="Fear", previous=20, trend="improving"
            ),
            btc_dominance=56.64,
            total_market_cap_change_24h=3.86,
            economic_events_today=[],
        ),
        "symbols": [],
        "summary": N8nMarketContextSummary(
            total_symbols=0,
            bullish_count=0,
            bearish_count=0,
            neutral_count=0,
            avg_rsi=None,
            market_sentiment="neutral",
        ),
        "errors": [],
    }


def _fake_portfolio_overview() -> dict:
    return {
        "success": True,
        "positions": [
            {
                "market_type": "CRYPTO",
                "symbol": "KRW-BTC",
                "name": "비트코인",
                "quantity": 0.1,
                "avg_price": 100_000_000,
                "current_price": 105_000_000,
                "evaluation": 10_500_000,
                "profit_loss": 500_000,
                "profit_rate": 0.05,
                "components": [],
            },
        ],
        "warnings": [],
    }


@pytest.mark.unit
class TestFetchDailyBrief:
    @pytest.mark.asyncio
    async def test_returns_success_structure(self):
        as_of = datetime(2026, 3, 17, 8, 30, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=_fake_pending_result("crypto"),
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=_fake_portfolio_overview(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            result = await fetch_daily_brief(
                markets=["crypto"],
                min_amount=50_000,
                as_of=as_of,
            )

        assert result["success"] is True
        assert result["date_fmt"] == "03/17 (화)"
        assert "market_overview" in result
        assert "pending_orders" in result
        assert "portfolio_summary" in result
        assert "yesterday_fills" in result
        assert "brief_text" in result
        assert isinstance(result["brief_text"], str)
        assert len(result["brief_text"]) > 0

    @pytest.mark.asyncio
    async def test_handles_partial_failures(self):
        as_of = datetime(2026, 3, 17, 8, 30, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                side_effect=Exception("pending failed"),
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=_fake_portfolio_overview(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            result = await fetch_daily_brief(
                markets=["crypto"],
                min_amount=50_000,
                as_of=as_of,
            )

        # Should still succeed with partial data
        assert result["success"] is True
        assert len(result["errors"]) > 0
