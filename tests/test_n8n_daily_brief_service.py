from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.core.timezone import KST
from app.services.n8n_daily_brief_service import _build_portfolio_summary


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
    from app.schemas.n8n.common import (
        N8nFearGreedData,
        N8nMarketOverview,
    )
    from app.schemas.n8n.market_context import N8nMarketContextSummary

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

    @pytest.mark.asyncio
    async def test_daily_brief_preserves_indicators_and_prefixed_crypto_symbols(self):
        pending = _fake_pending_result(
            "all",
            orders=[
                {
                    "market": "crypto",
                    "symbol": "BTC",
                    "raw_symbol": "USDT-BTC",
                    "indicators": {"rsi_14": 55.1},
                },
                {"market": "kr", "symbol": "005930", "raw_symbol": "005930"},
            ],
        )
        portfolio = {
            "success": True,
            "positions": [
                {"market_type": "CRYPTO", "symbol": "KRW-ETH", "name": "ETH"},
            ],
            "warnings": [],
        }

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=pending,
            ) as mock_pending,
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=portfolio,
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ) as mock_context,
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ),
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            await fetch_daily_brief(markets=["crypto", "kr"])

        assert mock_pending.await_count == 1
        assert mock_pending.await_args.kwargs["include_indicators"] is True
        assert sorted(mock_context.await_args.kwargs["symbols"]) == [
            "KRW-ETH",
            "USDT-BTC",
        ]

    @pytest.mark.asyncio
    async def test_daily_brief_passes_shared_symbols_to_yesterday_fills(self):
        pending = _fake_pending_result(
            "all",
            orders=[
                {"market": "crypto", "symbol": "BTC", "raw_symbol": "KRW-BTC"},
                {"market": "us", "symbol": "NVDA", "raw_symbol": "NVDA"},
            ],
        )
        portfolio = {
            "success": True,
            "positions": [
                {"market_type": "KR", "symbol": "005930", "name": "Samsung"},
            ],
            "warnings": [],
        }

        with (
            patch(
                "app.services.n8n_daily_brief_service.fetch_pending_orders",
                new_callable=AsyncMock,
                return_value=pending,
            ),
            patch(
                "app.services.n8n_daily_brief_service._get_portfolio_overview",
                new_callable=AsyncMock,
                return_value=portfolio,
            ),
            patch(
                "app.services.n8n_daily_brief_service.fetch_market_context",
                new_callable=AsyncMock,
                return_value=_fake_market_context(),
            ),
            patch(
                "app.services.n8n_daily_brief_service._fetch_yesterday_fills",
                new_callable=AsyncMock,
                return_value={"total": 0, "fills": []},
            ) as mock_fills,
        ):
            from app.services.n8n_daily_brief_service import fetch_daily_brief

            await fetch_daily_brief(markets=["crypto", "kr", "us"])

        assert mock_fills.await_args.kwargs["symbols_by_market"] == {
            "crypto": {"KRW-BTC"},
            "kr": {"005930"},
            "us": {"NVDA"},
        }


