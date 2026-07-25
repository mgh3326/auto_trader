"""ROB-692 — stock-detail deterministic recommendation card (service + router).

Covers: pass-through of the already-floored `build_recommendation_for_equity`
fields, the ROB-690 R:R reuse (fail-closed on non-`long`/degenerate/mismatched
triangles, buy_zone entry fallback), and the router's crypto/404/auth
behaviour mirroring the `research-consensus` sibling.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.routers import invest_api
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import router as invest_api_router
from app.schemas.invest_stock_detail_recommendation import (
    StockDetailRecommendationResponse,
)
from app.services.invest_view_model.stock_detail_recommendation_service import (
    StockDetailRecommendationProviders,
    build_stock_detail_recommendation,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import SymbolNotFound


def _make_analyze(
    *,
    action: str,
    confidence: str = "medium",
    current_price: float | None = 100.0,
    stop_loss: float | None = 90.0,
    sell_targets: list[dict[str, Any]] | None = None,
    buy_zones: list[dict[str, Any]] | None = None,
    insufficient_inputs: list[str] | None = None,
    rsi14: float | None = 55.2,
    reasoning: str = "RSI 55.2 (neutral)",
    symbol: str = "AAPL",
    derived_as_of: str = "2026-07-04T09:00:00+09:00",
):
    async def _fake(
        symbol_arg: str, market: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "quote": {"price": current_price},
            "recommendation": {
                "action": action,
                "confidence": confidence,
                "rsi14": rsi14,
                "buy_zones": buy_zones if buy_zones is not None else [],
                "sell_targets": sell_targets if sell_targets is not None else [],
                "stop_loss": stop_loss,
                "reasoning": reasoning,
                "insufficient_inputs": insufficient_inputs or [],
            },
            "profile": {},
            "derived_as_of": derived_as_of,
        }

    return _fake


# ---------------------------------------------------------------------------
# Service — field pass-through
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_passes_through_recommendation_fields():
    analyze = _make_analyze(
        action="hold",
        confidence="medium",
        current_price=100.0,
        stop_loss=90.0,
        sell_targets=[
            {"price": 120.0, "type": "resistance", "reasoning": "Resistance at 120.0"}
        ],
        buy_zones=[{"price": 95.0, "type": "support", "reasoning": "Support at 95.0"}],
        insufficient_inputs=["consensus"],
        rsi14=55.2,
        reasoning="RSI 55.2 (neutral)",
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.market == "us"
    assert response.symbol == "AAPL"
    assert response.action == "hold"
    assert response.confidence == "medium"
    assert response.rsi14 == 55.2
    assert response.reasoning == "RSI 55.2 (neutral)"
    assert response.insufficient_inputs == ["consensus"]
    assert response.current_price == 100.0
    assert len(response.buy_zones) == 1
    assert response.buy_zones[0].price == 95.0
    assert response.buy_zones[0].type == "support"
    assert len(response.sell_targets) == 1
    assert response.sell_targets[0].price == 120.0
    assert response.stop_loss == 90.0
    # hold -> unknown direction -> no R:R chip
    assert response.trade_setup is None


# ---------------------------------------------------------------------------
# R:R happy path (fulfills ROB-690 Step 4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buy_recommendation_computes_rr_via_shared_helper():
    analyze = _make_analyze(
        action="buy",
        confidence="high",
        current_price=100.0,
        stop_loss=90.0,
        sell_targets=[
            {"price": 120.0, "type": "resistance", "reasoning": "Resistance at 120.0"}
        ],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.trade_setup is not None
    setup = response.trade_setup
    assert setup.direction == "long"
    assert setup.entry == "100.0"
    assert setup.stop == "90.0"
    assert setup.target == "120.0"
    assert setup.risk_pct == "10.00"
    assert setup.reward_pct == "20.00"
    assert setup.rr_ratio == "2.00"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buy_recommendation_falls_back_to_top_buy_zone_when_price_missing():
    analyze = _make_analyze(
        action="buy",
        current_price=None,
        stop_loss=90.0,
        sell_targets=[
            {"price": 120.0, "type": "resistance", "reasoning": "Resistance at 120.0"}
        ],
        buy_zones=[
            {"price": 92.0, "type": "support", "reasoning": "Support at 92.0"},
            {"price": 98.0, "type": "bollinger_lower", "reasoning": "BB lower"},
        ],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.current_price is None
    assert response.trade_setup is not None
    # top (highest / last, since buy_zones sort ascending) buy_zone used as entry
    assert response.trade_setup.entry == "98.0"


# ---------------------------------------------------------------------------
# Fail-closed cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sell_recommendation_omits_rr_chip():
    analyze = _make_analyze(
        action="sell",
        current_price=100.0,
        stop_loss=90.0,
        sell_targets=[{"price": 120.0, "type": "resistance", "reasoning": "r"}],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.action == "sell"
    assert response.trade_setup is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hold_recommendation_omits_rr_chip():
    analyze = _make_analyze(
        action="hold",
        current_price=100.0,
        stop_loss=90.0,
        sell_targets=[{"price": 120.0, "type": "resistance", "reasoning": "r"}],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.action == "hold"
    assert response.trade_setup is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buy_recommendation_without_sell_targets_omits_rr_chip():
    analyze = _make_analyze(
        action="buy",
        current_price=100.0,
        stop_loss=90.0,
        sell_targets=[],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.action == "buy"
    assert response.trade_setup is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_buy_recommendation_with_degenerate_stop_omits_rr_chip():
    """stop_loss >= current_price violates the long triangle (stop < entry <
    target) -> direction_price_mismatch -> fail-closed (no chip)."""
    analyze = _make_analyze(
        action="buy",
        current_price=100.0,
        stop_loss=105.0,
        sell_targets=[{"price": 120.0, "type": "resistance", "reasoning": "r"}],
    )

    response = await build_stock_detail_recommendation(
        market="us",
        symbol="AAPL",
        providers=StockDetailRecommendationProviders(analyze=analyze),
    )

    assert response.action == "buy"
    assert response.trade_setup is None


# ---------------------------------------------------------------------------
# Service — unsupported symbol format maps to SymbolNotFound
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_maps_analyze_value_error_to_symbol_not_found():
    async def _raise(symbol_arg: str, market: str | None = None, **kwargs: Any):
        raise ValueError("Unsupported symbol format: '???'")

    with pytest.raises(SymbolNotFound):
        await build_stock_detail_recommendation(
            market="us",
            symbol="???",
            providers=StockDetailRecommendationProviders(analyze=_raise),
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    async def _stub_recommendation(**kwargs: Any):
        assert kwargs["market"] == "us"
        assert kwargs["symbol"] == "AAPL"
        return StockDetailRecommendationResponse(
            market="us",
            symbol="AAPL",
            name="Apple Inc.",
            as_of="2026-07-04T09:00:00+09:00",
            current_price=100.0,
            action="buy",
            confidence="high",
            rsi14=28.5,
            reasoning="RSI 28.5 (oversold)",
            insufficient_inputs=[],
            buy_zones=[],
            sell_targets=[{"price": 120.0, "type": "resistance", "reasoning": "r"}],
            stop_loss=90.0,
            trade_setup={
                "direction": "long",
                "entry": "100.0",
                "stop": "90.0",
                "target": "120.0",
                "risk_pct": "10.00",
                "reward_pct": "20.00",
                "rr_ratio": "2.00",
            },
        )

    monkeypatch.setattr(
        invest_api, "build_stock_detail_recommendation", _stub_recommendation
    )
    return TestClient(app)


@pytest.mark.unit
def test_recommendation_route_returns_read_only_contract(client: TestClient) -> None:
    response = client.get("/invest/api/stock-detail/us/AAPL/recommendation")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["market"] == "us"
    assert body["action"] == "buy"
    assert body["confidence"] == "high"
    assert body["trade_setup"]["direction"] == "long"
    assert body["trade_setup"]["rr_ratio"] == "2.00"


@pytest.mark.unit
def test_recommendation_route_rejects_crypto(client: TestClient) -> None:
    response = client.get("/invest/api/stock-detail/crypto/KRW-BTC/recommendation")

    assert response.status_code == 400
    assert response.json()["detail"] == "research_recommendation_supports_kr_us_only"


@pytest.mark.unit
def test_recommendation_route_maps_symbol_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_not_found(**kwargs: Any):
        raise SymbolNotFound("missing")

    monkeypatch.setattr(
        invest_api, "build_stock_detail_recommendation", _raise_not_found
    )

    response = client.get("/invest/api/stock-detail/us/MISSING/recommendation")

    assert response.status_code == 404
    assert response.json()["detail"] == "symbol_not_found"


@pytest.mark.unit
def test_recommendation_route_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a session/state user, `get_authenticated_user` raises 401 — the
    dependency stays wired (unlike the `client` fixture, this app does NOT
    override `get_authenticated_user`)."""
    from unittest.mock import AsyncMock

    app = FastAPI()
    app.include_router(invest_api_router)
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()

    monkeypatch.setattr(
        "app.routers.dependencies.get_current_user_from_session",
        AsyncMock(return_value=None),
    )

    unauth_client = TestClient(app)
    response = unauth_client.get("/invest/api/stock-detail/us/AAPL/recommendation")

    assert response.status_code == 401
