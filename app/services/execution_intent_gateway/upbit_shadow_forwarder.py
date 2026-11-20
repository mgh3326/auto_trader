"""hk 5441 — the shadow forwarder: it records and never sends (design I6).

This module imports no broker, order, ledger or network code, directly or
through anything it imports; ``test_upbit_static_boundaries`` checks its whole
import closure. A shadow intent can only be given to this class, so a shadow
decision cannot reach an exchange whatever else is misconfigured.

A shadowed submit is treated downstream as filled in full at its limit price
when the journal recorded it (design §3.6). That is an assumption for path
testing, not a fill.
"""

from __future__ import annotations

from typing import Any

from app.services.execution_intent_gateway.forward_result import ForwardResult


class UpbitShadowForwarder:
    async def submit(self, intent: Any, request: Any) -> ForwardResult:
        return ForwardResult(
            accepted=False,
            broker_order_id=None,
            result={
                "shadow": True,
                "sent": False,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity_decimal": request.quantity_decimal,
                "limit_price_krw": request.limit_price_krw,
            },
            shadowed=True,
        )
