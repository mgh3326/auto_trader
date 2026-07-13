"""ROB-307 PR1 — Binance Demo scalping risk / order-intent contract.

Deterministic and read-only. This module defines:

* the **risk envelope** locked by ROB-307 §5 (allowlist, 10 USDT notional
  cap, spot long-only, per-symbol + global + daily-count + daily-loss
  caps, spread/freshness/cooldown gates), and
* ``evaluate_risk`` — a pure function over value objects (no broker, no
  DB, no network) returning every blocking reason code, not just the
  first, so the observe-only record is fully auditable.

The allowlist mirrors ``futures_demo.sizing`` (XRP/DOGE/SOL; ``BTCUSDT``
excluded because its Futures MIN_NOTIONAL=50 exceeds the 10 USDT cap).
No LLM, no live endpoints, no order mutation reachable from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

Product = Literal["spot", "usdm_futures"]
Side = Literal["BUY", "SELL"]

# Locked §5 constants.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset({"XRPUSDT", "DOGEUSDT", "SOLUSDT"})
EXCLUDED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT"})
MAX_NOTIONAL_USDT: Decimal = Decimal("10")

# ROB-313 D3 — fee-rate used to *estimate* per-leg fees for cost analytics.
# Demo's "exact" commission is not real-VIP/BNB-accurate, so a single
# shared rate (also consumed by the future backtester) is more useful than
# chasing an idealized demo fee. Conservative USD-M taker default.
DEMO_SCALPING_FEE_RATE_BPS: Decimal = Decimal("5")


class ReasonCode:
    """Stable string reason codes for signal/risk audit trails.

    Plain string constants (repo convention favours ``Literal``/strings
    over ``StrEnum``). Persisted into ledger/report metadata, so values
    are append-only — never rename an existing code.
    """

    # Symbol gates
    SYMBOL_NOT_ALLOWLISTED = "symbol_not_allowlisted"
    SYMBOL_EXCLUDED = "symbol_excluded"
    # Market-condition gates
    SPREAD_TOO_WIDE = "spread_too_wide"
    STALE_DATA = "stale_data"
    # Fail-close when no trustworthy server-observed market snapshot exists
    # (provider failure / empty|malformed kline / missing timestamp / bad quote).
    # ROB-841: replaces the old 0/0 synthesis that silently disarmed the gates.
    MARKET_CONDITIONS_UNAVAILABLE = "market_conditions_unavailable"
    # Sizing / notional
    NOTIONAL_ABOVE_CAP = "notional_above_cap"
    NOTIONAL_BELOW_MIN = "notional_below_min"
    # Lifecycle / durable-state caps (§4 + §5)
    OPEN_LIFECYCLE_EXISTS = "open_lifecycle_exists"
    GLOBAL_LIFECYCLE_CAP_REACHED = "global_lifecycle_cap_reached"
    # ROB-844: the authoritative atomic root reservation lost the race — another
    # process (TaskIQ / MCP / websocket) claimed the exposure slot first. Blocks
    # the open with ZERO broker submit. Distinct from the advisory read-side
    # gates above (which fire pre-reservation on a possibly-stale snapshot).
    EXPOSURE_SLOT_TAKEN = "exposure_slot_taken"
    DAILY_ORDER_CAP_REACHED = "daily_order_cap_reached"
    DAILY_LOSS_BUDGET_EXHAUSTED = "daily_loss_budget_exhausted"
    COOLDOWN_ACTIVE = "cooldown_active"
    # Direction
    SPOT_SELL_WITHOUT_HOLDING = "spot_sell_without_holding"
    # Signal outcomes
    ENTER_LONG_BREAKOUT = "enter_long_breakout"
    ENTER_SHORT_BREAKDOWN = "enter_short_breakdown"
    NO_SIGNAL = "no_signal"
    INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class ScalpingRiskLimits:
    """The configured risk envelope. All defaults are conservative."""

    allowlist: frozenset[str] = DEFAULT_ALLOWLIST
    excluded: frozenset[str] = EXCLUDED_SYMBOLS
    max_notional_usdt: Decimal = MAX_NOTIONAL_USDT
    global_open_lifecycle_cap: int = 1
    daily_order_count_cap: int = 10
    daily_loss_budget_usdt: Decimal = Decimal("5")
    cooldown_seconds: int = 300
    max_spread_bps: Decimal = Decimal("20")  # 0.20%
    # 2× the 1m candle interval: a healthy in-progress candle ages 0..60s;
    # a feed stalled by ≥2 candles trips STALE_DATA.
    max_data_age_seconds: float = 120.0


@dataclass(frozen=True)
class LedgerSnapshot:
    """Durable lifecycle state read from ``binance_demo_order_ledger`` (§4).

    Computed by ``ledger_state`` from the DB, never held only in memory,
    so cooldown / single-lifecycle enforcement survives a fresh process
    or scheduler run.
    """

    has_open_lifecycle_for_symbol: bool
    global_open_lifecycle_count: int
    orders_today: int
    realized_loss_today_usdt: Decimal
    seconds_since_last_close_for_symbol: float | None


@dataclass(frozen=True)
class MarketConditions:
    """Read-only market snapshot relevant to the risk gates."""

    spread_bps: Decimal
    data_age_seconds: float
    spot_free_base_qty: Decimal  # held base asset; gates spot long-only SELL


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def evaluate_risk(
    *,
    product: Product,
    symbol: str,
    side: Side,
    target_notional_usdt: Decimal,
    limits: ScalpingRiskLimits,
    ledger: LedgerSnapshot,
    market: MarketConditions,
) -> RiskDecision:
    """Return every blocking reason for a candidate entry; empty == allowed.

    All checks accumulate (no short-circuit) so the observe-only record
    surfaces the full set of violations.
    """

    reasons: list[str] = []

    # --- Symbol gates ---
    if symbol in limits.excluded:
        reasons.append(ReasonCode.SYMBOL_EXCLUDED)
    elif symbol not in limits.allowlist:
        reasons.append(ReasonCode.SYMBOL_NOT_ALLOWLISTED)

    # --- Market-condition gates ---
    if market.spread_bps > limits.max_spread_bps:
        reasons.append(ReasonCode.SPREAD_TOO_WIDE)
    if market.data_age_seconds > limits.max_data_age_seconds:
        reasons.append(ReasonCode.STALE_DATA)

    # --- Notional cap (defense in depth; sizing also floors, never rounds up) ---
    if target_notional_usdt > limits.max_notional_usdt:
        reasons.append(ReasonCode.NOTIONAL_ABOVE_CAP)

    # --- Direction: spot is long-only (SELL only closes/reduces a holding) ---
    if product == "spot" and side == "SELL" and market.spot_free_base_qty <= 0:
        reasons.append(ReasonCode.SPOT_SELL_WITHOUT_HOLDING)

    # --- Durable lifecycle / daily caps (§4 + §5) ---
    if ledger.has_open_lifecycle_for_symbol:
        reasons.append(ReasonCode.OPEN_LIFECYCLE_EXISTS)
    if ledger.global_open_lifecycle_count >= limits.global_open_lifecycle_cap:
        reasons.append(ReasonCode.GLOBAL_LIFECYCLE_CAP_REACHED)
    if ledger.orders_today >= limits.daily_order_count_cap:
        reasons.append(ReasonCode.DAILY_ORDER_CAP_REACHED)
    if ledger.realized_loss_today_usdt >= limits.daily_loss_budget_usdt:
        reasons.append(ReasonCode.DAILY_LOSS_BUDGET_EXHAUSTED)
    if (
        ledger.seconds_since_last_close_for_symbol is not None
        and ledger.seconds_since_last_close_for_symbol < limits.cooldown_seconds
    ):
        reasons.append(ReasonCode.COOLDOWN_ACTIVE)

    return RiskDecision(allowed=not reasons, reason_codes=tuple(reasons))
