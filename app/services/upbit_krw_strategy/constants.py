"""Pre-registered constants (design 5852). Changing one needs a new epoch (S6)."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

RULES_VERSION: Final = "upbit-krw-shadow-v1"

# --- universe (design §3) -------------------------------------------------
QUOTE_CURRENCY: Final = "KRW"
#: The static exclusion list is USD-quoted; Upbit has ``KRW-USDT`` (F10).
EXTRA_STABLE_BASES: Final = frozenset({"USDT"})
MIN_CLOSED_DAILY_BARS: Final = 90
MIN_LAST_CLOSE_KRW: Final = Decimal(100)
LIQUIDITY_WINDOW_DAYS: Final = 30
MIN_MEDIAN_QUOTE_VOLUME_KRW: Final = Decimal(5_000_000_000)
U0_SIZE: Final = 20

# --- signals (design §4) --------------------------------------------------
LOOKBACK_DAYS: Final = 60
RSI_PERIOD: Final = 14
PULLBACK_RSI_MAX: Final = Decimal(40)
MAX_POSITIONS_PER_ARM: Final = 4
#: Re-entry ban after a stop or a close at average -4.5% or worse.
REENTRY_BAN_DAYS: Final = 8
REENTRY_BAN_LOSS_FRACTION: Final = Decimal("0.045")

# --- sizing (design §5.3) -------------------------------------------------
LEG_NOTIONAL_CAP_KRW: Final = Decimal(480_000)
SIGMA_STAR: Final = Decimal("0.60")
EWMA_SPAN_DAYS: Final = 20
ANNUALISATION_DAYS: Final = 365
MIN_ENTRY_NOTIONAL_KRW: Final = Decimal(50_000)
MIN_ORDER_KRW: Final = Decimal(5_000)
QUANTITY_STEP: Final = Decimal("0.00000001")

# --- exits (design §5.4) --------------------------------------------------
HARD_STOP_FRACTION: Final = Decimal("0.10")
TIME_STOP_DECISION_SLOTS: Final = 30
#: (take-profit return, share of the filled quantity); last rung takes the rest.
TAKE_PROFIT_RUNGS: Final = (Decimal("0.02"), Decimal("0.04"), Decimal("0.06"))
TAKE_PROFIT_RUNG_COUNT: Final = len(TAKE_PROFIT_RUNGS)
#: One rung stays open for at most this many 12-hour renewals (5 days).
TAKE_PROFIT_MAX_RENEWALS: Final = 10
TAKE_PROFIT_RENEWAL_HOURS: Final = 12

# --- costs ----------------------------------------------------------------
FEE_RATE_PER_SIDE: Final = Decimal("0.0005")
