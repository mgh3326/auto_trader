"""Pure close-based rules shared by retrospective replay and shadow forward runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

B3_ADDS = (0.10, 0.20)
B3_TARGET = 0.10
B3_STOP_AFTER_ADDS = 0.35
US_ADD_DRAWDOWN = 0.08
US_ADD_FRACTION = 0.5
TOUCH_TOLERANCE = 1e-9


def next_open_day(market: str, stamp: datetime) -> date:
    """Earliest possible entry date; callers resolve holidays on their calendar."""
    zones = {"crypto": "UTC", "us": "America/New_York", "kr": "Asia/Seoul"}
    opens = {"us": time(9, 30), "kr": time(9)}
    local = stamp.astimezone(ZoneInfo(zones[market]))
    day = local.date()
    if market == "crypto" or local.time() >= opens[market]:
        day += timedelta(days=1)
    return day


@dataclass(frozen=True)
class AveragingDecision:
    adds: int
    exit_reason: str | None


def averaging_decision(
    entry: float,
    unit_prices: list[float],
    close: float,
    *,
    adds: tuple[float, ...] = B3_ADDS,
    target: float = B3_TARGET,
    stop_after_adds: float | None = B3_STOP_AFTER_ADDS,
) -> AveragingDecision:
    """Equal notional units, preserving the pre-refactor replay's float boundaries."""
    prices = list(unit_prices)
    added = 0
    while len(prices) - 1 < len(adds):
        if close > entry * (1.0 - adds[len(prices) - 1]):
            break
        prices.append(close)
        added += 1
    avg = len(prices) / sum(1.0 / price for price in prices)
    reason = None
    if close >= avg * (1.0 + target):
        reason = "target"
    elif (
        stop_after_adds
        and len(prices) - 1 == len(adds)
        and close <= avg * (1.0 - stop_after_adds)
    ):
        reason = "loss-cut"
    return AveragingDecision(added, reason)


def add_trigger(unrealized, threshold: float = US_ADD_DRAWDOWN):
    """Scalar or pandas-series comparison, including an exact -8% boundary."""
    return unrealized <= -threshold + TOUCH_TOLERANCE


def add_quantity(held, fraction: float = US_ADD_FRACTION):
    return held * type(held)(str(fraction))
