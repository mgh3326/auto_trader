"""Typed inputs and state for the D3 research engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from research.kr_corpus.d3_engine.constants import (
    FEE_RATE,
    INITIAL_CASH,
    MONTHLY_CONTRIBUTION,
    ORDER_NOTIONAL,
)


class Arm(StrEnum):
    B0 = "B0"
    C1 = "C1"
    C2 = "C2"
    C3 = "C3"


class CashflowView(StrEnum):
    WITH_CONTRIBUTION = "with_contribution"
    NO_CONTRIBUTION = "no_contribution"


class DataView(StrEnum):
    ORIGINAL_VALID_BAR = "original_valid_bar"
    CLAMP_ADMIT_V1 = "clamp_admit_v1"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderClass(StrEnum):
    ADD = "add"
    NEW = "new"
    RESISTANCE_TRIM = "resistance_trim"
    TIME_TRIM = "time_trim"


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    EXPIRED = "expired"
    POLICY_REJECTED = "policy_rejected"
    CASH_REJECTED = "cash_rejected"


@dataclass(frozen=True, slots=True)
class Bar:
    session: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol must be non-empty")
        prices = (self.open, self.high, self.low, self.close)
        if any(not price.is_finite() for price in prices) or min(prices) <= 0:
            raise ValueError("bar prices must be finite positive Decimals")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise ValueError("bar OHLC ordering is invalid")


@dataclass(frozen=True, slots=True)
class CorporateAction:
    session: date
    symbol: str
    kind: str
    split_factor: Decimal | None = None
    data_ends_before_exploration_end: bool = False

    def __post_init__(self) -> None:
        if not self.symbol or not self.kind:
            raise ValueError("corporate action symbol/kind must be non-empty")
        if self.split_factor is not None and (
            not self.split_factor.is_finite() or self.split_factor <= 0
        ):
            raise ValueError("split_factor must be finite positive when supplied")


@dataclass(frozen=True, slots=True)
class EngineConfig:
    initial_cash: Decimal = INITIAL_CASH
    monthly_contribution: Decimal = MONTHLY_CONTRIBUTION
    order_notional: Decimal = ORDER_NOTIONAL
    fee_rate: Decimal = FEE_RATE
    max_new_entries_per_session: int = 3

    def __post_init__(self) -> None:
        if not self.initial_cash.is_finite() or self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if not self.monthly_contribution.is_finite() or self.monthly_contribution < 0:
            raise ValueError("monthly_contribution must be non-negative")
        if not self.order_notional.is_finite() or self.order_notional <= 0:
            raise ValueError("order_notional must be positive")
        if not self.fee_rate.is_finite() or self.fee_rate < 0:
            raise ValueError("fee_rate must be non-negative")
        if self.max_new_entries_per_session < 1:
            raise ValueError("max_new_entries_per_session must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioRunInput:
    arm: Arm
    cashflow_view: CashflowView
    bars: tuple[Bar, ...]
    data_view: DataView = DataView.ORIGINAL_VALID_BAR
    market_sessions: tuple[date, ...] = ()
    index_closes: tuple[tuple[date, Decimal], ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    decision_start: date | None = None
    config: EngineConfig = EngineConfig()


@dataclass(slots=True)
class Order:
    order_id: str
    session: date
    symbol: str
    side: OrderSide
    order_class: OrderClass
    limit: Decimal
    quantity: int
    rung: str
    rank: int
    status: OrderStatus = OrderStatus.SUBMITTED
    fill_price: Decimal | None = None

    @property
    def gross_limit_notional(self) -> Decimal:
        return self.limit * self.quantity


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    session: date
    symbol: str
    side: OrderSide
    order_class: OrderClass
    quantity: int
    price: Decimal
    gross: Decimal
    fee: Decimal


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: int = 0
    average_price: Decimal = Decimal("0")
    invested_cost_basis: Decimal = Decimal("0")
    cycle_first_fill_index: int | None = None
    underwater_streak: int = 0
    trim90_triggered: bool = False
    trim90_armed: bool = False
    trim90_filled: bool = False
    trim180_triggered: bool = False
    trim180_armed: bool = False
    trim180_filled: bool = False

    def apply_buy(
        self,
        *,
        quantity: int,
        price: Decimal,
        fee: Decimal,
        session_index: int,
    ) -> None:
        if quantity < 1:
            raise ValueError("buy quantity must be positive")
        old_gross = self.average_price * self.quantity
        new_gross = price * quantity
        if self.quantity == 0:
            self.cycle_first_fill_index = session_index
        self.quantity += quantity
        self.average_price = (old_gross + new_gross) / self.quantity
        self.invested_cost_basis += new_gross + fee

    def apply_sell(self, *, quantity: int) -> None:
        if quantity < 1 or quantity > self.quantity:
            raise ValueError("sell quantity outside position")
        fraction = Decimal(quantity) / Decimal(self.quantity)
        self.invested_cost_basis *= Decimal(1) - fraction
        self.quantity -= quantity
        if self.quantity == 0:
            self.average_price = Decimal("0")
            self.invested_cost_basis = Decimal("0")
            self.cycle_first_fill_index = None
            self.underwater_streak = 0
            self.trim90_triggered = False
            self.trim90_armed = False
            self.trim90_filled = False
            self.trim180_triggered = False
            self.trim180_armed = False
            self.trim180_filled = False


@dataclass(frozen=True, slots=True)
class EngineResult:
    arm: Arm
    cashflow_view: CashflowView
    data_view: DataView
    events: tuple[dict[str, object], ...]
    fills: tuple[Fill, ...]
    terminal_positions: tuple[dict[str, object], ...]
    metrics: dict[str, object]
    evidence: dict[str, object]
    status: str = "OK"


@dataclass(slots=True)
class RunState:
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[Order] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
