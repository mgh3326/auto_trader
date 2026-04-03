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
                "components": [
                    {
                        "broker": "kis",
                        "account_name": "ISA",
                        "source": "live",
                        "quantity": 2.0,
                        "avg_price": 118.0,
                        "current_price": 132.0,
                        "evaluation": 264.0,
                        "profit_loss": 28.0,
                        "profit_rate": 0.1186,
                    }
                ],
                "journal": {
                    "strategy": "trend",
                    "thesis": "AI capex leader",
                    "status": "active",
                    "notes": "keep position",
                    "hold_until": "2026-04-30T00:00:00+00:00",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-02T00:00:00+00:00",
                    "target_price": 145.0,
                    "stop_loss": 118.0,
                    "target_distance_pct": 9.85,
                    "stop_distance_pct": -10.61,
                    "indicators_snapshot": {"rsi_14": 28.4},
                },
                "weights": {
                    "portfolio_weight_pct": 9.8,
                    "market_weight_pct": 24.5,
                },
                "action_summary": {
                    "status": "관망",
                    "status_tone": "neutral",
                    "tags": ["비중 보통", "목표가까지 여유", "RSI 중립"],
                    "reason": "전체 비중 9.8% · 시장 내 24.5% · RSI 41.2",
                    "short_reason": "전체 비중 9.8% · 시장 내 24.5% · RSI 41.2",
                },
            }
        )
        self.get_indicators_payload = AsyncMock(
            return_value={
                "price": 132.0,
                "summary_cards": [
                    {"label": "RSI(14)", "value": "28.4", "tone": "oversold"},
                    {"label": "MACD", "value": "Bullish", "tone": "bullish"},
                ],
            }
        )
        self.get_news_payload = AsyncMock(
            return_value={
                "count": 1,
                "news": [
                    {
                        "title": "NVIDIA <script>alert(1)</script>",
                        "source": "Reuters",
                        "published_at": "2026-04-02T09:00:00+09:00",
                        "url": "https://example.com/nvda",
                        "summary": "Demand remains strong",
                        "excerpt": "Demand remains strong",
                        "sentiment": "positive",
                        "relevance": "high",
                    }
                ],
            }
        )
        self.get_orders_payload = AsyncMock(
            return_value={
                "summary": {
                    "last_fill": {
                        "order_id": "fill-1",
                        "side": "buy",
                        "status": "filled",
                        "ordered_at": "2026-04-01T09:19:00+09:00",
                        "price": 455.5,
                        "quantity": 1.0,
                        "amount": 455.5,
                        "currency": "USD",
                    },
                    "pending_count": 1,
                    "fill_count": 1,
                },
                "recent_fills": [
                    {
                        "order_id": "fill-1",
                        "side": "buy",
                        "status": "filled",
                        "ordered_at": "2026-04-01T09:19:00+09:00",
                        "price": 455.5,
                        "quantity": 1.0,
                        "amount": 455.5,
                        "currency": "USD",
                    }
                ],
                "pending_orders": [
                    {
                        "order_id": "pending-1",
                        "side": "sell",
                        "status": "pending",
                        "ordered_at": "2026-04-02T10:00:00+09:00",
                        "price": 480.0,
                        "quantity": 2.0,
                        "remaining_quantity": 1.5,
                        "amount": 960.0,
                        "currency": "USD",
                    }
                ],
                "errors": [],
            }
        )
        self.get_opinions_payload = AsyncMock(
            return_value={
                "supported": True,
                "message": None,
                "consensus": "Buy",
                "avg_target_price": 155.0,
                "upside_pct": 12.3,
                "buy_count": 8,
                "hold_count": 3,
                "sell_count": 1,
                "summary_cards": [
                    {"label": "Consensus", "value": "Buy", "tone": "positive"},
                    {"label": "Avg Target", "value": "155.0", "tone": "neutral"},
                ],
                "distribution": {"buy": 8, "hold": 3, "sell": 1},
                "top_opinions": [
                    {
                        "firm": "Alpha <Capital>",
                        "rating": "Buy",
                        "target_price": 155.0,
                        "date": "2026-04-01",
                    }
                ],
                "overflow_count": 2,
                "opinions": [{"firm": "Alpha <Capital>", "rating": "Buy"}],
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
    assert "보유 기한" in body
    assert "상태" in body
    assert "메모" in body
    assert "$36.00 (10.00%)" in body
    assert "11.86%" in body
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


@pytest.mark.unit
def test_position_detail_indicators_api_returns_payload() -> None:
    client, detail = _create_client()
    detail.get_indicators_payload = AsyncMock(
        return_value={"price": 132.0, "indicators": {"rsi": {"14": 28.4}}}
    )

    response = client.get("/portfolio/api/positions/us/NVDA/indicators")

    assert response.status_code == 200
    assert response.json()["indicators"]["rsi"]["14"] == 28.4


@pytest.mark.unit
def test_position_detail_opinions_api_returns_crypto_fallback() -> None:
    client, detail = _create_client()
    detail.get_opinions_payload = AsyncMock(
        return_value={
            "supported": False,
            "message": "애널리스트 의견이 제공되지 않는 시장입니다.",
            "opinions": [],
        }
    )

    response = client.get("/portfolio/api/positions/crypto/KRW-BTC/opinions")

    assert response.status_code == 200
    assert response.json()["supported"] is False


@pytest.mark.unit
def test_position_detail_orders_api_returns_payload() -> None:
    client, detail = _create_client()

    response = client.get("/portfolio/api/positions/us/NVDA/orders")

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["pending_count"] == 1
    assert data["recent_fills"][0]["amount"] == 455.5
    assert data["pending_orders"][0]["remaining_quantity"] == 1.5


@pytest.mark.unit
def test_position_detail_page_contains_lazy_section_hooks() -> None:
    client, _ = _create_client()
    response = client.get("/portfolio/positions/us/NVDA")
    body = response.text

    assert 'id="position-indicators-section"' in body
    assert 'id="position-orders-section"' in body
    assert 'id="position-news-section"' in body
    assert 'id="position-opinions-section"' in body
    assert "loadLazySection(" in body
    assert "function escapeHtml(value)" in body
    assert "function sanitizeUrl(value)" in body
    assert "published_at" in body
    assert "excerpt" in body
    assert "recent_fills" in body
    assert "pending_orders" in body
    assert "summary" in body
    assert "sentiment" in body
    assert "avg_target_price" in body
    assert "buy_count" in body
    assert "summary_cards" in body
    assert "distribution" in body
    assert "top_opinions" in body
    assert "overflow_count" in body
    assert "filled_at || item.ordered_at" in body


@pytest.mark.unit
def test_position_detail_page_renders_non_us_currency_and_zero_values() -> None:
    client, detail = _create_client()
    detail.get_page_payload.return_value = {
        "summary": {
            "market_type": "KR",
            "symbol": "035720",
            "name": "카카오",
            "current_price": 70000.0,
            "quantity": 10.0,
            "avg_price": 70000.0,
            "profit_loss": 0.0,
            "profit_rate": 0.0,
            "evaluation": 700000.0,
            "account_count": 1,
            "target_distance_pct": 0.0,
            "stop_distance_pct": 0.0,
        },
        "components": [
            {
                "broker": "kis",
                "account_name": "종합",
                "source": "live",
                "quantity": 10.0,
                "avg_price": 70000.0,
                "current_price": 70000.0,
                "evaluation": 700000.0,
                "profit_loss": 0.0,
                "profit_rate": 0.0,
            }
        ],
        "journal": {
            "strategy": "swing",
            "thesis": "plateau",
            "status": "active",
            "notes": "",
            "hold_until": None,
            "created_at": "2026-04-01T00:00:00+00:00",
            "updated_at": "2026-04-02T00:00:00+00:00",
            "target_price": 70000.0,
            "stop_loss": 70000.0,
            "target_distance_pct": 0.0,
            "stop_distance_pct": 0.0,
            "indicators_snapshot": None,
        },
    }

    response = client.get("/portfolio/positions/kr/035720")

    assert response.status_code == 200
    body = response.text
    assert "₩70,000.00" in body
    assert "$70,000.00" not in body
    assert "0.00%" in body


@pytest.mark.unit
def test_position_detail_page_renders_sparse_kr_payload_with_placeholders() -> None:
    client, detail = _create_client()
    detail.get_page_payload.return_value = {
        "summary": {
            "market_type": "KR",
            "symbol": "035720",
            "name": "카카오",
            "current_price": 70000.0,
            "quantity": None,
            "avg_price": None,
            "profit_loss": None,
            "profit_rate": None,
            "evaluation": None,
            "account_count": 1,
            "target_distance_pct": None,
            "stop_distance_pct": None,
        },
        "components": [
            {
                "broker": "kis",
                "account_name": "종합",
                "source": "live",
                "quantity": None,
                "avg_price": None,
                "current_price": 70000.0,
                "evaluation": None,
                "profit_loss": None,
                "profit_rate": None,
            }
        ],
        "journal": {
            "strategy": None,
            "status": None,
            "thesis": None,
            "hold_until": None,
            "target_price": None,
            "stop_loss": None,
            "target_distance_pct": None,
            "stop_distance_pct": None,
            "created_at": None,
            "updated_at": None,
            "notes": None,
            "indicators_snapshot": None,
        },
    }

    response = client.get("/portfolio/positions/kr/035720")

    assert response.status_code == 200
    assert "-" in response.text
    assert "카카오" in response.text


def test_position_detail_page_renders_compact_action_strip() -> None:
    client, detail = _create_client()
    response = client.get("/portfolio/positions/us/NVDA")
    body = response.text
    # We expect these IDs and classes in the new hero summary structure
    assert 'id="position-summary-card"' in body
    assert "전체 비중 9.8%" in body
    assert "시장 내 24.5%" in body
    assert "status-badge" in body


def test_position_detail_page_renders_rich_lazy_placeholders() -> None:
    client, detail = _create_client()
    response = client.get("/portfolio/positions/us/NVDA")
    body = response.text
    # These sections should have the new container classes and icons
    assert 'id="orders-lazy-content"' in body
    assert 'id="news-lazy-content"' in body
    assert 'id="opinions-lazy-content"' in body
    assert "bi-journal-text" in body
    assert "bi-newspaper" in body
    assert "bi-chat-dots" in body
