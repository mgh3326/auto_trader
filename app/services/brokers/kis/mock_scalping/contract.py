"""ROB-321 PR3 — KIS mock scalping risk / order-intent contract.

Deterministic and read-only, mirroring the Binance demo_scalping contract but
KIS-specific:

* **Cash equities, long-only.** Entry signals are BUY-only; SELL exists only as
  a close/exit (handled by the exit manager in PR4), never as a short entry.
* **Notional in KRW.**
* The risk envelope (ROB-321 §1 mock scalping guards): symbol allowlist, max
  notional, max open positions, per-symbol cooldown, daily order/loss caps,
  spread + data-freshness gates.

``evaluate_risk`` is a pure function over value objects (no broker, no DB, no
network) returning every blocking reason code, not just the first. No order
mutation reachable from here; the durable ``LedgerSnapshot`` is read by PR4 from
the KIS mock shadow ledger and injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]

# Conservative default candidate universe — liquid KR large-caps. Operator may
# override via ScalpingRiskLimits(allowlist=...).
DEFAULT_ALLOWLIST: frozenset[str] = frozenset({"005930", "000660", "005380"})
MAX_NOTIONAL_KRW: Decimal = Decimal("100000")  # 10만원 per entry


class ReasonCode:
    """Stable string reason codes for signal/risk audit trails.

    Append-only — never rename an existing code (persisted to ledger/report
    metadata).
    """

    # Symbol gates
    SYMBOL_NOT_ALLOWLISTED = "symbol_not_allowlisted"
    # Market-condition gates
    SPREAD_TOO_WIDE = "spread_too_wide"
    STALE_DATA = "stale_data"
    # Sizing / notional
    NOTIONAL_ABOVE_CAP = "notional_above_cap"
    # Lifecycle / durable-state caps
    OPEN_POSITION_EXISTS = "open_position_exists"
    MAX_OPEN_POSITIONS_REACHED = "max_open_positions_reached"
    DAILY_ORDER_CAP_REACHED = "daily_order_cap_reached"
    DAILY_LOSS_BUDGET_EXHAUSTED = "daily_loss_budget_exhausted"
    COOLDOWN_ACTIVE = "cooldown_active"
    # Direction (cash market is long-only)
    SHORT_ENTRY_NOT_ALLOWED = "short_entry_not_allowed"
    # Signal outcomes
    ENTER_LONG_BREAKOUT = "enter_long_breakout"
    CHASE_TOO_FAR = "chase_too_far"
    NO_SIGNAL = "no_signal"
    INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class ScalpingRiskLimits:
    """The configured risk envelope. All defaults are conservative."""

    allowlist: frozenset[str] = DEFAULT_ALLOWLIST
    max_notional_krw: Decimal = MAX_NOTIONAL_KRW
    max_open_positions: int = 1
    daily_order_count_cap: int = 10
    daily_loss_budget_krw: Decimal = Decimal("50000")
    cooldown_seconds: int = 300
    max_spread_bps: Decimal = Decimal("30")  # 0.30%
    max_data_age_seconds: float = 60.0


@dataclass(frozen=True)
class LedgerSnapshot:
    """Durable lifecycle state read from the KIS mock shadow ledger (PR4).

    Computed from the DB, never held only in memory, so cooldown /
    single-position enforcement survives a fresh process or scheduler run.
    """

    has_open_position_for_symbol: bool
    open_position_count: int
    orders_today: int
    realized_loss_today_krw: Decimal
    seconds_since_last_close_for_symbol: float | None


@dataclass(frozen=True)
class MarketConditions:
    """Read-only market snapshot relevant to the risk gates."""

    spread_bps: Decimal
    data_age_seconds: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def evaluate_risk(
    *,
    symbol: str,
    side: Side,
    target_notional_krw: Decimal,
    limits: ScalpingRiskLimits,
    ledger: LedgerSnapshot,
    market: MarketConditions,
) -> RiskDecision:
    """Return every blocking reason for a candidate entry; empty == allowed.

    All checks accumulate (no short-circuit) so the observe-only record
    surfaces the full set of violations.
    """

    reasons: list[str] = []

    # --- Direction: cash market is long-only; SELL is never an entry. ---
    if side == "SELL":
        reasons.append(ReasonCode.SHORT_ENTRY_NOT_ALLOWED)

    # --- Symbol gate ---
    if symbol not in limits.allowlist:
        reasons.append(ReasonCode.SYMBOL_NOT_ALLOWLISTED)

    # --- Market-condition gates ---
    if market.spread_bps > limits.max_spread_bps:
        reasons.append(ReasonCode.SPREAD_TOO_WIDE)
    if market.data_age_seconds > limits.max_data_age_seconds:
        reasons.append(ReasonCode.STALE_DATA)

    # --- Notional cap (defense in depth; sizing also floors, never rounds up) ---
    if target_notional_krw > limits.max_notional_krw:
        reasons.append(ReasonCode.NOTIONAL_ABOVE_CAP)

    # --- Durable lifecycle / daily caps ---
    if ledger.has_open_position_for_symbol:
        reasons.append(ReasonCode.OPEN_POSITION_EXISTS)
    if ledger.open_position_count >= limits.max_open_positions:
        reasons.append(ReasonCode.MAX_OPEN_POSITIONS_REACHED)
    if ledger.orders_today >= limits.daily_order_count_cap:
        reasons.append(ReasonCode.DAILY_ORDER_CAP_REACHED)
    if ledger.realized_loss_today_krw >= limits.daily_loss_budget_krw:
        reasons.append(ReasonCode.DAILY_LOSS_BUDGET_EXHAUSTED)
    if (
        ledger.seconds_since_last_close_for_symbol is not None
        and ledger.seconds_since_last_close_for_symbol < limits.cooldown_seconds
    ):
        reasons.append(ReasonCode.COOLDOWN_ACTIVE)

    return RiskDecision(allowed=not reasons, reason_codes=tuple(reasons))
