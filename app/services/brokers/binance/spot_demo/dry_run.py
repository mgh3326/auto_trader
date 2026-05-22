"""ROB-296 — Pure dry-run planning helpers for the Spot Demo smoke.

These functions produce source-labeled order plan templates **without
any HTTP, DB, or ledger side effects**. They exist so the smoke CLI can
demonstrate planning/filtering coverage even when:

  * the operator has not yet provisioned Spot Demo credentials, or
  * the operator wants to verify planning without spending a real
    preflight HTTP call.

What this module does NOT do:
  * Construct or call any httpx client.
  * Build a signed payload (the secret never flows in here).
  * Reach the ledger or any DB.
  * Submit an order.

Per ROB-296 §2 (ledger policy) and §6 (smoke path): persistent order
lifecycle is intentionally out of scope for this PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class SpotDemoPlannedOrder:
    """Source-labeled order plan template — no signed payload, no HTTP.

    The fields are deliberately a subset of what a full execution client
    would produce. There is no ``signed_payload_template`` here because
    this module never sees the api_secret; signing belongs at the
    transport boundary (``preflight``-style clients) and is deferred to
    follow-up work.
    """

    source: str  # "spot_demo"
    venue: str  # "binance"
    product: str  # "spot"
    symbol: str
    side: str  # "BUY" / "SELL"
    order_type: str  # "MARKET" / "LIMIT"
    quantity: Decimal
    price: Decimal | None
    notional_usdt: Decimal
    notional_cap_usdt: Decimal
    within_cap: bool

    def to_evidence_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "venue": self.venue,
            "product": self.product,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": str(self.quantity),
            "price": str(self.price) if self.price is not None else None,
            "notional_usdt": str(self.notional_usdt),
            "notional_cap_usdt": str(self.notional_cap_usdt),
            "within_cap": self.within_cap,
        }


ALLOWED_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})
ALLOWED_ORDER_TYPES: frozenset[str] = frozenset({"MARKET", "LIMIT"})


def plan_spot_demo_order(
    *,
    symbol: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Decimal | None,
    notional_cap_usdt: Decimal,
) -> SpotDemoPlannedOrder:
    """Build a source-labeled planned order without any side effect.

    Validates the shape of the request (sides, order types, LIMIT/MARKET
    price requirements) and computes whether the notional sits within the
    operator's configured cap. Does NOT raise on cap exceedance — the
    smoke CLI surfaces ``within_cap=False`` as evidence rather than
    crashing the dry-run.
    """
    if side not in ALLOWED_SIDES:
        raise ValueError(f"side {side!r} not in {sorted(ALLOWED_SIDES)}")
    if order_type not in ALLOWED_ORDER_TYPES:
        raise ValueError(
            f"order_type {order_type!r} not in {sorted(ALLOWED_ORDER_TYPES)}"
        )
    if order_type == "LIMIT" and price is None:
        raise ValueError("LIMIT order requires explicit price")
    if order_type == "MARKET" and price is not None:
        raise ValueError("MARKET order must not carry a price")
    notional = (price if price is not None else Decimal("0")) * quantity
    return SpotDemoPlannedOrder(
        source="spot_demo",
        venue="binance",
        product="spot",
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        notional_usdt=notional,
        notional_cap_usdt=notional_cap_usdt,
        within_cap=notional <= notional_cap_usdt,
    )
