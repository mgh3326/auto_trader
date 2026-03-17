"""Pending-review service — wraps pending-orders with fill probability."""

from __future__ import annotations

import logging
from typing import Any

from app.core.timezone import now_kst
from app.services.n8n_pending_orders_service import fetch_pending_orders

logger = logging.getLogger(__name__)


def compute_fill_probability(gap_pct: float | None, days_pending: int) -> str:
    abs_gap = abs(gap_pct or 0)

    if days_pending > 5 and abs_gap > 3:
        return "stale"
    if abs_gap < 1:
        return "high"
    elif abs_gap <= 5:
        return "medium"
    else:
        return "low"


_SUGGESTIONS = {
    "high": "곧 체결 예상 — 대기",
    "medium": "가격 조정 검토",
    "low": "체결 가능성 낮음 — 취소 또는 가격 조정",
    "stale": "장기 미체결 — 재검토 필요",
}


async def fetch_pending_review(
    market: str = "all",
    min_amount: float = 0,
) -> dict[str, Any]:
    as_of_dt = now_kst().replace(microsecond=0)

    result = await fetch_pending_orders(
        market=market,
        min_amount=min_amount,
        include_current_price=True,
        side=None,
        as_of=as_of_dt,
        attention_only=False,
        near_fill_pct=2.0,
    )

    enriched_orders = []
    for order in result.get("orders", []):
        gap_pct = order.get("gap_pct")
        days_pending = order.get("age_days", 0)
        probability = compute_fill_probability(gap_pct, days_pending)

        enriched_orders.append(
            {
                "order_id": order.get("order_id", ""),
                "symbol": order.get("symbol", ""),
                "raw_symbol": order.get("raw_symbol", ""),
                "market": order.get("market", ""),
                "side": order.get("side", ""),
                "order_price": order.get("order_price", 0),
                "current_price": order.get("current_price"),
                "gap_pct": gap_pct,
                "gap_pct_fmt": order.get("gap_pct_fmt"),
                "amount_krw": order.get("amount_krw"),
                "quantity": order.get("quantity", 0),
                "remaining_qty": order.get("remaining_qty", 0),
                "created_at": order.get("created_at", ""),
                "age_days": days_pending,
                "currency": order.get("currency", "KRW"),
                "days_pending": days_pending,
                "fill_probability": probability,
                "suggestion": _SUGGESTIONS.get(probability),
            }
        )

    return {
        "orders": enriched_orders,
        "errors": result.get("errors", []),
    }
