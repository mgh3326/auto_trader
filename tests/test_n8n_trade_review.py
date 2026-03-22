from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.unit
class TestFilledOrdersService:
    @pytest.mark.asyncio
    async def test_fetch_upbit_filled_filters_orders_older_than_days(self):
        from datetime import datetime

        from app.core.timezone import KST
        from app.services.n8n_filled_orders_service import _fetch_upbit_filled

        mock_closed = [
            {
                "uuid": "recent-fill",
                "side": "bid",
                "price": "1000",
                "state": "done",
                "market": "KRW-XRP",
                "executed_volume": "5",
                "paid_fee": "2.5",
                "created_at": "2026-03-20T10:00:00+09:00",
            },
            {
                "uuid": "stale-fill",
                "side": "ask",
                "price": "1200",
                "state": "done",
                "market": "KRW-XRP",
                "executed_volume": "3",
                "paid_fee": "1.0",
                "created_at": "2026-03-18T09:00:00+09:00",
            },
        ]

        fixed_now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
            ),
            patch(
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
            ),
        ):
            orders, errors = await _fetch_upbit_filled(days=1)

        assert errors == []
        assert [order["order_id"] for order in orders] == ["recent-fill"]

    @pytest.mark.asyncio
    async def test_fetch_upbit_filled_skips_unparseable_filled_at(self, caplog):
        from datetime import datetime

        from app.core.timezone import KST
        from app.services.n8n_filled_orders_service import _fetch_upbit_filled

        mock_closed = [
            {
                "uuid": "bad-fill",
                "side": "bid",
                "price": "1000",
                "state": "done",
                "market": "KRW-XRP",
                "executed_volume": "1",
                "paid_fee": "0.5",
                "created_at": "not-a-datetime",
            }
        ]

        fixed_now = datetime(2026, 3, 21, 0, 0, 0, tzinfo=KST)

        with (
            patch(
                "app.services.n8n_filled_orders_service.upbit_service.fetch_closed_orders",
                new_callable=AsyncMock,
                return_value=mock_closed,
            ),
            patch(
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
            ),
            caplog.at_level("WARNING"),
        ):
            orders, errors = await _fetch_upbit_filled(days=1)

        assert orders == []
        assert errors == []
        assert "Upbit filled order skipped due to invalid filled_at" in caplog.text

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
        from datetime import datetime

        from app.core.timezone import KST
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
        fixed_now = datetime(2026, 3, 18, 0, 0, 0, tzinfo=KST)

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
            patch(
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
            ),
        ):
            result = await fetch_filled_orders(days=1, markets="crypto")

        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_id"] == "aaa-111"
        assert result["orders"][0]["side"] == "buy"

    @pytest.mark.asyncio
    async def test_min_amount_filter(self):
        from datetime import datetime

        from app.core.timezone import KST
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
        fixed_now = datetime(2026, 3, 18, 0, 0, 0, tzinfo=KST)

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
                "app.services.n8n_filled_orders_service.now_kst",
                return_value=fixed_now,
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

    @pytest.mark.asyncio
    async def test_fetch_pending_review_exposes_button_action_context(
        self, monkeypatch
    ):
        from app.services.n8n_pending_review_service import fetch_pending_review

        monkeypatch.setattr(
            "app.services.n8n_pending_review_service.fetch_pending_orders",
            AsyncMock(
                return_value={
                    "orders": [
                        {
                            "order_id": "US-1",
                            "symbol": "AAPL",
                            "market": "us",
                            "raw_symbol": "AAPL",
                            "side": "sell",
                            "order_price": 210.0,
                            "current_price": 198.0,
                            "gap_pct": 6.1,
                            "gap_pct_fmt": "+6.1%",
                            "amount_krw": 300000,
                            "quantity": 2,
                            "remaining_qty": 2,
                            "created_at": "2026-03-22T00:30:00+09:00",
                            "age_days": 2,
                            "currency": "USD",
                        }
                    ],
                    "errors": [],
                }
            ),
        )

        result = await fetch_pending_review(market="us")
        order = result["orders"][0]

        assert order["action_context"]["cancel"] == {
            "order_id": "US-1",
            "market": "us",
            "symbol": "AAPL",
        }
        assert order["action_context"]["modify"]["order_id"] == "US-1"
        assert order["action_context"]["modify"]["market"] == "us"
        assert order["action_context"]["modify"]["symbol"] == "AAPL"


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


