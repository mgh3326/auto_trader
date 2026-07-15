"""ROB-298 — DTOs for Spot Demo execution backend responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SpotDemoOrderSubmitResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    qty: Decimal
    executed_qty: Decimal
    cummulative_quote_qty: Decimal
    status: str  # FILLED / PARTIALLY_FILLED / NEW / ...
    fee_usdt: Decimal | None = None
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpotDemoOrderTestResult:
    """`/api/v3/order/test` returned 200 with an empty body (success)."""

    symbol: str
    side: str
    order_type: str
    qty: Decimal


@dataclass(frozen=True)
class SpotDemoCancelResult:
    client_order_id: str
    broker_order_id: str
    symbol: str
    status: str
    raw_response_redacted: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpotDemoOpenOrder:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: str
    qty: Decimal
    status: str


@dataclass(frozen=True)
class SpotDemoOpenOrdersResult:
    orders: list[SpotDemoOpenOrder]


@dataclass(frozen=True)
class SpotDemoAssetBalance:
    """Free/locked amounts for a SINGLE asset.

    Deliberately narrow: ``get_asset_balance`` returns only the one asset
    the caller asked about so the full account payload (every balance row +
    account-level flags) never enters logs or evidence. ``free`` is the
    amount sellable right now (post-commission); ``locked`` is reserved by
    open orders.
    """

    asset: str
    free: Decimal
    locked: Decimal
