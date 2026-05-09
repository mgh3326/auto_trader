"""Error-path snapshot tests for app/routers/n8n.py.

These tests ensure that each covered endpoint returns HTTP 500 with
success=False when the underlying service raises an exception, and
that response JSON keys are preserved after the refactor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_client() -> TestClient:
    app = FastAPI()
    from app.routers.n8n import router

    app.include_router(router)
    return TestClient(app)


@pytest.mark.integration
class TestN8nErrorPaths:
    def test_pending_orders_service_error_returns_500(self) -> None:
        client = _make_client()
        with patch(
            "app.routers.n8n.fetch_pending_orders",
            new_callable=AsyncMock,
            side_effect=RuntimeError("service down"),
        ):
            resp = client.get("/api/n8n/pending-orders")
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert "orders" in body
        assert "summary" in body
        assert any("service down" in str(e) for e in body["errors"])

    def test_filled_orders_service_error_returns_500(self) -> None:
        client = _make_client()
        with patch(
            "app.routers.n8n.fetch_filled_orders",
            new_callable=AsyncMock,
            side_effect=RuntimeError("fill error"),
        ):
            resp = client.get("/api/n8n/filled-orders")
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert "orders" in body
        assert any("fill error" in str(e) for e in body["errors"])

    def test_trade_reviews_post_error_returns_500(self) -> None:
        client = _make_client()
        valid_review = {
            "order_id": "ORD001",
            "account": "upbit",
            "symbol": "BTC",
            "instrument_type": "crypto",
            "side": "buy",
            "price": 50000.0,
            "quantity": 1.0,
            "total_amount": 50000.0,
            "filled_at": "2024-01-01T00:00:00",
            "verdict": "good",
        }
        with patch(
            "app.routers.n8n.save_trade_reviews",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db error"),
        ):
            resp = client.post(
                "/api/n8n/trade-reviews", json={"reviews": [valid_review]}
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert any("db error" in str(e) for e in body["errors"])

    def test_pending_snapshots_post_error_returns_500(self) -> None:
        client = _make_client()
        valid_snapshot = {
            "symbol": "BTC",
            "instrument_type": "crypto",
            "side": "buy",
            "order_price": 50000.0,
            "quantity": 1.0,
            "account": "upbit",
        }
        with patch(
            "app.routers.n8n.save_pending_snapshots",
            new_callable=AsyncMock,
            side_effect=RuntimeError("snapshot fail"),
        ):
            resp = client.post(
                "/api/n8n/pending-snapshots", json={"snapshots": [valid_snapshot]}
            )
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert any("snapshot fail" in str(e) for e in body["errors"])

    def test_sell_signal_symbol_error_returns_500(self) -> None:
        client = _make_client()
        with (
            patch(
                "app.routers.n8n.get_sell_condition",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.routers.n8n.evaluate_sell_signal",
                new_callable=AsyncMock,
                side_effect=RuntimeError("signal error"),
            ),
        ):
            resp = client.get("/api/n8n/sell-signal/005930")
        assert resp.status_code == 500
        body = resp.json()
        assert body["success"] is False
        assert body["symbol"] == "005930"
        assert any("signal error" in str(e) for e in body["errors"])
