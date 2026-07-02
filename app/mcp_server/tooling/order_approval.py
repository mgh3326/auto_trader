# app/mcp_server/tooling/order_approval.py
"""ROB-653 P6-B — generic order approval-hash helpers for the shared
_place_order_impl path (kis_live KR/US + upbit crypto).

Reuses the pure P6-A primitives verbatim (app.mcp_server.tooling.toss_approval);
only the canonical payload builder is broker-generic. The shared P6 token
version/digest prefix are intentionally reused: the generic canonical key set
differs structurally from Toss's, so a token minted on one path fails the
canonical-equality check on the other (fail-closed, non-interchangeable).
"""

from __future__ import annotations

from typing import Any

from app.mcp_server.tooling.toss_approval import (
    APPROVAL_TTL_SECONDS,
    derive_approval_digest,
    derive_client_order_id,
    encode_approval_token,
    verify_approval_token,
)

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "build_order_canonical_payload",
    "salt_market_for",
    "derive_approval_digest",
    "derive_client_order_id",
    "encode_approval_token",
    "verify_approval_token",
]


def build_order_canonical_payload(
    *,
    market_type: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: str | None,
    price: str | None,
) -> dict[str, Any]:
    """Canonical order content shared by the dry-run preview and the live send.

    ``quantity``/``price`` must already be stringified post-normalization wire
    values (tick-snapped price, amount→quantity resolved) or ``None`` so preview
    and place derive an identical digest.
    """
    return {
        "market_type": market_type,
        "symbol": symbol,
        "side": side.upper(),
        "orderType": order_type.upper(),
        "quantity": quantity,
        "price": price,
    }


def salt_market_for(market_type: str) -> str:
    """Trading-day salt market: US equities settle on ET, everything else KST."""
    return "us" if market_type == "equity_us" else "kr"
