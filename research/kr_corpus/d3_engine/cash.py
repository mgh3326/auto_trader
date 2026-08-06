"""T+2 orderable-cash ledger with payable and receivable audit trails."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Settlement:
    trade_session_index: int
    settle_session_index: int
    amount: Decimal
    kind: str


class CashLedger:
    """Track orderable cash; buy payable is reserved once and never double-debited."""

    def __init__(self, initial_settled_cash: Decimal) -> None:
        if initial_settled_cash < 0:
            raise ValueError("initial cash cannot be negative")
        self.orderable_cash = initial_settled_cash
        self.reserved_orders: dict[str, Decimal] = {}
        self.payables: list[Settlement] = []
        self.receivables: list[Settlement] = []

    def contribute(self, amount: Decimal) -> None:
        if amount < 0:
            raise ValueError("contribution cannot be negative")
        self.orderable_cash += amount

    def reserve_order(self, order_id: str, amount: Decimal) -> bool:
        if amount <= 0:
            raise ValueError("reservation must be positive")
        if order_id in self.reserved_orders:
            raise ValueError("duplicate cash reservation")
        if amount > self.orderable_cash:
            return False
        self.orderable_cash -= amount
        self.reserved_orders[order_id] = amount
        return True

    def expire_order(self, order_id: str) -> Decimal:
        amount = self.reserved_orders.pop(order_id)
        self.orderable_cash += amount
        return amount

    def fill_buy(
        self,
        *,
        order_id: str,
        actual_amount: Decimal,
        trade_session_index: int,
    ) -> Settlement:
        reserved = self.reserved_orders.pop(order_id)
        difference = reserved - actual_amount
        if difference < 0:
            raise AssertionError("buy fill exceeded cash reservation")
        self.orderable_cash += difference
        settlement = Settlement(
            trade_session_index,
            trade_session_index + 2,
            actual_amount,
            "payable",
        )
        self.payables.append(settlement)
        return settlement

    def fill_buy_immediate(
        self, *, amount: Decimal, trade_session_index: int
    ) -> Settlement:
        """Golden slice: reserve a filled buy without a prior submitted order."""

        if amount > self.orderable_cash:
            raise ValueError("insufficient orderable cash")
        self.orderable_cash -= amount
        settlement = Settlement(
            trade_session_index,
            trade_session_index + 2,
            amount,
            "payable",
        )
        self.payables.append(settlement)
        return settlement

    def fill_sell(self, *, net_amount: Decimal, trade_session_index: int) -> Settlement:
        if net_amount < 0:
            raise ValueError("sell net amount cannot be negative")
        settlement = Settlement(
            trade_session_index,
            trade_session_index + 2,
            net_amount,
            "receivable",
        )
        self.receivables.append(settlement)
        return settlement

    def settle_pre_open(self, session_index: int) -> dict[str, Decimal]:
        """Clear payable audit rows with zero cash delta; credit receivables once."""

        payable_amount = sum(
            (
                item.amount
                for item in self.payables
                if item.settle_session_index == session_index
            ),
            Decimal(0),
        )
        receivable_amount = sum(
            (
                item.amount
                for item in self.receivables
                if item.settle_session_index == session_index
            ),
            Decimal(0),
        )
        self.payables = [
            item for item in self.payables if item.settle_session_index != session_index
        ]
        self.receivables = [
            item
            for item in self.receivables
            if item.settle_session_index != session_index
        ]
        self.orderable_cash += receivable_amount
        return {
            "payable_cleared": payable_amount,
            "receivable_credited": receivable_amount,
            "cash_delta": receivable_amount,
        }
