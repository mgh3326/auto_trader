"""ROB-341 — KisMockBroker holdings/cash-delta confirm_fill wiring tests.

confirm_fill must derive its verdict from the baseline-vs-post holdings delta
(stamped into the submit result at submit time), never from daily-ccld. Fakes
the snapshot read; no broker / network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.kis.mock_scalping_exec.adapters import KisMockBroker
from app.services.brokers.kis.mock_scalping_exec.executor import Fill


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_uses_holdings_delta(monkeypatch):
    broker = KisMockBroker(get_state=lambda s: None)

    async def fake_snapshot(symbol):  # post-submit observed holdings + cash
        return Decimal("10"), Decimal("300000")

    monkeypatch.setattr(broker, "_read_snapshot", fake_snapshot)

    submit_result = {
        "odno": "0001",
        "_baseline": {
            "symbol": "005930",
            "side": "buy",
            "ordered_qty": "10",
            "limit_price": "70000",
            "holdings_qty": "0",
            "cash": "1000000",
        },
    }
    fill = await broker.confirm_fill(submit_result)
    assert isinstance(fill, Fill)
    assert fill.quantity == Decimal("10")
    assert fill.price == Decimal("70000")  # 700000 cash delta / 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_no_baseline_fails_closed():
    broker = KisMockBroker(get_state=lambda s: None)
    assert await broker.confirm_fill({"odno": "0001"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_fill_no_delta_fails_closed(monkeypatch):
    broker = KisMockBroker(get_state=lambda s: None)

    async def fake_snapshot(symbol):
        return Decimal("0"), Decimal("1000000")  # nothing filled

    monkeypatch.setattr(broker, "_read_snapshot", fake_snapshot)
    submit_result = {
        "odno": "0001",
        "_baseline": {
            "symbol": "005930",
            "side": "buy",
            "ordered_qty": "10",
            "limit_price": "70000",
            "holdings_qty": "0",
            "cash": "1000000",
        },
    }
    assert await broker.confirm_fill(submit_result) is None
