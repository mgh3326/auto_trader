"""ROB-305 §4 — Futures Demo confirm lifecycle reconciles ``status=NEW``.

Recent Demo evidence showed a MARKET submit can return ``status=NEW`` even
when the account later reflects the fill. The confirm lifecycle must:

  * NOT treat a submit-response ``NEW`` as immediate success/failure;
  * NOT advance a ``submitted`` ledger row straight to ``closed``
    (the locked state machine forbids ``submitted → closed``);
  * reconcile through signed reads (``GET /fapi/v1/order`` + positionRisk +
    openOrders) so the row reaches ``submitted → filled → closed/reconciled``;
  * when a fill cannot be proven yet the account is flat with zero open
    orders, record a safe anomaly instead of a fake clean success.

These tests drive ``_execute_confirm_lifecycle`` with a hand-written fake
broker (no HTTP) and a real ``BinanceDemoLedgerService`` over the test DB,
so the assertions are about real ledger transitions, not mock calls.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

import scripts.binance_futures_demo_smoke as smoke
from app.core.db import AsyncSessionLocal
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoLeverageResult,
    FuturesDemoOpenOrdersResult,
    FuturesDemoOrderStatusResult,
    FuturesDemoOrderSubmitResult,
    FuturesDemoOrderTestResult,
    FuturesDemoPositionModeResult,
    FuturesDemoPositionResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    FuturesDemoDryRunResult,
)

_QTY = Decimal("30")
_PRICE = Decimal("0.5")

pytestmark = pytest.mark.usefixtures("binance_demo_smoke_ledger_isolation")


class _FakeFuturesExecution:
    """Deterministic in-memory stand-in for the demo-fapi broker.

    Models the realistic NEW→fill behaviour: a MARKET order may report a
    ``status`` of NEW on submit while the *account* (positionRisk) already
    reflects the fill. ``open_get_order_statuses`` / ``close_get_order_statuses``
    are the successive ``GET /fapi/v1/order`` poll results.
    """

    def __init__(
        self,
        *,
        open_submit_status: str,
        close_submit_status: str,
        open_get_order_statuses: list[str] | None = None,
        close_get_order_statuses: list[str] | None = None,
        open_get_order_raises: int = 0,
        close_get_order_raises: int = 0,
        position_after_open: Decimal = _QTY,
        position_after_close: Decimal = Decimal("0"),
    ) -> None:
        self._open_submit_status = open_submit_status
        self._close_submit_status = close_submit_status
        self._open_get_order_statuses = list(open_get_order_statuses or [])
        self._close_get_order_statuses = list(close_get_order_statuses or [])
        # Number of leading get_order calls (per leg) that raise a transient
        # error before normal behaviour — models demo-fapi returning 400 for a
        # just-submitted order it has not yet indexed for lookup.
        self._open_get_order_raises = open_get_order_raises
        self._close_get_order_raises = close_get_order_raises
        self._position_after_open = position_after_open
        self._position_after_close = position_after_close
        self._open_cid: str | None = None
        self._close_cid: str | None = None
        self._close_submitted = False
        self.get_order_calls: list[str] = []
        self.submit_calls: list[dict[str, Any]] = []

    credential_fingerprint = "sha256:" + "52" * 32

    async def get_position_mode(self) -> FuturesDemoPositionModeResult:
        return FuturesDemoPositionModeResult(is_hedge_mode=False)

    async def set_leverage(
        self, *, symbol: str, leverage: int
    ) -> FuturesDemoLeverageResult:
        return FuturesDemoLeverageResult(
            symbol=symbol, leverage=leverage, max_notional_value=Decimal("1000")
        )

    def preview_submit(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> FuturesDemoDryRunResult:
        return FuturesDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
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
        reduce_only: bool = False,
    ) -> FuturesDemoOrderTestResult:
        return FuturesDemoOrderTestResult(
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
        reduce_only: bool = False,
        confirm: bool = False,
    ) -> FuturesDemoOrderSubmitResult:
        assert confirm is True, "lifecycle must submit with confirm=True"
        self.submit_calls.append({"cid": client_order_id, "reduce_only": reduce_only})
        if reduce_only:
            self._close_cid = client_order_id
            self._close_submitted = True
            status = self._close_submit_status
        else:
            self._open_cid = client_order_id
            status = self._open_submit_status
        return FuturesDemoOrderSubmitResult(
            client_order_id=client_order_id,
            broker_order_id=f"bk-{client_order_id}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            executed_qty=qty if status == "FILLED" else Decimal("0"),
            avg_price=_PRICE,
            status=status,
            reduce_only=reduce_only,
        )

    async def get_order(
        self, *, symbol: str, client_order_id: str
    ) -> FuturesDemoOrderStatusResult:
        self.get_order_calls.append(client_order_id)
        if client_order_id == self._close_cid:
            if self._close_get_order_raises > 0:
                self._close_get_order_raises -= 1
                raise RuntimeError("demo-fapi 400: order not yet queryable")
            queue = self._close_get_order_statuses
            fallback = self._close_submit_status
        else:
            if self._open_get_order_raises > 0:
                self._open_get_order_raises -= 1
                raise RuntimeError("demo-fapi 400: order not yet queryable")
            queue = self._open_get_order_statuses
            fallback = self._open_submit_status
        status = queue.pop(0) if queue else fallback
        return FuturesDemoOrderStatusResult(
            client_order_id=client_order_id,
            broker_order_id=f"bk-{client_order_id}",
            symbol=symbol,
            side="BUY",
            order_type="MARKET",
            status=status,
            orig_qty=_QTY,
            executed_qty=_QTY if status == "FILLED" else Decimal("0"),
            avg_price=_PRICE,
            reduce_only=False,
        )

    async def get_position(self, *, symbol: str) -> FuturesDemoPositionResult:
        amt = (
            self._position_after_close
            if self._close_submitted
            else self._position_after_open
        )
        return FuturesDemoPositionResult(
            symbol=symbol,
            position_amt=amt,
            entry_price=_PRICE,
            leverage=1,
            is_flat=(amt == 0),
        )

    async def get_open_orders(self, *, symbol: str) -> FuturesDemoOpenOrdersResult:
        return FuturesDemoOpenOrdersResult(orders=[])


async def _run_lifecycle(
    *,
    execution: _FakeFuturesExecution,
    db_session: Any,
) -> tuple[int, BinanceDemoLedgerService, str, str]:
    ledger = BinanceDemoLedgerService(db_session)
    instrument_id = await ledger.resolve_or_create_instrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol="XRPUSDT",
        base_asset="XRP",
        quote_asset="USDT",
    )
    open_cid = smoke._new_cid()
    close_cid = smoke._new_cid()
    exit_code = await smoke._execute_confirm_lifecycle(
        execution=execution,
        ledger=ledger,
        session=db_session,
        venue_host="demo-fapi.binance.com",
        instrument_id=instrument_id,
        open_cid=open_cid,
        close_cid=close_cid,
        symbol="XRPUSDT",
        side="BUY",
        order_type="MARKET",
        price=None,
        qty=_QTY,
        notional=_QTY * _PRICE,
        leverage=1,
        close_with="SELL",
        close_step_size=Decimal("1"),
        quantity_precision=0,
    )
    return exit_code, ledger, open_cid, close_cid


@pytest.fixture(autouse=True)
def _no_poll_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bounded reconcile poll instantaneous in tests."""
    monkeypatch.setattr(smoke, "_FILL_RECONCILE_DELAY_SECONDS", 0.0, raising=False)


