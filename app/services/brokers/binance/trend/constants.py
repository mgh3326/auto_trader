"""Locked TREND-TSMOM-v1 envelope constants (operator decision hk 3796/3797).

The values are hard safety invariants of this lane, not operator-tunable dials.
``LOCKED_LIMITS`` is the single allowed value and ``assert_limits_locked`` fails
closed before any network or DB use if a caller supplies anything else (the
ROB-993 ``kill_switch.LOCKED_LIMITS`` idiom). There is deliberately no CLI flag,
env var or setting that can change them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

IDENTITY: Final = "TREND-TSMOM-v1"

# The correlation id prefix and client order id prefix are fixed per identity.
CORRELATION_PREFIX: Final = "binance-trend:"
CLIENT_ORDER_PREFIX: Final = "trend-"


@dataclass(frozen=True)
class TrendLimits:
    universe: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
    leg_notional_cap_usdt: Decimal = Decimal("60")
    max_concurrent_positions: int = 4
    max_gross_notional_usdt: Decimal = Decimal("240")
    max_consecutive_hard_stops_per_utc_day: int = 2
    max_conflicts_per_utc_week: int = 3
    # Required by readback only; this package never sets leverage or margin mode.
    required_leverage: int = 1


LOCKED_LIMITS: Final[TrendLimits] = TrendLimits()


class TrendLimitsNotLocked(ValueError):
    """Supplied limits deviate from :data:`LOCKED_LIMITS`."""


def assert_limits_locked(limits: TrendLimits) -> None:
    """Fail closed unless ``limits`` is exactly the locked lane invariant."""
    if not isinstance(limits, TrendLimits) or limits != LOCKED_LIMITS:
        raise TrendLimitsNotLocked(
            f"trend limits {limits!r} deviate from the locked lane invariant "
            f"{LOCKED_LIMITS!r} — this lane has no operator-tunable safety caps"
        )
