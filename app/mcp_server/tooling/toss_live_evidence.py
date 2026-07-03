from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
import logging
from zoneinfo import ZoneInfo

from app.services.brokers.toss import TossReadClient

logger = logging.getLogger(__name__)


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


_KST = ZoneInfo("Asia/Seoul")
# Bound CLOSED pagination so a huge order history cannot blow the time budget.
_TOSS_CLOSED_PAGE_CAP = 20


def _kst_date_str(dt: datetime | None) -> str:
    ref = dt or datetime.now(_KST)
    return ref.astimezone(_KST).date().isoformat()


class TossBatchEvidenceSource:
    """ROB-669 (absorbs ROB-632) — batched broker evidence for reconcile.

    Replaces the per-row ``get_order`` N+1 (fresh client + OAuth + account-seq
    resolve + rate-limit wait per open ledger row) with a bounded set of list
    calls: one ``GET /orders?status=OPEN`` (all open orders in a single call —
    Toss ignores cursor/limit for OPEN) plus windowed ``GET /orders?status=CLOSED``
    cursor pagination from the oldest open ledger row's KST date to today. List
    rows carry the same execution fields the single-order classifier consumes, so
    ``classify_toss_order_evidence`` is reused unchanged. Rows older than the
    window fall back to a single ``get_order`` (never dropped).
    """

    def __init__(
        self,
        client: TossReadClient,
        *,
        order_map: dict[str, Any],
        window_from: str,
        window_to: str,
        closed_pages_capped: bool,
        owns_client: bool,
    ) -> None:
        self._client = client
        self._order_map = order_map
        self.window_from = window_from
        self.window_to = window_to
        self.closed_pages_capped = closed_pages_capped
        self._owns_client = owns_client
        self.single_fetch_count = 0

    @classmethod
    async def build(
        cls,
        *,
        rows: list[Any],
        symbol: str | None = None,
        client: TossReadClient | None = None,
    ) -> TossBatchEvidenceSource:
        owns_client = client is None
        client = client or TossReadClient.from_settings()
        oldest = min(
            (getattr(r, "trade_date", None) for r in rows if getattr(r, "trade_date", None)),
            default=None,
        )
        window_from = _kst_date_str(oldest)
        window_to = _kst_date_str(None)

        order_map: dict[str, Any] = {}
        # 1) OPEN — one call returns all open orders.
        open_page = await client.list_orders(status="OPEN", symbol=symbol)
        for order in open_page.orders:
            order_map[str(order.order_id)] = order

        # 2) CLOSED — windowed cursor pagination, capped.
        cursor: str | None = None
        pages = 0
        capped = False
        while True:
            page = await client.list_orders(
                status="CLOSED",
                symbol=symbol,
                from_date=window_from,
                to_date=window_to,
                cursor=cursor,
                limit=100,
            )
            for order in page.orders:
                order_map[str(order.order_id)] = order  # CLOSED wins over OPEN
            pages += 1
            if not page.has_next or not page.next_cursor:
                break
            if pages >= _TOSS_CLOSED_PAGE_CAP:
                capped = True
                logger.warning(
                    "toss reconcile CLOSED pagination capped at %d pages "
                    "(window %s..%s); older rows use single-order fallback",
                    _TOSS_CLOSED_PAGE_CAP,
                    window_from,
                    window_to,
                )
                break
            cursor = page.next_cursor

        return cls(
            client,
            order_map=order_map,
            window_from=window_from,
            window_to=window_to,
            closed_pages_capped=capped,
            owns_client=owns_client,
        )

    async def evidence_for(self, row: Any) -> TossFillEvidence:
        order = self._order_map.get(str(row.broker_order_id))
        if order is not None:
            return classify_toss_order_evidence(order)
        # Older than the window (or a pre-window replacement original): single fetch.
        self.single_fetch_count += 1
        order = await self._client.get_order(str(row.broker_order_id))
        return classify_toss_order_evidence(order)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
