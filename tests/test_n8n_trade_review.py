from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestFilledOrdersService:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_orders(self):
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=[],
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
        ):
            result = await fetch_filled_orders(days=1, markets="crypto,kr,us")

        assert result["orders"] == []
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_filters_upbit_cancelled_orders(self):
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_closed = [
            {
                "uuid": "aaa-111",
                "side": "bid",
                "ord_type": "limit",
                "price": "98000000",
                "state": "done",
                "market": "KRW-BTC",
                "volume": "0.015",
                "executed_volume": "0.015",
                "paid_fee": "735",
                "created_at": "2026-03-17T14:30:00+09:00",
            },
            {
                "uuid": "bbb-222",
                "side": "bid",
                "ord_type": "limit",
                "price": "100000000",
                "state": "cancel",
                "market": "KRW-BTC",
                "volume": "0.01",
                "executed_volume": "0",
                "paid_fee": "0",
                "created_at": "2026-03-17T15:00:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
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
                side_effect=lambda orders: orders,
            ),
        ):
            result = await fetch_filled_orders(days=1, markets="crypto")

        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_id"] == "aaa-111"
        assert result["orders"][0]["side"] == "buy"

    @pytest.mark.asyncio
    async def test_min_amount_filter(self):
        from app.services.n8n_filled_orders_service import fetch_filled_orders

        mock_closed = [
            {
                "uuid": "aaa-111",
                "side": "bid",
                "ord_type": "limit",
                "price": "1000",
                "state": "done",
                "market": "KRW-XRP",
                "volume": "5",
                "executed_volume": "5",
                "paid_fee": "2.5",
                "created_at": "2026-03-17T14:30:00+09:00",
            },
        ]

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
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
        ):
            result = await fetch_filled_orders(
                days=1, markets="crypto", min_amount=10000
            )

        assert len(result["orders"]) == 0


@pytest.mark.unit
class TestTradeReviewService:
    def _make_review_item(self, **overrides):
        base = {
            "order_id": "test-order-001",
            "account": "upbit",
            "symbol": "BTC",
            "instrument_type": "crypto",
            "side": "buy",
            "price": 98000000,
            "quantity": 0.015,
            "total_amount": 1470000,
            "fee": 735,
            "currency": "KRW",
            "filled_at": "2026-03-17T14:30:00+09:00",
            "price_at_review": 101200000,
            "pnl_pct": 3.27,
            "verdict": "good",
            "comment": "RSI 31 oversold entry",
            "review_type": "daily",
            "indicators": {
                "rsi_14": 31.2,
                "rsi_7": 28.5,
                "ema_200": 95000000,
                "adx": 42.1,
                "volume_ratio": 1.8,
                "fear_greed": 25,
            },
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_rejects_null_order_id(self):
        from app.services.n8n_trade_review_service import save_trade_reviews

        mock_session = AsyncMock()
        item = self._make_review_item(order_id=None)

        result = await save_trade_reviews(mock_session, [item])

        assert result["saved_count"] == 0
        assert len(result["errors"]) == 1
        assert "order_id" in result["errors"][0]["error"].lower()

    @pytest.mark.asyncio
    async def test_saves_trade_with_snapshot_and_review(self):
        from app.services.n8n_trade_review_service import save_trade_reviews

        mock_session = AsyncMock()

        mock_result = MagicMock()
        mock_result.inserted_primary_key = (42,)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_session.scalars = AsyncMock(return_value=mock_scalars)

        item = self._make_review_item()
        result = await save_trade_reviews(mock_session, [item])

        assert result["saved_count"] == 1
        assert result["skipped_count"] == 0
        mock_session.commit.assert_awaited_once()


@pytest.mark.unit
class TestPendingReviewService:
    def test_fill_probability_high(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=0.5, days_pending=1) == "high"

    def test_fill_probability_medium(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=3.0, days_pending=1) == "medium"

    def test_fill_probability_low(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=6.0, days_pending=2) == "low"

    def test_fill_probability_stale(self):
        from app.services.n8n_pending_review_service import compute_fill_probability

        assert compute_fill_probability(gap_pct=4.0, days_pending=6) == "stale"


@pytest.mark.unit
class TestPendingSnapshotService:
    @pytest.mark.asyncio
    async def test_save_snapshots(self):
        from app.services.n8n_pending_snapshot_service import save_pending_snapshots

        mock_session = AsyncMock()

        items = [
            {
                "symbol": "BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "order_price": 96500000,
                "quantity": 0.01,
                "current_price": 101200000,
                "gap_pct": -4.6,
                "days_pending": 3,
                "account": "upbit",
                "order_id": "xyz-456",
            }
        ]

        result = await save_pending_snapshots(mock_session, items)

        assert result["saved_count"] == 1
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_updates_matching_snapshots(self):
        from app.services.n8n_pending_snapshot_service import resolve_pending_snapshots

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute = AsyncMock(return_value=mock_result)

        resolutions = [
            {"order_id": "xyz-456", "account": "upbit", "resolved_as": "filled"}
        ]

        result = await resolve_pending_snapshots(mock_session, resolutions)

        assert result["resolved_count"] == 1
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_rejects_invalid_status(self):
        from app.services.n8n_pending_snapshot_service import resolve_pending_snapshots

        mock_session = AsyncMock()

        resolutions = [
            {"order_id": "xyz-456", "account": "upbit", "resolved_as": "invalid"}
        ]

        result = await resolve_pending_snapshots(mock_session, resolutions)

        assert result["resolved_count"] == 0
        assert len(result["errors"]) == 1
