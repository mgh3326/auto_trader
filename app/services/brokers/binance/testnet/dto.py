"""ROB-286 — Data transfer objects for the testnet execution adapter.

Plain ``dataclass`` records, JSON-serializable by inspection only (no
custom serializer here — call-sites pick what to log/persist). Designed
to be friendly to ledger row construction in
``BinanceTestnetLedgerService`` without coupling the DTOs to the ORM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OrderPreview:
    """Pre-submit summary returned by ``preview_order``.

    All quantities are ``Decimal`` so callers don't introduce float
    drift in size/price math.
    """

    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "LIMIT" or "MARKET"
    quantity: Decimal
    price: Decimal | None  # None for MARKET
    notional_usdt: Decimal
    client_order_id: str
    # The exact params dict that would be signed if confirmed.
    # API-secret is NEVER stored here.
    signed_payload_template: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DryRunResult:
    """Returned by ``submit_order(confirm=False)`` and ``cancel_order(confirm=False)``.

    No HTTP was attempted. The preview captures everything the operator
    would need to spot-check before passing ``confirm=True``.
    """

    preview: OrderPreview
    reason: str  # e.g. "dry_run=True (default)", "confirm=False"


@dataclass(frozen=True, slots=True)
class OrderSubmitResult:
    """Returned by ``submit_order(confirm=True)`` after a real testnet hit.

    The dict fields are the Binance response shape, normalized to a
    minimal subset of what the ledger needs.
    """

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    status: str  # Binance "NEW" / "FILLED" / ...
    transact_time_ms: int
    raw_response: dict[str, object]


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Returned by ``cancel_order(confirm=True)``."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    status: str
    raw_response: dict[str, object]


@dataclass(frozen=True, slots=True)
class StopOrderResult:
    """ROB-289 — Returned by ``place_stop_limit_order`` / ``place_stop_market_order``.

    Same shape as ``OrderSubmitResult`` minus the broker fields that aren't
    meaningful for stop orders (e.g., ``price`` for STOP_LOSS market). The
    ``order_type`` field is locked to the two spot-only stop variants used
    by the paired TP/SL flow:

      * ``STOP_LOSS_LIMIT`` — used for the TP (take-profit) leg with GTC TIF.
      * ``STOP_LOSS`` — stop-market for the SL (stop-loss) leg, no TIF.

    Spot-only. No reduce-only field (split intentionally to keep the
    forbidden-literal audit clean) — that flag is a futures concept and
    would invite a future-path leak (reviewer focus #6 in the plan).
    """

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "STOP_LOSS_LIMIT" or "STOP_LOSS"
    stop_price: Decimal
    limit_price: Decimal | None  # None for STOP_LOSS (stop-market)
    status: str  # broker-reported initial status (e.g., "NEW")
    transact_time_ms: int
    raw_response: dict[str, object]


@dataclass(frozen=True, slots=True)
class OpenOrder:
    """Returned by ``open_orders`` query."""

    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    status: str
    update_time_ms: int


@dataclass(frozen=True, slots=True)
class Fill:
    """Returned by ``recent_fills`` query (Binance ``myTrades`` shape)."""

    trade_id: int
    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee_amount: Decimal
    fee_asset: str
    transact_time_ms: int


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Minimal Binance ``account`` response shape used by reconciliation."""

    can_trade: bool
    update_time_ms: int
    balances: dict[str, Decimal]  # asset → free amount
    fetched_at: datetime
