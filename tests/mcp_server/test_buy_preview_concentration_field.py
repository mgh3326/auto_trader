from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_execution


@pytest.mark.asyncio
async def test_shared_buy_preview_includes_concentration(monkeypatch):
    async def _fake_conc(**kwargs):
        return {
            "verdict": "over",
            "cluster": "semis_memory",
            "cap_pct": 10,
            "current_pct": 8.0,
            "projected_pct": 11.5,
            "fail_open": False,
            "warning": "semis_memory projected 11.5% exceeds sector-cluster cap 10%",
        }

    monkeypatch.setattr(
        order_execution, "evaluate_sector_concentration", _fake_conc, raising=False
    )
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=55000.0)
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "price": 55000,
                "quantity": 10,
                "estimated_value": 550000.0,
                "fee": 0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())

    result = await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=55000.0,
        dry_run=True,
        is_mock=True,
    )

    assert result["success"] is True
    assert "sector_concentration" in result
    assert result["sector_concentration"]["verdict"] == "over"
    assert (
        result["sector_concentration"]["warning"]
        == "semis_memory projected 11.5% exceeds sector-cluster cap 10%"
    )


@pytest.mark.asyncio
async def test_toss_buy_preview_includes_concentration(monkeypatch):
    # Enable Toss preview environment
    from app.core.config import settings
    from app.mcp_server.tooling import orders_toss_variants

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(orders_toss_variants, "validate_toss_api_config", lambda: [])

    # Stub cost context and currency rate helpers
    async def _stub_toss_costs():
        return {
            "version": 1,
            "accounts": {
                "toss": {
                    "broker": "toss",
                    "markets": {
                        "kr": {"commission_bps": 0.0, "fx_spread_bps": 0.0},
                        "us": {"commission_bps": 10.0, "fx_spread_bps": 1.7},
                    },
                }
            },
        }

    async def _stub_usd_krw_quote():
        return SimpleNamespace(default_rate=1360.0, source="toss")

    monkeypatch.setattr(
        orders_toss_variants,
        "get_account_costs_setting",
        AsyncMock(side_effect=_stub_toss_costs),
        raising=False,
    )
    monkeypatch.setattr(
        orders_toss_variants,
        "get_usd_krw_rate_details",
        AsyncMock(side_effect=_stub_usd_krw_quote),
        raising=False,
    )

    # Mock Toss Client
    class _FakeTossClient:
        async def aclose(self):
            return None

        async def list_orders(self, **kwargs):
            return SimpleNamespace(orders=[])

        async def fetch_multiple_current_prices(self, symbols):
            return {"005930": Decimal("50000")}

    class _FakeClientContext:
        async def __aenter__(self):
            return _FakeTossClient()

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(
        orders_toss_variants, "_client_context", lambda: _FakeClientContext()
    )

    async def _fake_conc(**kwargs):
        return {
            "verdict": "over",
            "cluster": "financials",
            "cap_pct": 10,
            "current_pct": 9.5,
            "projected_pct": 11.2,
            "fail_open": False,
            "warning": "financials projected 11.2% exceeds sector-cluster cap 10%",
        }

    monkeypatch.setattr(
        orders_toss_variants, "evaluate_sector_concentration", _fake_conc, raising=False
    )

    res = await orders_toss_variants.toss_preview_order(
        symbol="005930",
        side="buy",
        quantity=3,
        price="50000",
        order_amount="150000",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert "sector_concentration" in res
    assert res["sector_concentration"]["verdict"] == "over"
    assert (
        "financials projected 11.2% exceeds sector-cluster cap 10%"
        in res["order_warnings"]
    )

    res_place = await orders_toss_variants.toss_place_order(
        symbol="005930",
        side="buy",
        quantity="3",
        price="50000",
        order_amount="150000",
        account_mode="toss_live",
        dry_run=True,
    )
    assert res_place["success"] is True
    assert "sector_concentration" in res_place
    assert res_place["sector_concentration"]["verdict"] == "over"
