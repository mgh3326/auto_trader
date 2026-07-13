"""ROB-305 — Spot Demo confirm lifecycle preserves legal ledger transitions.

Regression guard for the issue's "Spot Demo actual-order smoke path preserves
legal ledger transitions and zero open orders" requirement. Unlike Futures
(§4), Spot Demo MARKET orders fill synchronously and report ``FILLED`` on the
submit response, so there is no NEW-reconcile branch here — this test pins the
happy-path round-trip: ``planned → previewed → validated → submitted → filled
→ closed → reconciled`` for both legs, ending flat with zero open orders.

Driven through ``_execute_confirm_lifecycle`` with a hand-written fake broker
(no HTTP) and a real ``BinanceDemoLedgerService`` over the test DB.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

import scripts.binance_spot_demo_smoke as spot_smoke
from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.spot_demo.dto import (
    SpotDemoAssetBalance,
    SpotDemoOpenOrdersResult,
    SpotDemoOrderSubmitResult,
    SpotDemoOrderTestResult,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    SpotDemoDryRunResult,
)

_QTY = Decimal("30")
_PRICE = Decimal("0.5")

pytestmark = pytest.mark.usefixtures("binance_demo_smoke_ledger_isolation")


class _FakeSpotExecution:
    """In-memory stand-in: MARKET BUY fills, SELL flattens to zero free."""

    def __init__(self) -> None:
        self._sell_submitted = False
        self.submit_calls: list[dict[str, Any]] = []

    credential_fingerprint = "sha256:" + "51" * 32

    def preview_submit(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
    ) -> SpotDemoDryRunResult:
        return SpotDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=client_order_id,
        )

    async def order_test(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None = None,
        time_in_force: str | None = None,
    ) -> SpotDemoOrderTestResult:
        return SpotDemoOrderTestResult(
            symbol=symbol, side=side, order_type=order_type, qty=qty
        )

    async def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        confirm: bool = False,
    ) -> SpotDemoOrderSubmitResult:
        assert confirm is True, "lifecycle must submit with confirm=True"
        self.submit_calls.append({"cid": client_order_id, "side": side})
        if side == "SELL":
            self._sell_submitted = True
        return SpotDemoOrderSubmitResult(
            client_order_id=client_order_id,
            broker_order_id=f"bk-{client_order_id}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            executed_qty=qty,
            cummulative_quote_qty=qty * _PRICE,
            status="FILLED",
        )

    async def get_asset_balance(self, *, asset: str) -> SpotDemoAssetBalance:
        # Sellable balance after the BUY; flat after the SELL.
        free = Decimal("0") if self._sell_submitted else _QTY
        return SpotDemoAssetBalance(asset=asset, free=free, locked=Decimal("0"))

    async def get_open_orders(self, *, symbol: str) -> SpotDemoOpenOrdersResult:
        return SpotDemoOpenOrdersResult(orders=[])


@pytest.mark.asyncio
async def test_spot_confirm_round_trip_reaches_reconciled_zero_open_orders(
    db_session: Any,
) -> None:
    """MARKET BUY→SELL round-trip ends both legs reconciled, zero open orders."""
    execution = _FakeSpotExecution()
    ledger = BinanceDemoLedgerService(db_session)
    instrument_id = await ledger.resolve_or_create_instrument(
        venue="binance",
        product="spot",
        venue_symbol="XRPUSDT",
        base_asset="XRP",
        quote_asset="USDT",
    )
    buy_cid = spot_smoke._new_cid()
    close_cid = spot_smoke._new_cid()
    report: dict[str, Any] = {"blockers": []}

    exit_code = await spot_smoke._execute_confirm_lifecycle(
        execution=execution,
        ledger=ledger,
        session=db_session,
        venue_host="demo-api.binance.com",
        instrument_id=instrument_id,
        buy_cid=buy_cid,
        close_cid=close_cid,
        symbol="XRPUSDT",
        order_type="MARKET",
        price=None,
        qty=_QTY,
        notional=_QTY * _PRICE,
        close_with="SELL",
        step_size=Decimal("1"),
        min_notional=Decimal("5"),
        ref_price=_PRICE,
        report=report,
    )

    assert exit_code == 0
    assert report["open_orders_count"] == 0
    assert report["reconciliation_status"] == "reconciled"

    buy_row = await ledger.get_by_client_order_id(buy_cid)
    assert buy_row is not None
    assert buy_row.lifecycle_state == "reconciled"
    # Legal chain: every intermediate state stamped on the way to reconciled.
    assert buy_row.filled_at is not None
    assert buy_row.closed_at is not None
    assert buy_row.reconciled_at is not None
    assert (
        buy_row.extra_metadata["credential_fingerprint"]
        == execution.credential_fingerprint
    )

    close_row = await ledger.get_by_client_order_id(close_cid)
    assert close_row is not None
    assert close_row.lifecycle_state == "reconciled"
    assert close_row.filled_at is not None


@pytest.mark.asyncio
async def test_spot_confirm_cross_symbol_global_cap_loser_has_zero_broker_calls() -> (
    None
):
    """Two real smoke handlers share the atomic root cap; only one proceeds."""
    winner_at_order_test = asyncio.Event()
    release_winner = asyncio.Event()

    class _GatedWinner(_FakeSpotExecution):
        async def order_test(self, **kwargs):
            winner_at_order_test.set()
            await release_winner.wait()
            return await super().order_test(**kwargs)

    class _ExplodingLoser(_FakeSpotExecution):
        broker_calls = 0

        def preview_submit(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must stop before preview")

        async def order_test(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must stop before order_test")

        async def submit_order(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must dispatch zero POST")

    async def _run(symbol: str, execution: _FakeSpotExecution) -> int:
        async with AsyncSessionLocal() as session:
            ledger = BinanceDemoLedgerService(session)
            instrument_id = await ledger.resolve_or_create_instrument(
                venue="binance",
                product="spot",
                venue_symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
            )
            return await spot_smoke._execute_confirm_lifecycle(
                execution=execution,
                ledger=ledger,
                session=session,
                venue_host="demo-api.binance.com",
                instrument_id=instrument_id,
                buy_cid=spot_smoke._new_cid(),
                close_cid=spot_smoke._new_cid(),
                symbol=symbol,
                order_type="MARKET",
                price=None,
                qty=_QTY,
                notional=_QTY * _PRICE,
                close_with="SELL",
                step_size=Decimal("1"),
                min_notional=Decimal("5"),
                ref_price=_PRICE,
                report={"blockers": []},
            )

    winner = _GatedWinner()
    loser = _ExplodingLoser()
    winner_task = asyncio.create_task(_run("R844SMOKEAUSDT", winner))
    await winner_at_order_test.wait()
    loser_result = await _run("R844SMOKEBUSDT", loser)
    release_winner.set()
    winner_result = await winner_task

    assert winner_result == 0
    assert loser_result == 1
    assert loser.broker_calls == 0
    assert loser.submit_calls == []
