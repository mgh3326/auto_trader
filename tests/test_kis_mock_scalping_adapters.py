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
async def test_read_snapshot_uses_kis_client_balance_facade(monkeypatch):
    # ROB-341 operator smoke uses the KISClient facade object. The facade has no
    # public .account child, so _read_snapshot must call a public facade method.
    broker = KisMockBroker(get_state=lambda s: None)

    class FakeClient:
        async def fetch_domestic_balance_snapshot(self, *, is_mock: bool):
            assert is_mock is True
            return {
                "holdings": [{"pdno": "005930", "hldg_qty": "2"}],
                "cash": {"dnca_tot_amt": "12345"},
            }

    monkeypatch.setattr(broker, "_get_mock_client", lambda: FakeClient())

    qty, cash = await broker._read_snapshot("005930")

    assert qty == Decimal("2")
    assert cash == Decimal("12345")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_buy_skips_baseline_snapshot(monkeypatch):
    # ROB-341 review #6: a dry-run (confirm=False) submit never reaches
    # confirm_fill, so it must not pay for a baseline balance snapshot.
    import app.services.brokers.kis.mock_scalping_exec.adapters as mod

    async def fake_place(**kwargs):
        return {"odno": "0001"}

    monkeypatch.setattr(mod, "_place_order_impl", fake_place)
    broker = KisMockBroker(get_state=lambda s: None)
    called = {"n": 0}

    async def fake_snapshot(symbol):
        called["n"] += 1
        return Decimal("0"), None

    monkeypatch.setattr(broker, "_read_snapshot", fake_snapshot)
    result = await broker.submit_buy(
        symbol="005930",
        price=Decimal("70000"),
        quantity=Decimal("1"),
        correlation_id="cid1",
        confirm=False,
    )
    assert called["n"] == 0  # no baseline read on dry-run
    assert "_baseline" not in result


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
