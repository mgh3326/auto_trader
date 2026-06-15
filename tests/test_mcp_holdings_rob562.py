
from __future__ import annotations
from decimal import Decimal
import pytest
from unittest.mock import AsyncMock

from app.mcp_server.tooling import portfolio_holdings
from app.core.config import settings

def _toss_api_position(symbol: str = "BRK.B") -> dict:
    return {
        "account": "toss",
        "account_name": "Toss",
        "broker": "toss",
        "source": "toss_api",
        "instrument_type": "equity_us",
        "market": "us",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": 100.0,
    }

def _manual_toss_position(symbol: str = "BRK.B") -> dict:
    return {
        "account": "toss:기본 계좌",
        "account_name": "기본 계좌",
        "broker": "toss",
        "source": "manual",
        "instrument_type": "equity_us",
        "market": "us",
        "symbol": symbol,
        "name": symbol,
        "quantity": 1.0,
        "avg_buy_price": 100.0,
    }

@pytest.mark.asyncio
async def test_account_order_routable_toss_manual_with_mutations_enabled(monkeypatch):
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    
    # ROB-562: Manual Toss position should be routable if API is enabled and mutations are enabled
    # Currently this fails because it only looks at source="manual"
    assert portfolio_holdings._account_order_routable(source="manual", broker="toss") is True
    
    # Samsung manual should still be False
    assert portfolio_holdings._account_order_routable(source="manual", broker="samsung") is False

@pytest.mark.asyncio
async def test_toss_group_routable_consistent_on_api_failure(monkeypatch):
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    
    # Simulate API failure: empty positions, one error, success=False
    async def fake_collect_toss_api_positions(*args, **kwargs):
        return [], [{"error": "timeout", "source": "toss_api", "degraded": True}], False

    # Manual fallback has one position
    async def fake_collect_manual_positions(*args, **kwargs):
        return [_manual_toss_position("AAPL")], []

    monkeypatch.setattr(portfolio_holdings, "_collect_toss_api_positions", fake_collect_toss_api_positions)
    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions)
    
    # Mocking other collectors to return empty
    async def _empty_upbit(*args, **kwargs): return [], []
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty_upbit)
    async def _empty_kis(*args, **kwargs): return [], []
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty_kis)

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False,
        routing_account_mode="kis_live",
    )
    
    toss_accounts = [a for a in result["accounts"] if a["broker"] == "toss"]
    assert len(toss_accounts) > 0
    toss_acc = toss_accounts[0]
    
    # ROB-562: Should be True even if it fell back to manual due to API failure
    assert toss_acc["order_routable"] is True
    
    # Check for degraded flag in errors
    toss_api_errors = [e for e in result["errors"] if e.get("source") == "toss_api"]
    assert len(toss_api_errors) > 0
    assert toss_api_errors[0].get("degraded") is True

@pytest.mark.asyncio
async def test_samsung_manual_stays_non_routable(monkeypatch):
    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    
    async def fake_collect_manual_positions(*args, **kwargs):
        return [
            {
                "account": "samsung:123",
                "account_name": "Samsung",
                "broker": "samsung",
                "source": "manual",
                "instrument_type": "equity_kr",
                "market": "kr",
                "symbol": "005930",
                "name": "Samsung",
                "quantity": 10,
                "avg_buy_price": 70000,
            }
        ], []

    monkeypatch.setattr(portfolio_holdings, "_collect_manual_positions", fake_collect_manual_positions)
    async def _empty_toss(*args, **kwargs): return [], [], True
    monkeypatch.setattr(portfolio_holdings, "_collect_toss_api_positions", _empty_toss)
    async def _empty_upbit(*args, **kwargs): return [], []
    monkeypatch.setattr(portfolio_holdings, "_collect_upbit_positions", _empty_upbit)
    async def _empty_kis(*args, **kwargs): return [], []
    monkeypatch.setattr(portfolio_holdings, "_collect_kis_positions", _empty_kis)

    result = await portfolio_holdings._get_holdings_impl(
        include_current_price=False,
        routing_account_mode="kis_live",
    )
    
    samsung_acc = next(a for a in result["accounts"] if a["broker"] == "samsung")
    assert samsung_acc["order_routable"] is False
