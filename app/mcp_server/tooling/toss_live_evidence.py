from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.services.brokers.toss import TossReadClient


@dataclass(frozen=True)
class TossFillEvidence:
    verdict: str
    local_status: str
    broker_status: str
    filled_qty: Decimal
    avg_price: Decimal | None
    commission: Decimal | None
    tax: Decimal | None
    fee_total: Decimal
    settlement_date: date | None
    raw_order: dict[str, Any]
    reason: str


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _raw_order(order: Any) -> dict[str, Any]:
    return {
        "orderId": getattr(order, "order_id", None),
        "symbol": getattr(order, "symbol", None),
        "side": getattr(order, "side", None),
        "orderType": getattr(order, "order_type", None),
        "timeInForce": getattr(order, "time_in_force", None),
        "status": getattr(order, "status", None),
        "price": _json_safe(getattr(order, "price", None)),
        "quantity": _json_safe(getattr(order, "quantity", None)),
        "orderAmount": _json_safe(getattr(order, "order_amount", None)),
        "currency": getattr(order, "currency", None),
        "orderedAt": getattr(order, "ordered_at", None),
        "canceledAt": getattr(order, "canceled_at", None),
        "execution": _json_safe(getattr(order, "execution", {}) or {}),
    }


def classify_toss_order_evidence(order: Any) -> TossFillEvidence:
    broker_status = str(getattr(order, "status", "") or "").upper()
    execution = dict(getattr(order, "execution", {}) or {})
    filled_qty = _to_decimal(execution.get("filledQuantity")) or Decimal("0")
    avg_price = _to_decimal(execution.get("averageFilledPrice"))
    commission = _to_decimal(execution.get("commission"))
    tax = _to_decimal(execution.get("tax"))
    fee_total = (commission or Decimal("0")) + (tax or Decimal("0"))
    settlement_date = _to_date(execution.get("settlementDate"))

    if filled_qty > 0 and avg_price and avg_price > 0:
        if broker_status == "FILLED":
            local_status = "filled"
            verdict = "filled"
        elif broker_status == "REPLACED":
            local_status = "replaced"
            verdict = "partial"
        elif broker_status == "CANCELED":
            local_status = "cancelled"
            verdict = "partial"
        elif broker_status == "PARTIAL_FILLED":
            local_status = "partial"
            verdict = "partial"
        else:
            local_status = "partial"
            verdict = "partial"
        return TossFillEvidence(
            verdict=verdict,
            local_status=local_status,
            broker_status=broker_status,
            filled_qty=filled_qty,
            avg_price=avg_price,
            commission=commission,
            tax=tax,
            fee_total=fee_total,
            settlement_date=settlement_date,
            raw_order=_raw_order(order),
            reason=f"{broker_status} {filled_qty}@{avg_price}",
        )

    if broker_status in {"PENDING", "PARTIAL_FILLED"}:
        verdict = "pending"
        local_status = "pending"
    elif broker_status == "CANCELED":
        verdict = "none"
        local_status = "cancelled"
    elif broker_status == "REJECTED":
        verdict = "none"
        local_status = "rejected"
    elif broker_status == "REPLACED":
        verdict = "none"
        local_status = "replaced"
    elif broker_status == "CANCEL_REJECTED":
        verdict = "pending"
        local_status = "cancel_rejected"
    elif broker_status == "REPLACE_REJECTED":
        verdict = "pending"
        local_status = "replace_rejected"
    else:
        verdict = "pending"
        local_status = "pending"

    return TossFillEvidence(
        verdict=verdict,
        local_status=local_status,
        broker_status=broker_status,
        filled_qty=Decimal("0"),
        avg_price=None,
        commission=commission,
        tax=tax,
        fee_total=fee_total,
        settlement_date=settlement_date,
        raw_order=_raw_order(order),
        reason=f"{broker_status} no executable fill evidence",
    )


class TossEvidenceAdapter:
    async def fetch_evidence(self, row: Any) -> TossFillEvidence:
        client = TossReadClient.from_settings()
        try:
            order = await client.get_order(str(row.broker_order_id))
            return classify_toss_order_evidence(order)
        finally:
            await client.aclose()
