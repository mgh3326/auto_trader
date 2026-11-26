"""Closed, order-linked fee evidence for Alpaca paper crypto fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True)
class CryptoFeeEvidence:
    amount: Decimal | None
    currency: str | None
    net_received_qty: Decimal | None
    reason: str | None


def _field(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def resolve_crypto_fee_evidence(
    *,
    order_id: str,
    symbol: str,
    side: str,
    filled_qty: Decimal,
    activities: list[Any],
) -> CryptoFeeEvidence:
    """Resolve complete fee activities; absence and malformed evidence stay unknown."""
    if not activities:
        return CryptoFeeEvidence(None, None, None, "fee_activity_missing")
    fee_total = Decimal("0")
    currency: str | None = None
    seen: set[str] = set()
    normalized_symbol = symbol.upper().replace("/", "")
    base = symbol.upper().split("/")[0]
    for row in activities:
        activity_id = _field(row, "id")
        row_order = _field(row, "order_id")
        activity_type = str(_field(row, "activity_type") or "").upper()
        if (
            not activity_id
            or str(activity_id) in seen
            or str(row_order) != str(order_id)
        ):
            return CryptoFeeEvidence(None, None, None, "fee_activity_order_mismatch")
        seen.add(str(activity_id))
        row_symbol = str(_field(row, "symbol") or "").upper().replace("/", "")
        if row_symbol != normalized_symbol:
            return CryptoFeeEvidence(None, None, None, "fee_activity_symbol_mismatch")
        if str(_field(row, "status") or "").lower() != "executed":
            return CryptoFeeEvidence(None, None, None, "fee_activity_unreadable")
        try:
            if activity_type == "CFEE":
                raw_fee = _field(row, "qty")
                fee_currency = (
                    base if side == "buy" else str(_field(row, "currency") or "")
                )
            elif activity_type == "FEE":
                raw_fee = _field(row, "net_amount")
                fee_currency = str(_field(row, "currency") or "USD").upper()
            else:
                return CryptoFeeEvidence(None, None, None, "fee_activity_type_unknown")
            fee = abs(Decimal(str(raw_fee)))
        except (InvalidOperation, TypeError, ValueError):
            return CryptoFeeEvidence(None, None, None, "fee_activity_unreadable")
        if not fee.is_finite() or not fee_currency:
            return CryptoFeeEvidence(None, None, None, "fee_activity_unreadable")
        if currency is not None and currency != fee_currency:
            return CryptoFeeEvidence(None, None, None, "fee_currency_ambiguous")
        currency = fee_currency
        fee_total += fee
    net_qty: Decimal | None = None
    if side == "buy":
        if currency == base:
            if fee_total >= filled_qty:
                return CryptoFeeEvidence(
                    None, None, None, "fee_exceeds_filled_quantity"
                )
            net_qty = filled_qty - fee_total
        else:
            net_qty = filled_qty
    return CryptoFeeEvidence(fee_total, currency, net_qty, None)
