"""Deterministic correlation-id spine for the paper learning loop (ROB-705).

Mirrors ROB-653 P6-B idempotency keying: the canonical string includes the KST
trade-day and a rung discriminator so a re-placed order (after cancel) or two
identical ladder rungs do NOT collide on one id. Collision would be silent ---
review.trade_retrospectives.correlation_id is UNIQUE and pending-coverage
dedups on it, so one retrospective would "cover" two distinct orders.
Pure: no I/O, no LLM.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal


def paper_correlation_id(
    *,
    account_id: int,
    symbol: str,
    side: str,
    limit_price: Decimal,
    quantity: Decimal,
    kst_trade_day: str,
    rung: int = 0,
) -> str:
    # canonical decision fields | KST trade-day | rung  (ROB-653 P6-B shape)
    canonical = "|".join(
        (
            symbol.upper(),
            side.lower(),
            format(limit_price, "f"),
            format(quantity, "f"),
            kst_trade_day,
            str(rung),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"paper:{account_id}:{digest}"
