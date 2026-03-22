from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv_df(n: int = 250, base_close: float = 100_000_000) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame with enough rows for all indicators."""
    # Use sine wave + trend to create both gains and losses for RSI calculation

    t = np.linspace(0, 4 * np.pi, n)
    noise = np.random.RandomState(42).normal(0, 0.02, n)
    close_values = base_close * (
        1 + 0.1 * np.sin(t) + 0.05 * np.linspace(0, 1, n) + noise
    )
    close = pd.Series(close_values, dtype=float)

    return pd.DataFrame(
        {
            "open": close - 5_000,
            "high": close + 10_000,
            "low": close - 10_000,
            "close": close,
            "volume": [1_000_000.0 + i * 100 for i in range(n)],
        }
    )


@pytest.mark.unit
class TestComputeReviewIndicators:
    @pytest.mark.asyncio
    async def test_returns_all_indicator_fields_for_crypto(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250)

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is not None
        # All N8nTradeReviewIndicators fields except fear_greed (fetched separately)
        for key in (
            "rsi_14",
            "rsi_7",
            "ema_20",
            "ema_200",
            "macd",
            "macd_signal",
            "adx",
            "stoch_rsi_k",
            "volume_ratio",
        ):
            assert key in result, f"Missing key: {key}"

        assert isinstance(result["rsi_14"], float)
        assert isinstance(result["ema_20"], float)
        assert isinstance(result["ema_200"], float)
        assert isinstance(result["macd"], float)
        assert isinstance(result["volume_ratio"], float)

    @pytest.mark.asyncio
    async def test_returns_none_on_insufficient_data(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(10)  # Too few rows

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_fetch_error(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API down"),
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is None

    @pytest.mark.asyncio
    async def test_works_for_equity_kr(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250, base_close=80_000)

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("005930", "equity_kr")

        assert result is not None
        assert isinstance(result["rsi_14"], float)
        assert isinstance(result["ema_20"], float)

    @pytest.mark.asyncio
    async def test_volume_ratio_with_zero_avg_volume(self):
        from app.services.n8n_filled_orders_indicators import (
            _compute_review_indicators,
        )

        df = _make_ohlcv_df(250)
        df["volume"] = 0.0  # All zeros

        with patch(
            "app.services.n8n_filled_orders_indicators._fetch_ohlcv_for_indicators",
            new_callable=AsyncMock,
            return_value=df,
        ):
            result = await _compute_review_indicators("BTC", "crypto")

        assert result is not None
        assert result["volume_ratio"] is None


@pytest.mark.unit
class TestEnrichWithIndicators:
    @pytest.mark.asyncio
    async def test_enriches_crypto_orders_with_indicators_and_fear_greed(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
            {"symbol": "ETH", "raw_symbol": "KRW-ETH", "instrument_type": "crypto"},
        ]

        mock_indicators = {
            "rsi_14": 42.0,
            "rsi_7": 38.0,
            "ema_20": 106_000_000.0,
            "ema_200": 98_000_000.0,
            "macd": -1_200_000.0,
            "macd_signal": -800_000.0,
            "adx": 28.0,
            "stoch_rsi_k": 22.0,
            "volume_ratio": 1.3,
        }

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                new_callable=AsyncMock,
                return_value=mock_indicators,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value={"value": 25, "label": "Extreme Fear"},
            ),
        ):
            result = await _enrich_with_indicators(orders)

        assert result[0]["indicators"]["rsi_14"] == 42.0
        assert result[0]["indicators"]["fear_greed"] == 25
        assert result[1]["indicators"]["fear_greed"] == 25

    @pytest.mark.asyncio
    async def test_same_symbol_fetched_only_once(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
        ]

        mock_compute = AsyncMock(return_value={"rsi_14": 50.0})

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                mock_compute,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await _enrich_with_indicators(orders)

        # Should be called once for BTC, not twice
        assert mock_compute.call_count == 1

    @pytest.mark.asyncio
    async def test_equity_orders_skip_fear_greed(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {
                "symbol": "005930",
                "raw_symbol": "005930",
                "instrument_type": "equity_kr",
            },
        ]

        mock_indicators = {"rsi_14": 55.0, "ema_20": 80_000.0}

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                new_callable=AsyncMock,
                return_value=mock_indicators,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
            ) as _mock_fg,
        ):
            result = await _enrich_with_indicators(orders)

        # Fear & Greed should not be fetched for equity
        assert result[0]["indicators"].get("fear_greed") is None

    @pytest.mark.asyncio
    async def test_indicator_failure_yields_none(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _enrich_with_indicators(orders)

        assert result[0]["indicators"] is None

    @pytest.mark.asyncio
    async def test_mixed_markets(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
            {
                "symbol": "005930",
                "raw_symbol": "005930",
                "instrument_type": "equity_kr",
            },
            {"symbol": "NVDA", "raw_symbol": "NVDA", "instrument_type": "equity_us"},
        ]

        mock_indicators = {"rsi_14": 50.0, "ema_20": 100.0}

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                new_callable=AsyncMock,
                return_value=mock_indicators,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value={"value": 40},
            ),
        ):
            result = await _enrich_with_indicators(orders)

        # All 3 orders should have indicators
        assert all(o.get("indicators") is not None for o in result)
        # Only crypto gets fear_greed
        assert result[0]["indicators"]["fear_greed"] == 40
        assert result[1]["indicators"].get("fear_greed") is None
        assert result[2]["indicators"].get("fear_greed") is None

    @pytest.mark.asyncio
    async def test_crypto_orders_use_raw_symbol_for_indicator_lookup(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )

        orders = [
            {"symbol": "APT", "raw_symbol": "KRW-APT", "instrument_type": "crypto"},
        ]

        mock_compute = AsyncMock(return_value={"rsi_14": 42.0})

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                mock_compute,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await _enrich_with_indicators(orders)

        mock_compute.assert_awaited_once_with("KRW-APT", "crypto")

    @pytest.mark.asyncio
    async def test_crypto_orders_keep_quote_market_separate_in_indicator_cache(self):
        from app.services.n8n_filled_orders_indicators import (
            _enrich_with_indicators,
        )
        from unittest.mock import call

        orders = [
            {"symbol": "BTC", "raw_symbol": "KRW-BTC", "instrument_type": "crypto"},
            {"symbol": "BTC", "raw_symbol": "USDT-BTC", "instrument_type": "crypto"},
        ]

        mock_compute = AsyncMock(
            side_effect=[
                {"rsi_14": 10.0},
                {"rsi_14": 20.0},
            ]
        )

        with (
            patch(
                "app.services.n8n_filled_orders_indicators._compute_review_indicators",
                mock_compute,
            ),
            patch(
                "app.services.n8n_filled_orders_indicators.fetch_fear_greed",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _enrich_with_indicators(orders)

        assert mock_compute.await_args_list == [
            call("KRW-BTC", "crypto"),
            call("USDT-BTC", "crypto"),
        ]
        assert result[0]["indicators"]["rsi_14"] == 10.0
        assert result[1]["indicators"]["rsi_14"] == 20.0


@pytest.mark.unit
class TestFilledOrderSchema:
    def test_indicators_field_is_optional_and_defaults_to_none(self):
        from app.schemas.n8n.filled_orders import N8nFilledOrderItem

        item = N8nFilledOrderItem(
            symbol="BTC",
            raw_symbol="KRW-BTC",
            instrument_type="crypto",
            side="buy",
            price=100_000_000,
            quantity=0.01,
            total_amount=1_000_000,
            fee=500,
            currency="KRW",
            account="upbit",
            order_id="test-123",
            filled_at="2026-03-22T10:00:00+09:00",
        )
        assert item.indicators is None

    def test_indicators_field_accepts_valid_indicators(self):
        from app.schemas.n8n.filled_orders import N8nFilledOrderItem
        from app.schemas.n8n.trade_review import N8nTradeReviewIndicators

        indicators = N8nTradeReviewIndicators(rsi_14=42.3, ema_20=106_000_000)

        item = N8nFilledOrderItem(
            symbol="BTC",
            raw_symbol="KRW-BTC",
            instrument_type="crypto",
            side="buy",
            price=100_000_000,
            quantity=0.01,
            total_amount=1_000_000,
            fee=500,
            currency="KRW",
            account="upbit",
            order_id="test-123",
            filled_at="2026-03-22T10:00:00+09:00",
            indicators=indicators,
        )
        assert item.indicators is not None
        assert item.indicators.rsi_14 == 42.3


@pytest.mark.unit
class TestFetchFilledOrdersWithIndicators:
    @pytest.mark.asyncio
    async def test_include_indicators_false_skips_enrichment(self):
        """Default behavior — no indicators attached."""
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_orders = [
            {
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "price": 100_000_000,
                "total_amount": 1_000_000,
                "filled_at": "2026-03-22T10:00:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service._fetch_upbit_filled",
                new_callable=AsyncMock,
                return_value=(mock_orders, []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_domestic_filled",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_overseas_filled",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._enrich_with_current_prices",
                new_callable=AsyncMock,
                side_effect=lambda o: o,
            ),
        ):
            result = await fetch_filled_orders(days=1, include_indicators=False)

        assert "indicators" not in result["orders"][0]

    @pytest.mark.asyncio
    async def test_include_indicators_true_calls_enrichment(self):
        """Verify service passes orders with both stripped symbol and raw_symbol."""
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_orders = [
            {
                "symbol": "APT",
                "raw_symbol": "KRW-APT",
                "instrument_type": "crypto",
                "side": "buy",
                "price": 100_000,
                "total_amount": 100_000,
                "filled_at": "2026-03-22T10:00:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service._fetch_upbit_filled",
                new_callable=AsyncMock,
                return_value=(mock_orders, []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_domestic_filled",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._fetch_kis_overseas_filled",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch(
                "app.services.n8n_filled_orders_service._enrich_with_current_prices",
                new_callable=AsyncMock,
                side_effect=lambda o: o,
            ),
            patch(
                "app.services.n8n_filled_orders_service._enrich_with_indicators",
                new_callable=AsyncMock,
                side_effect=lambda o: o,
            ) as mock_enrich,
        ):
            result = await fetch_filled_orders(days=1, include_indicators=True)

        mock_enrich.assert_called_once()
        # Verify order shape includes both stripped symbol and raw_symbol for lookup
        assert result["orders"][0]["raw_symbol"] == "KRW-APT"
        assert result["orders"][0]["symbol"] == "APT"


@pytest.mark.unit
class TestFilledOrdersRouter:
    def _get_client(self):
        """Create TestClient with just n8n router (bypasses auth middleware)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_include_indicators_query_param_defaults_to_false(self):
        """GET /api/n8n/filled-orders without include_indicators returns no indicators."""
        mock_result = {
            "orders": [
                {
                    "symbol": "BTC",
                    "raw_symbol": "KRW-BTC",
                    "instrument_type": "crypto",
                    "side": "buy",
                    "price": 100_000_000,
                    "quantity": 0.01,
                    "total_amount": 1_000_000,
                    "fee": 500,
                    "currency": "KRW",
                    "account": "upbit",
                    "order_id": "test-123",
                    "filled_at": "2026-03-22T10:00:00+09:00",
                    "current_price": 101_000_000,
                    "pnl_pct": 1.0,
                    "pnl_pct_fmt": "+1.00%",
                },
            ],
            "errors": [],
        }

        with patch(
            "app.routers.n8n.fetch_filled_orders",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fetch:
            client = self._get_client()
            resp = client.get("/api/n8n/filled-orders?days=1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # Verify include_indicators=False was passed
        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs.get("include_indicators") is False

    def test_include_indicators_true_param_passed_to_service(self):
        mock_result = {"orders": [], "errors": []}

        with patch(
            "app.routers.n8n.fetch_filled_orders",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_fetch:
            client = self._get_client()
            resp = client.get("/api/n8n/filled-orders?include_indicators=true")

        assert resp.status_code == 200
        call_kwargs = mock_fetch.call_args.kwargs
        assert call_kwargs.get("include_indicators") is True
