from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.errors import OrderProposalError

_VALID_SIDES = frozenset({"buy", "sell"})
_VALID_ORDER_TYPES = frozenset({"limit", "market"})
_VALID_STATUSES = frozenset({"open", "filled", "cancelled", "expired", "rejected"})


def canonical_decimal(value: Any) -> str | None:
    if value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OrderProposalError(f"invalid decimal value: {value!r}") from exc
    if not decimal.is_finite():
        raise OrderProposalError(f"invalid decimal value: {value!r}")

    text = format(decimal.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


@dataclass(frozen=True)
class TargetOrderSnapshot:
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    limit_price: str | None
    remaining_quantity: str
    status: str
    observed_at: str

    @classmethod
    def from_broker_order(
        cls,
        row: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> TargetOrderSnapshot:
        return cls._from_parts(
            broker_order_id=row.get("order_id"),
            symbol=row.get("symbol"),
            side=row.get("side"),
            order_type=row.get("order_type") or "limit",
            limit_price=canonical_decimal(row.get("ordered_price")),
            remaining_quantity=canonical_decimal(row.get("remaining_qty")) or "0",
            status=cls._normalize_status(row.get("status")),
            observed_at=observed_at,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TargetOrderSnapshot:
        observed_raw = payload.get("observed_at")
        if not isinstance(observed_raw, str):
            raise OrderProposalError("target observed_at is required")
        try:
            observed_at = datetime.fromisoformat(observed_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OrderProposalError("target observed_at must be ISO-8601") from exc

        return cls._from_parts(
            broker_order_id=payload.get("broker_order_id"),
            symbol=payload.get("symbol"),
            side=payload.get("side"),
            order_type=payload.get("order_type"),
            limit_price=canonical_decimal(payload.get("limit_price")),
            remaining_quantity=canonical_decimal(payload.get("remaining_quantity"))
            or "0",
            status=cls._normalize_status(payload.get("status")),
            observed_at=observed_at,
        )

    @classmethod
    def _from_parts(
        cls,
        *,
        broker_order_id: Any,
        symbol: Any,
        side: Any,
        order_type: Any,
        limit_price: str | None,
        remaining_quantity: str,
        status: Any,
        observed_at: datetime,
    ) -> TargetOrderSnapshot:
        broker_order_id = cls._required_text("broker_order_id", broker_order_id)
        symbol = cls._required_text("symbol", symbol)
        side = cls._required_text("side", side).lower()
        order_type = cls._required_text("order_type", order_type).lower()
        status = cls._required_text("status", status).lower()

        if side not in _VALID_SIDES:
            raise OrderProposalError("target side must be buy or sell")
        if order_type not in _VALID_ORDER_TYPES:
            raise OrderProposalError("target order_type must be limit or market")
        if status not in _VALID_STATUSES:
            raise OrderProposalError(f"unsupported target order status: {status}")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise OrderProposalError("target observed_at must be timezone-aware")

        remaining = Decimal(remaining_quantity)
        if remaining < 0:
            raise OrderProposalError("target remaining quantity cannot be negative")
        if status == "open" and remaining <= 0:
            raise OrderProposalError("open target requires positive remaining quantity")

        return cls(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            limit_price=limit_price,
            remaining_quantity=remaining_quantity,
            status=status,
            observed_at=observed_at.astimezone(UTC).isoformat(),
        )

    @staticmethod
    def _required_text(name: str, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise OrderProposalError(f"target {name} is required")
        return text

    @staticmethod
    def _normalize_status(value: Any) -> str:
        status = str(value or "").strip().lower()
        return "open" if status in {"pending", "partial", "open"} else status

    def to_payload(self) -> dict[str, str | None]:
        return asdict(self)

    def matches_approved(self, fresh: TargetOrderSnapshot) -> bool:
        return all(
            getattr(self, field) == getattr(fresh, field)
            for field in (
                "broker_order_id",
                "symbol",
                "side",
                "order_type",
                "limit_price",
                "remaining_quantity",
                "status",
            )
        )
