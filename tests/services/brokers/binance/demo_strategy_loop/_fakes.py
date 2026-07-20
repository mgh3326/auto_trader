"""ROB-993 adversarial-review regression fakes (verify-993-2256.md).

A controllable fake ``BinanceFuturesDemoExecutionClient`` and a
controllable fake ledger, so ``execution.execute_signal_round_trip`` can be
exercised — including deliberately mutated broker responses — without any
real HTTP or DB. Not a pytest test module itself (no ``test_`` prefix).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoLeverageResult,
    FuturesDemoOpenOrder,
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


@dataclass
class FakeExecutionClient:
    """Records every call (name + kwargs) and returns caller-configured
    canned/mutated responses. ``position_amt_by_symbol`` is the live
    broker-side position the fake maintains; ``submit_mutations`` /
    ``get_order_mutations`` let a test corrupt a single response field to
    simulate broker echo tampering."""

    position_amt_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    open_orders_by_symbol: dict[str, list[FuturesDemoOpenOrder]] = field(
        default_factory=dict
    )
    is_hedge_mode: bool = False
    submit_status: str = "FILLED"
    get_order_status: str = "FILLED"
    submit_mutations: dict[str, Any] = field(default_factory=dict)
    get_order_mutations: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Shared with FakeLedger (pass the SAME list to both) so a test can
    # assert on the interleaved exec-call / ledger-transition order.
    event_log: list[tuple[str, ...]] = field(default_factory=list)

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        self.event_log.append(("exec", name))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def get_position_mode(self) -> FuturesDemoPositionModeResult:
        self._record("get_position_mode")
        return FuturesDemoPositionModeResult(is_hedge_mode=self.is_hedge_mode)

    async def set_leverage(
        self, *, symbol: str, leverage: int
    ) -> FuturesDemoLeverageResult:
        self._record("set_leverage", symbol=symbol, leverage=leverage)
        return FuturesDemoLeverageResult(
            symbol=symbol, leverage=leverage, max_notional_value=Decimal("0")
        )

    def preview_submit(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Decimal,
        client_order_id: str | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
    ) -> FuturesDemoDryRunResult:
        self._record("preview_submit", symbol=symbol, side=side, qty=qty)
        return FuturesDemoDryRunResult(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            client_order_id=client_order_id or "preview",
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
        self._record(
            "order_test", symbol=symbol, side=side, qty=qty, reduce_only=reduce_only
        )
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
        client_order_id: str | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        reduce_only: bool = False,
        confirm: bool = False,
    ) -> FuturesDemoOrderSubmitResult:
        self._record(
            "submit_order",
            symbol=symbol,
            side=side,
            qty=qty,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
        )
        # Move the fake broker's live position on a successful (non-mutated) fill.
        current = self.position_amt_by_symbol.get(symbol, Decimal("0"))
        delta = qty if side == "BUY" else -qty
        if self.submit_status == "FILLED":
            self.position_amt_by_symbol[symbol] = current + delta

        payload: dict[str, Any] = {
            "client_order_id": client_order_id or "cid",
            "broker_order_id": f"broker-{client_order_id}",
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "qty": qty,
            "executed_qty": qty,
            "avg_price": Decimal("1"),
            "status": self.submit_status,
            "reduce_only": reduce_only,
        }
        payload.update(self.submit_mutations)
        return FuturesDemoOrderSubmitResult(**payload)

    async def get_order(
        self, *, symbol: str, client_order_id: str
    ) -> FuturesDemoOrderStatusResult:
        self._record("get_order", symbol=symbol, client_order_id=client_order_id)
        payload: dict[str, Any] = {
            "client_order_id": client_order_id,
            "broker_order_id": f"broker-{client_order_id}",
            "symbol": symbol,
            "side": "BUY",
            "order_type": "MARKET",
            "status": self.get_order_status,
            "orig_qty": Decimal("0"),
            "executed_qty": Decimal("0"),
            "avg_price": Decimal("1"),
            "reduce_only": False,
        }
        payload.update(self.get_order_mutations)
        return FuturesDemoOrderStatusResult(**payload)

    async def get_position(self, *, symbol: str) -> FuturesDemoPositionResult:
        self._record("get_position", symbol=symbol)
        amt = self.position_amt_by_symbol.get(symbol, Decimal("0"))
        return FuturesDemoPositionResult(
            symbol=symbol,
            position_amt=amt,
            entry_price=Decimal("1"),
            leverage=1,
            is_flat=(amt == 0),
        )

    async def get_open_orders(self, *, symbol: str) -> FuturesDemoOpenOrdersResult:
        self._record("get_open_orders", symbol=symbol)
        return FuturesDemoOpenOrdersResult(
            orders=self.open_orders_by_symbol.get(symbol, [])
        )

    async def get_all_positions(self) -> list[FuturesDemoPositionResult]:
        self._record("get_all_positions")
        return [
            FuturesDemoPositionResult(
                symbol=symbol,
                position_amt=amt,
                entry_price=Decimal("1"),
                leverage=1,
                is_flat=(amt == 0),
            )
            for symbol, amt in self.position_amt_by_symbol.items()
        ]

    async def get_all_open_orders(self) -> FuturesDemoOpenOrdersResult:
        self._record("get_all_open_orders")
        all_orders = [
            order for orders in self.open_orders_by_symbol.values() for order in orders
        ]
        return FuturesDemoOpenOrdersResult(orders=all_orders)


@dataclass
class FakeLedgerRow:
    client_order_id: str
    lifecycle_state: str
    parent_client_order_id: str | None = None
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeLedger:
    """Minimal ledger double: tracks lifecycle_state per client_order_id
    without the real state-machine/DB, so tests can assert exactly which
    state a root/child row ends in."""

    rows: dict[str, FakeLedgerRow] = field(default_factory=dict)
    global_open_root_cap_seen: list[int] = field(default_factory=list)
    # Shared with FakeExecutionClient (pass the SAME list to both).
    event_log: list[tuple[str, ...]] = field(default_factory=list)

    async def reserve_root_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        notional_usdt: Decimal | None = None,
        extra_metadata: dict[str, Any] | None = None,
        global_open_root_cap: int,
        now: dt.datetime,
        **kwargs: Any,
    ):
        self.global_open_root_cap_seen.append(global_open_root_cap)
        self.rows[client_order_id] = FakeLedgerRow(
            client_order_id=client_order_id,
            lifecycle_state="planned",
            extra_metadata=dict(extra_metadata or {}),
        )
        self.event_log.append(("ledger", client_order_id, "planned"))

        @dataclass
        class _Reservation:
            status: str = "reserved"
            reason: str | None = None

        return _Reservation()

    async def record_planned(
        self,
        *,
        client_order_id: str,
        parent_client_order_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FakeLedgerRow:
        row = FakeLedgerRow(
            client_order_id=client_order_id,
            lifecycle_state="planned",
            parent_client_order_id=parent_client_order_id,
            extra_metadata=dict(extra_metadata or {}),
        )
        self.rows[client_order_id] = row
        self.event_log.append(("ledger", client_order_id, "planned"))
        return row

    async def _transition(
        self, client_order_id: str, new_state: str, *, extra_metadata_merge=None, **_
    ) -> FakeLedgerRow:
        row = self.rows[client_order_id]
        row.lifecycle_state = new_state
        if extra_metadata_merge:
            row.extra_metadata.update(extra_metadata_merge)
        self.event_log.append(("ledger", client_order_id, new_state))
        return row

    async def record_previewed(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "previewed", **kwargs)

    async def record_validated(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "validated", **kwargs)

    async def record_submitted(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "submitted", **kwargs)

    async def record_filled(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "filled", **kwargs)

    async def record_closed(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "closed", **kwargs)

    async def record_cancelled(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "cancelled", **kwargs)

    async def record_reconciled(self, *, client_order_id: str, **kwargs: Any):
        return await self._transition(client_order_id, "reconciled", **kwargs)

    async def record_anomaly(self, *, client_order_id: str, reason: str, **kwargs: Any):
        kwargs.setdefault("extra_metadata_merge", {})
        kwargs["extra_metadata_merge"] = {
            **kwargs["extra_metadata_merge"],
            "anomaly_reason": reason,
        }
        return await self._transition(client_order_id, "anomaly", **kwargs)


class FakeSession:
    async def commit(self) -> None:
        return None
