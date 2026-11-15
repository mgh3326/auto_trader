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

DEMO_URL: Final = "https://demo-fapi.binance.com"

# Operator decision hk:doc decision/2026-11-15/at-3796-3797-design-answers.
# Volatility-targeted sizing: notional = cap * min(1, SIGMA_STAR / sigma_hat).
SIGMA_STAR: Final = Decimal("0.60")
EWMA_SPAN_DAYS: Final = 20
ANNUALISATION_DAYS: Final = 365
# The lookback ``L`` is a required strategy argument (CT-5 fixes it at seal).
LOOKBACK_CHOICES: Final = (20, 60, 120)
TIME_EXIT_DAYS: Final = 30
# A 1m hard-stop watch covers at most this much history after a restart; older
# touches are covered only by the current executable quote and the 4h decision.
HARD_STOP_WATCH_HOURS: Final = 24

# Closed exit-reason vocabulary. ``TrendStateService`` validates a reason against
# this tuple before any write; ``entry_gate`` counts hard stops with the same
# constant. There is deliberately no DB CHECK for it in this PR.
EXIT_HARD_STOP: Final = "hard_stop"
EXIT_SIGNAL_FLIP: Final = "signal_flip"
EXIT_TIME_EXIT: Final = "time_exit"
EXIT_REASONS: Final = (EXIT_HARD_STOP, EXIT_SIGNAL_FLIP, EXIT_TIME_EXIT)


class UnknownExitReason(ValueError):
    """An exit reason outside :data:`EXIT_REASONS`."""


def assert_exit_reason(reason: object) -> str:
    if not isinstance(reason, str) or reason not in EXIT_REASONS:
        raise UnknownExitReason(f"exit reason {reason!r} not in {EXIT_REASONS!r}")
    return reason


def assert_trend_demo_url(base_url: str) -> None:
    """Exact origin: scheme, host and no decorations."""
    if base_url != DEMO_URL:
        raise ValueError("trend requires the exact Futures Demo origin")


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