@pytest.mark.asyncio
async def test_open_new_resolved_filled_via_get_order(db_session: Any) -> None:
    """Open submit=NEW then GET /fapi/v1/order=FILLED → legal reconcile, exit 0."""
    execution = _FakeFuturesExecution(
        open_submit_status="NEW",
        close_submit_status="FILLED",
        open_get_order_statuses=["FILLED"],
    )
    exit_code, ledger, open_cid, close_cid = await _run_lifecycle(
        execution=execution, db_session=db_session
    )

    assert exit_code == 0
    # The open row was reconciled through the legal chain (filled set on the way).
    open_row = await ledger.get_by_client_order_id(open_cid)
    assert open_row is not None
    assert open_row.lifecycle_state == "reconciled"
    assert open_row.filled_at is not None
    # Reconciliation actually polled the order status for the open cid.
    assert open_cid in execution.get_order_calls
    close_row = await ledger.get_by_client_order_id(close_cid)
    assert close_row is not None
    assert close_row.lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_futures_confirm_cross_symbol_global_cap_loser_has_zero_broker_calls() -> (
    None
):
    """Reservation happens before position/leverage/order calls in the handler."""
    winner_at_position_mode = asyncio.Event()
    release_winner = asyncio.Event()

    class _GatedWinner(_FakeFuturesExecution):
        async def get_position_mode(self):
            winner_at_position_mode.set()
            await release_winner.wait()
            return await super().get_position_mode()

    class _ExplodingLoser(_FakeFuturesExecution):
        broker_calls = 0

        async def get_position_mode(self):
            self.broker_calls += 1
            raise AssertionError("reservation loser must dispatch zero broker reads")

        async def set_leverage(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must dispatch zero mutation")

        def preview_submit(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must stop before preview")

        async def order_test(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must stop before order_test")

        async def submit_order(self, **kwargs):
            self.broker_calls += 1
            raise AssertionError("reservation loser must dispatch zero POST")

    def _execution(cls):
        return cls(open_submit_status="FILLED", close_submit_status="FILLED")

    async def _run(symbol: str, execution: _FakeFuturesExecution) -> int:
        async with AsyncSessionLocal() as session:
            ledger = BinanceDemoLedgerService(session)
            instrument_id = await ledger.resolve_or_create_instrument(
                venue="binance",
                product="usdm_futures",
                venue_symbol=symbol,
                base_asset=symbol.removesuffix("USDT"),
                quote_asset="USDT",
            )
            return await smoke._execute_confirm_lifecycle(
                execution=execution,
                ledger=ledger,
                session=session,
                venue_host="demo-fapi.binance.com",
                instrument_id=instrument_id,
                open_cid=smoke._new_cid(),
                close_cid=smoke._new_cid(),
                symbol=symbol,
                side="BUY",
                order_type="MARKET",
                price=None,
                qty=_QTY,
                notional=_QTY * _PRICE,
                leverage=1,
                close_with="SELL",
                close_step_size=Decimal("1"),
                quantity_precision=0,
            )

    winner = _execution(_GatedWinner)
    loser = _execution(_ExplodingLoser)
    winner_task = asyncio.create_task(_run("R844FSMOKEAUSDT", winner))
    await winner_at_position_mode.wait()
    loser_result = await _run("R844FSMOKEBUSDT", loser)
    release_winner.set()
    winner_result = await winner_task

    assert winner_result == 0
    assert loser_result == 1
    assert loser.broker_calls == 0
    assert loser.submit_calls == []


@pytest.mark.asyncio
async def test_open_new_resolved_filled_via_position_evidence(db_session: Any) -> None:
    """Open NEW + GET stays NEW but positionRisk non-flat → filled by evidence."""
    execution = _FakeFuturesExecution(
        open_submit_status="NEW",
        close_submit_status="FILLED",
        open_get_order_statuses=["NEW", "NEW", "NEW", "NEW", "NEW"],
        position_after_open=_QTY,  # account reflects the fill even though status=NEW
    )
    exit_code, ledger, open_cid, _close_cid = await _run_lifecycle(
        execution=execution, db_session=db_session
    )

    assert exit_code == 0
    open_row = await ledger.get_by_client_order_id(open_cid)
    assert open_row is not None
    assert open_row.filled_at is not None
    assert open_row.lifecycle_state == "reconciled"
    # The fill was recorded from account-state evidence, not order status.
    assert (open_row.extra_metadata or {}).get(
        "fill_evidence"
    ) == "position_risk_nonflat"


@pytest.mark.asyncio
async def test_close_new_resolved_filled_via_get_order(db_session: Any) -> None:
    """Close submit=NEW then GET=FILLED → close reconciled through filled, exit 0."""
    execution = _FakeFuturesExecution(
        open_submit_status="FILLED",
        close_submit_status="NEW",
        close_get_order_statuses=["FILLED"],
    )
    exit_code, ledger, _open_cid, close_cid = await _run_lifecycle(
        execution=execution, db_session=db_session
    )

    assert exit_code == 0
    close_row = await ledger.get_by_client_order_id(close_cid)
    assert close_row is not None
    assert close_row.filled_at is not None
    assert close_row.lifecycle_state == "reconciled"
    assert close_cid in execution.get_order_calls


@pytest.mark.asyncio
async def test_close_new_get_order_transient_error_then_filled(db_session: Any) -> None:
    """Close NEW, get_order 400s on the first poll then returns FILLED.

    Regression for the real demo-fapi smoke (ROB-305): a just-submitted order
    is not yet queryable, so ``GET /fapi/v1/order`` returns 400 on the
    immediate first poll. The bounded poll must tolerate the transient error
    and keep polling — not give up on the first exception — so the close
    proves FILLED and reconciles cleanly instead of falling to anomaly.
    """
    execution = _FakeFuturesExecution(
        open_submit_status="FILLED",
        close_submit_status="NEW",
        close_get_order_statuses=["FILLED"],
        close_get_order_raises=1,  # first poll 400s, then the queue applies
    )
    exit_code, ledger, _open_cid, close_cid = await _run_lifecycle(
        execution=execution, db_session=db_session
    )

    assert exit_code == 0
    close_row = await ledger.get_by_client_order_id(close_cid)
    assert close_row is not None
    assert close_row.lifecycle_state == "reconciled"
    assert close_row.filled_at is not None
    # The poll was retried after the transient error (>1 get_order call).
    assert execution.get_order_calls.count(close_cid) >= 2


@pytest.mark.asyncio
async def test_close_fill_unprovable_but_flat_records_anomaly(db_session: Any) -> None:
    """Close NEW, GET never FILLED, yet flat + 0 open orders → anomaly, exit 2.

    Section 4: a safe final account state must NOT be reported as clean
    success when the close fill cannot be proven.
    """
    execution = _FakeFuturesExecution(
        open_submit_status="FILLED",
        close_submit_status="NEW",
        close_get_order_statuses=["NEW", "NEW", "NEW", "NEW", "NEW"],
        position_after_close=Decimal("0"),  # account flat
    )
    exit_code, ledger, open_cid, close_cid = await _run_lifecycle(
        execution=execution, db_session=db_session
    )

    assert exit_code == 2
    close_row = await ledger.get_by_client_order_id(close_cid)
    assert close_row is not None
    assert close_row.lifecycle_state == "anomaly"
    assert close_row.anomaly_reason is not None
    # The open round-trip was genuinely proven, so it still reconciles.
    open_row = await ledger.get_by_client_order_id(open_cid)
    assert open_row is not None
    assert open_row.lifecycle_state == "reconciled"
    # Never advanced a submitted row straight to closed.
    assert close_row.closed_at is None