@pytest.mark.unit
class TestTradeReviewListSchema:
    def test_list_item_schema_accepts_valid_data(self):
        from app.schemas.n8n import N8nTradeReviewListItem

        item = N8nTradeReviewListItem(
            order_id="test-001",
            symbol="BTC",
            market="crypto",
            side="buy",
            price=98000000,
            quantity=0.015,
            total_amount=1470000,
            fee=735,
            currency="KRW",
            filled_at="2026-03-17T14:30:00+09:00",
            verdict="good",
            pnl_pct=3.27,
            comment="RSI oversold entry",
            review_type="daily",
            review_date="2026-03-18T09:00:00+09:00",
            indicators=None,
        )
        assert item.order_id == "test-001"
        assert item.market == "crypto"

    def test_list_response_schema(self):
        from app.schemas.n8n import N8nTradeReviewListResponse

        resp = N8nTradeReviewListResponse(
            success=True,
            period="2026-03-11 ~ 2026-03-18",
            total_count=0,
            reviews=[],
            errors=[],
        )
        assert resp.success is True
        assert resp.total_count == 0


@pytest.mark.unit
class TestGetTradeReviews:
    @pytest.mark.asyncio
    async def test_returns_empty_for_no_data(self):
        from app.services.n8n_trade_review_service import get_trade_reviews

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await get_trade_reviews(mock_session, period="7d")

        assert result["total_count"] == 0
        assert result["reviews"] == []
        assert "period" in result

    @pytest.mark.unit
    def test_parses_period_format(self):
        from app.services.n8n_trade_review_service import parse_period

        delta = parse_period("7d")
        assert delta.days == 7

        delta = parse_period("30d")
        assert delta.days == 30

    @pytest.mark.unit
    def test_invalid_period_defaults_to_7d(self):
        from app.services.n8n_trade_review_service import parse_period

        delta = parse_period("invalid")
        assert delta.days == 7

        delta = parse_period("")
        assert delta.days == 7


@pytest.mark.unit
class TestGetTradeReviewsEndpoint:
    @pytest.fixture
    def client(self):
        """Create test client with n8n router."""
        from app.routers.n8n import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_trade_reviews_default_params(self, client):
        mock_result = {
            "period": "2026-03-11 ~ 2026-03-18",
            "total_count": 0,
            "reviews": [],
            "errors": [],
        }
        with patch(
            "app.routers.n8n.get_trade_reviews",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.get("/api/n8n/trade-reviews")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total_count"] == 0

    def test_get_trade_reviews_with_filters(self, client):
        mock_result = {
            "period": "2026-03-11 ~ 2026-03-18",
            "total_count": 1,
            "reviews": [
                {
                    "order_id": "test-001",
                    "symbol": "BTC",
                    "market": "crypto",
                    "side": "buy",
                    "price": 98000000,
                    "quantity": 0.015,
                    "total_amount": 1470000,
                    "fee": 735,
                    "currency": "KRW",
                    "filled_at": "2026-03-17T14:30:00+09:00",
                    "verdict": "good",
                    "pnl_pct": 3.27,
                    "comment": "RSI entry",
                    "review_type": "daily",
                    "review_date": "2026-03-18T09:00:00+09:00",
                    "indicators": None,
                }
            ],
            "errors": [],
        }
        with patch(
            "app.routers.n8n.get_trade_reviews",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.get(
                "/api/n8n/trade-reviews",
                params={
                    "period": "30d",
                    "market": "crypto",
                    "symbol": "BTC",
                    "limit": 50,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 1
        assert data["reviews"][0]["symbol"] == "BTC"

    def test_get_trade_reviews_500_on_error(self, client):
        with patch(
            "app.routers.n8n.get_trade_reviews",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            resp = client.get("/api/n8n/trade-reviews")

        assert resp.status_code == 500
        data = resp.json()
        assert data["success"] is False