@pytest.mark.unit
class TestBuildPortfolioSummary:
    """Test _build_portfolio_summary PnL calculation."""

    def _make_overview(self, positions: list[dict]) -> dict:
        return {"positions": positions}

    def test_us_pnl_uses_profit_rate_not_avg_price(self):
        """Regression: Issue #327 - US PnL should not mix KRW/USD avg_price.

        When manual holdings have avg_price in KRW and KIS has USD,
        the aggregated avg_price is nonsensical. The fix uses per-position
        profit_rate and evaluation to derive PnL.
        """
        # Simulate an aggregated US position where avg_price is a
        # mixed KRW/USD weighted average (the broken aggregation output).
        # KIS: 5 shares, avg $150, current $160 → profit_rate ≈ 0.0667
        # Manual: 5 shares, avg ₩200,000 (KRW!), but after aggregation:
        #   avg_price = (5*150 + 5*200000) / 10 = 100,075 (nonsense)
        #   evaluation = 10 * 160 = 1,600 (USD)
        #   profit_rate = (1600 - 1000750) / 1000750 ≈ -0.998 (WRONG from aggregation)
        #
        # But the individual KIS position profit_rate (0.0667) is correct.
        # After our fix, _build_portfolio_summary should use evaluation and
        # profit_rate from positions, not recalculate from avg_price.

        overview = self._make_overview(
            [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA",
                    "quantity": 10,
                    "avg_price": 100_075,  # Broken mixed avg from aggregation
                    "current_price": 160.0,
                    "evaluation": 1_600.0,  # 10 * $160, in USD
                    "profit_loss": 100.0,  # Correct: $1600 - $1500 = $100
                    "profit_rate": 0.0667,  # Correct: from KIS API or proper calc
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None

        # PnL should be approximately +6.67%, NOT -99.8%
        assert us["pnl_pct"] is not None
        assert us["pnl_pct"] > 0, f"Expected positive PnL, got {us['pnl_pct']}"
        assert us["pnl_pct"] == pytest.approx(6.67, abs=1.0)

    def test_kr_pnl_still_works(self):
        """KR positions should still calculate PnL correctly."""
        overview = self._make_overview(
            [
                {
                    "market_type": "KR",
                    "symbol": "005930",
                    "name": "삼성전자",
                    "quantity": 100,
                    "avg_price": 70_000.0,
                    "current_price": 75_000.0,
                    "evaluation": 7_500_000.0,
                    "profit_loss": 500_000.0,
                    "profit_rate": 0.0714,
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        kr = result.get("kr")
        assert kr is not None
        assert kr["pnl_pct"] is not None
        assert kr["pnl_pct"] > 0
        assert kr["pnl_pct"] == pytest.approx(7.14, abs=1.0)

    def test_crypto_pnl_still_works(self):
        """Crypto positions should still calculate PnL correctly."""
        overview = self._make_overview(
            [
                {
                    "market_type": "CRYPTO",
                    "symbol": "KRW-BTC",
                    "name": "BTC",
                    "quantity": 0.5,
                    "avg_price": 100_000_000.0,
                    "current_price": 110_000_000.0,
                    "evaluation": 55_000_000.0,
                    "profit_loss": 5_000_000.0,
                    "profit_rate": 0.10,
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        crypto = result.get("crypto")
        assert crypto is not None
        assert crypto["pnl_pct"] is not None
        assert crypto["pnl_pct"] == pytest.approx(10.0, abs=1.0)

    def test_position_without_profit_rate_falls_back(self):
        """Positions missing profit_rate should fall back gracefully."""
        overview = self._make_overview(
            [
                {
                    "market_type": "US",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "quantity": 5,
                    "avg_price": 180.0,
                    "current_price": 190.0,
                    "evaluation": 950.0,
                    "profit_loss": None,
                    "profit_rate": None,
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # When profit_rate is None, fall back to avg_price * qty calculation
        # $950 - $900 = $50, $50/$900 = 5.56%
        assert us["pnl_pct"] is not None
        assert us["pnl_pct"] == pytest.approx(5.56, abs=1.0)

    def test_zero_evaluation_returns_none_pnl(self):
        """Zero evaluation should not cause division errors."""
        overview = self._make_overview(
            [
                {
                    "market_type": "US",
                    "symbol": "XYZ",
                    "name": "Dead Stock",
                    "quantity": 10,
                    "avg_price": 50.0,
                    "current_price": 0.0,
                    "evaluation": 0.0,
                    "profit_loss": -500.0,
                    "profit_rate": -1.0,
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # Should handle gracefully, not crash
        assert us["pnl_pct"] == pytest.approx(-100.0) or us["pnl_pct"] is None

    def test_multi_position_weighted_pnl(self):
        """Multiple US positions should produce weighted PnL."""
        overview = self._make_overview(
            [
                {
                    "market_type": "US",
                    "symbol": "NVDA",
                    "name": "NVIDIA",
                    "quantity": 10,
                    "avg_price": 150.0,
                    "current_price": 160.0,
                    "evaluation": 1_600.0,
                    "profit_loss": 100.0,
                    "profit_rate": 0.0667,  # +6.67%
                },
                {
                    "market_type": "US",
                    "symbol": "AAPL",
                    "name": "Apple",
                    "quantity": 20,
                    "avg_price": 180.0,
                    "current_price": 170.0,
                    "evaluation": 3_400.0,
                    "profit_loss": -200.0,
                    "profit_rate": -0.0556,  # -5.56%
                },
            ]
        )

        result = _build_portfolio_summary(overview)
        us = result.get("us")
        assert us is not None
        # Total eval: $5,000; Total cost: $1,500 + $3,600 = $5,100; PnL: -1.96%
        # Using profit_rate method: cost_NVDA = 1600/1.0667 ≈ 1500, cost_AAPL = 3400/0.9444 ≈ 3600
        # Total cost ≈ 5100, PnL = (5000 - 5100)/5100 * 100 ≈ -1.96%
        assert us["pnl_pct"] is not None
        assert abs(us["pnl_pct"] - (-1.96)) < 0.5
