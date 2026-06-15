from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


def test_compute_us_fx_pnl_matches_total_identity():
    from app.mcp_server.tooling.fx_pnl import compute_us_equity_fx_pnl

    result = compute_us_equity_fx_pnl(
        buy_price=Decimal("100"),
        sell_price=Decimal("130"),
        quantity=Decimal("2"),
        buy_fx_rate=Decimal("1389.33"),
        sell_fx_rate=Decimal("1503.19"),
    )

    assert result["buy_notional_usd"] == Decimal("200")
    assert result["sell_notional_usd"] == Decimal("260")
    assert result["security_pnl_usd"] == Decimal("60")
    assert result["security_pnl_krw"] == Decimal("90191.4000")
    assert result["fx_pnl_krw"] == Decimal("22772.0000")
    assert result["total_pnl_krw"] == Decimal("112963.4000")


def test_compute_us_fx_pnl_returns_none_when_buy_fx_missing():
    from app.mcp_server.tooling.fx_pnl import compute_us_equity_fx_pnl

    assert (
        compute_us_equity_fx_pnl(
            buy_price=Decimal("100"),
            sell_price=Decimal("130"),
            quantity=Decimal("2"),
            buy_fx_rate=None,
            sell_fx_rate=Decimal("1503.19"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_capture_reconcile_spot_labels_approximate(monkeypatch):
    from app.mcp_server.tooling import fx_pnl

    quote = fx_pnl.UsdKrwExchangeRateQuote(
        rate=1503.19,
        mid_rate=1503.19,
        source="toss",
    )
    monkeypatch.setattr(
        fx_pnl,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=quote),
    )

    captured = await fx_pnl.capture_reconcile_spot_fx()

    assert captured.rate == Decimal("1503.19")
    assert captured.fx_rate_source == "reconcile_spot"
    assert captured.fx_pnl_accuracy == "approximate"


@pytest.mark.asyncio
async def test_capture_reconcile_spot_fails_open(monkeypatch):
    from app.mcp_server.tooling import fx_pnl

    monkeypatch.setattr(
        fx_pnl,
        "get_usd_krw_rate_details",
        AsyncMock(side_effect=RuntimeError("fx down")),
    )

    captured = await fx_pnl.capture_reconcile_spot_fx()

    assert captured.rate is None
    assert captured.fx_rate_source == "unavailable"
    assert captured.fx_pnl_accuracy == "unavailable"
