"""Arm-local C1/C2/C3 state machines."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from research.kr_corpus.d3_engine.constants import (
    C1_FILLED_GROSS_CAP,
    C1_MAX_ADDS,
)
from research.kr_corpus.d3_engine.models import Position


@dataclass(slots=True)
class C1Cycle:
    filled_buy_gross: Decimal = Decimal("0")
    reserved_buy_gross: Decimal = Decimal("0")
    filled_add_count: int = 0
    reserved_add_count: int = 0

    def reserve(self, *, notional: Decimal, is_add: bool) -> tuple[bool, str | None]:
        if notional <= 0:
            raise ValueError("C1 notional must be positive")
        if is_add and self.filled_add_count + self.reserved_add_count >= C1_MAX_ADDS:
            return False, "max_adds_per_cycle"
        if (
            self.filled_buy_gross + self.reserved_buy_gross + notional
            > C1_FILLED_GROSS_CAP
        ):
            return False, "filled_notional_cap"
        self.reserved_buy_gross += notional
        if is_add:
            self.reserved_add_count += 1
        return True, None

    def expire(self, notional: Decimal, *, is_add: bool) -> None:
        self.reserved_buy_gross -= notional
        if is_add:
            self.reserved_add_count -= 1
        if self.reserved_buy_gross < 0:
            raise AssertionError("C1 reservation underflow")
        if self.reserved_add_count < 0:
            raise AssertionError("C1 add reservation underflow")

    def fill(
        self,
        *,
        notional: Decimal,
        is_add: bool,
        reserved_notional: Decimal | None = None,
    ) -> None:
        self.expire(
            reserved_notional if reserved_notional is not None else notional,
            is_add=is_add,
        )
        self.filled_buy_gross += notional
        if is_add:
            self.filled_add_count += 1
        if self.filled_buy_gross > C1_FILLED_GROSS_CAP:
            raise AssertionError("C1 filled cap exceeded")


def c2_allows(*, t_minus_1_close: Decimal | None, sma200: Decimal | None) -> bool:
    if t_minus_1_close is None or sma200 is None:
        return False
    return t_minus_1_close >= sma200


@dataclass(frozen=True, slots=True)
class C3CloseOutcome:
    underwater: bool
    streak: int
    armed_90: bool
    armed_180: bool


def update_c3_close(position: Position, *, close: Decimal) -> C3CloseOutcome:
    """Evaluate with the post-fill average; equality resets the streak."""

    if position.quantity == 0:
        position.underwater_streak = 0
        return C3CloseOutcome(False, 0, False, False)
    underwater = close < position.average_price
    position.underwater_streak = position.underwater_streak + 1 if underwater else 0
    armed_90 = False
    armed_180 = False
    if position.underwater_streak >= 90 and not position.trim90_triggered:
        position.trim90_triggered = True
        position.trim90_armed = True
        armed_90 = True
    if (
        c3_180_should_arm(
            streak=position.underwater_streak,
            trim90_filled=position.trim90_filled,
        )
        and not position.trim180_triggered
    ):
        position.trim180_triggered = True
        position.trim180_armed = True
        armed_180 = True
    return C3CloseOutcome(
        underwater,
        position.underwater_streak,
        armed_90,
        armed_180,
    )


def c3_buy_suppressed(position: Position) -> bool:
    return position.trim90_armed or position.trim180_armed


def c3_180_should_arm(*, streak: int, trim90_filled: bool) -> bool:
    return streak >= 180 and trim90_filled


def c3_trim_quantity(position: Position) -> int:
    return position.quantity // 3


def adjusted_simulation_quantity(quantity: int) -> int:
    """Adjusted-price simulation never restates integer shares without a ledger."""

    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    return quantity


def unresolved_terminal_status(
    *, data_ends_before_exploration_end: bool, position_quantity: int
) -> str:
    return (
        "INCONCLUSIVE_UNRESOLVED_TERMINAL"
        if data_ends_before_exploration_end and position_quantity > 0
        else "OK"
    )


def mark_c3_trim_filled(position: Position, *, stage: int) -> None:
    if stage == 90:
        position.trim90_armed = False
        position.trim90_filled = True
        return
    if stage == 180:
        if not position.trim90_filled:
            raise AssertionError("180 trim cannot fill before 90 trim")
        position.trim180_armed = False
        position.trim180_filled = True
        return
    raise ValueError("C3 trim stage must be 90 or 180")


def mark_c3_trim_skipped(position: Position, *, stage: int) -> None:
    if stage == 90:
        position.trim90_armed = False
        return
    if stage == 180:
        position.trim180_armed = False
        return
    raise ValueError("C3 trim stage must be 90 or 180")
