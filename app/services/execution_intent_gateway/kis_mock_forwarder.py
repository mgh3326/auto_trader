"""ROB-1440 — forward child requests to the existing KIS mock order functions.

``is_mock=True`` is fixed here and no caller can change it. Every guard below the
gateway still applies: attribution, coordination route, follow-up capability,
ledger writes. If one of them refuses, the gateway records an unknown outcome and
does not send again.
"""

from __future__ import annotations

from typing import Any

from app.schemas.execution_intent import AmendChild, SubmitChild
from app.services.execution_intent_gateway.decisions import IntentSnapshot
from app.services.execution_intent_gateway.gateway import ForwardResult

_MARKET = "kr"


def _text_or_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


class KisMockForwarder:
    async def submit(
        self, intent: IntentSnapshot, request: SubmitChild
    ) -> ForwardResult:
        from app.mcp_server.tooling.order_execution import _place_order_impl

        result = await _place_order_impl(
            symbol=intent.symbol,
            side=intent.side,
            market=_MARKET,
            order_type="limit",
            quantity=float(request.quantity),
            price=float(request.limit_price_krw),
            dry_run=False,
            reason=f"intent_gateway:{intent.intent_id}",
            strategy=intent.strategy,
            correlation_id=f"intent-gateway:{request.idempotency_key}",
            is_mock=True,
        )
        return ForwardResult(
            accepted=result.get("success") is True,
            broker_order_id=_text_or_none(result.get("order_no")),
            result=result,
        )

    async def amend(
        self, intent: IntentSnapshot, request: AmendChild, *, broker_order_id: str
    ) -> ForwardResult:
        from app.mcp_server.tooling.orders_modify_cancel import modify_order_impl

        result = await modify_order_impl(
            order_id=broker_order_id,
            symbol=intent.symbol,
            market=_MARKET,
            new_price=float(request.limit_price_krw),
            new_quantity=float(request.quantity),
            dry_run=False,
            is_mock=True,
        )
        return ForwardResult(
            accepted=result.get("success") is True,
            broker_order_id=_text_or_none(result.get("new_order_id")),
            result=result,
        )

    async def cancel(
        self, intent: IntentSnapshot, *, broker_order_id: str
    ) -> ForwardResult:
        from app.mcp_server.tooling.orders_modify_cancel import cancel_order_impl

        result = await cancel_order_impl(
            order_id=broker_order_id,
            symbol=intent.symbol,
            market=_MARKET,
            is_mock=True,
        )
        return ForwardResult(
            accepted=result.get("success") is True,
            broker_order_id=broker_order_id,
            result=result,
        )
