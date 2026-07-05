"""Deterministic correlation-id spine for the LIVE learning loop (ROB-714).

Mirrors app.services.paper_correlation (ROB-705). The canonical string includes
the KST trade-day and a rung discriminator so a re-placed order (after cancel)
or two identical ladder rungs do NOT collide on one id. account_scope namespaces
the id per ledger (kis_live / upbit_live / toss_live). Pure: no I/O, no LLM.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal


def live_correlation_id(
    *,
    account_scope: str,
    symbol: str,
    side: str,
    price: Decimal,
    quantity: Decimal,
    kst_trade_day: str,
    rung: int = 0,
) -> str:
    canonical = "|".join(
        (
            account_scope.lower(),
            symbol.upper(),
            side.lower(),
            format(price, "f"),
            format(quantity, "f"),
            kst_trade_day,
            str(rung),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"live:{account_scope.lower()}:{digest}"
