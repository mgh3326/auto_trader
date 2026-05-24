"""ROB-307 PR3 — tests for reconcile_bracket (detect exit, cancel survivor).

A bracketed position is held (parent row 'filled', exits resting). On a
later tick reconcile_bracket checks the venue: if still holding → no-op
('still_protected'); if an exit fired (futures flat / spot OCO resolved)
→ cancel any surviving leg and drive the parent filled→closed→reconciled.
Broker faked; ledger is the real service on the test DB.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)


class _OO:
    def __init__(self, coid):
        self.client_order_id = coid


class _OpenOrders:
    def __init__(self, orders):
        self.orders = orders


class _Position:
    def __init__(self, amt):
        self.position_amt = amt
        self.is_flat = amt == 0


class _FutReconClient:
    def __init__(self, *, flat, open_orders):
        self._flat = flat
        self._open = list(open_orders)
        self.cancels: list[str] = []

    async def get_position(self, *, symbol):
        return _Position(Decimal("0") if self._flat else Decimal("7.3"))

    async def get_open_orders(self, *, symbol):
        return _OpenOrders(list(self._open))

    async def cancel_order(self, *, symbol, client_order_id):
        self.cancels.append(client_order_id)
        self._open = [o for o in self._open if o.client_order_id != client_order_id]
        return None


class _SpotReconClient:
    def __init__(self, *, open_orders):
        self._open = list(open_orders)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders(list(self._open))


async def _instrument(db_session, symbol, product) -> int:
    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == product,
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product=product,
        venue_symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    return inst.id


async def _make_filled_parent(db_session, product, symbol) -> str:
    svc = BinanceDemoLedgerService(db_session)
    iid = await _instrument(db_session, symbol, product)
    cid = f"recon-{product}-{symbol}"
    host = "demo-api.binance.com" if product == "spot" else "demo-fapi.binance.com"
    await svc.record_planned(
        instrument_id=iid,
        product=product,
        venue_host=host,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("7.3"),
        price=None,
        extra_metadata={"bracket": {"type": "test"}},
        now=_NOW,
    )
    await svc.record_previewed(client_order_id=cid, now=_NOW)
    await svc.record_validated(client_order_id=cid, now=_NOW)
    await svc.record_submitted(client_order_id=cid, broker_order_id="b1", now=_NOW)
    await svc.record_filled(client_order_id=cid, now=_NOW)
    return cid


async def _state(db_session, cid) -> str:
    row = await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == cid
        )
    )
    return row.lifecycle_state


@pytest.mark.asyncio
async def test_futures_still_holding_is_noop(db_session) -> None:
    cid = await _make_filled_parent(db_session, "usdm_futures", "RCFUTAUSDT")
    client = _FutReconClient(flat=False, open_orders=[_OO("sl"), _OO("tp")])
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=None,
        now=_NOW,
    )
    result = await ex.reconcile_bracket(open_client_order_id=cid)
    assert result.status == "still_protected"
    assert client.cancels == []
    assert await _state(db_session, cid) == "filled"  # still held


@pytest.mark.asyncio
async def test_futures_exit_fired_cancels_survivor_and_reconciles(db_session) -> None:
    cid = await _make_filled_parent(db_session, "usdm_futures", "RCFUTBUSDT")
    # Position went flat (TP fired); the SL leg still rests and must be cancelled.
    client = _FutReconClient(flat=True, open_orders=[_OO("surviving-sl")])
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=None,
        now=_NOW,
    )
    result = await ex.reconcile_bracket(open_client_order_id=cid)
    assert result.status == "reconciled"
    assert result.final_flat is True
    assert client.cancels == ["surviving-sl"]
    assert await _state(db_session, cid) == "reconciled"


@pytest.mark.asyncio
async def test_spot_oco_resolved_reconciles(db_session) -> None:
    cid = await _make_filled_parent(db_session, "spot", "RCSPOTAUSDT")
    client = _SpotReconClient(
        open_orders=[]
    )  # OCO resolved (one filled, other auto-cancelled)
    ex = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=None,
        now=_NOW,
    )
    result = await ex.reconcile_bracket(open_client_order_id=cid)
    assert result.status == "reconciled"
    assert await _state(db_session, cid) == "reconciled"


@pytest.mark.asyncio
async def test_spot_oco_still_resting_is_noop(db_session) -> None:
    cid = await _make_filled_parent(db_session, "spot", "RCSPOTBUSDT")
    client = _SpotReconClient(open_orders=[_OO("tp"), _OO("sl")])
    ex = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=None,
        now=_NOW,
    )
    result = await ex.reconcile_bracket(open_client_order_id=cid)
    assert result.status == "still_protected"
    assert await _state(db_session, cid) == "filled"


@pytest.mark.asyncio
async def test_reconcile_noop_when_parent_not_filled(db_session) -> None:
    # A parent already reconciled (terminal) is a no-op, not a re-close.
    cid = await _make_filled_parent(db_session, "spot", "RCSPOTCUSDT")
    svc = BinanceDemoLedgerService(db_session)
    await svc.record_closed(client_order_id=cid, now=_NOW)
    await svc.record_reconciled(client_order_id=cid, now=_NOW)
    client = _SpotReconClient(open_orders=[])
    ex = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=None,
        now=_NOW,
    )
    result = await ex.reconcile_bracket(open_client_order_id=cid)
    assert result.status == "noop"
