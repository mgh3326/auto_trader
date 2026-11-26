"""Closed, order-linked fee evidence for Alpaca paper crypto fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
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


def count_filled_legs_for_day(
    *, fills: list[Any], symbol: str, posting_date: date, expected_order_id: str
) -> int:
    """Count distinct same-symbol orders in the broker's complete UTC-day fill set."""
    normalized_symbol = symbol.upper().replace("/", "")
    order_ids: set[str] = set()
    for fill in fills:
        fill_time = _field(fill, "transaction_time")
        if isinstance(fill_time, datetime):
            fill_date = (
                fill_time.astimezone(UTC).date()
                if fill_time.tzinfo
                else fill_time.date()
            )
        else:
            continue
        fill_symbol = str(_field(fill, "symbol") or "").upper().replace("/", "")
        order_id = _field(fill, "order_id")
        if fill_date == posting_date and fill_symbol == normalized_symbol and order_id:
            order_ids.add(str(order_id))
    return len(order_ids) if str(expected_order_id) in order_ids else 0


def filled_order_dates(*, fills: list[Any], order_id: str) -> set[date]:
    dates: set[date] = set()
    for fill in fills:
        if str(_field(fill, "order_id") or "") != str(order_id):
            continue
        fill_time = _field(fill, "transaction_time")
        if isinstance(fill_time, datetime):
            dates.add(
                fill_time.astimezone(UTC).date()
                if fill_time.tzinfo
                else fill_time.date()
            )
    return dates


def activity_posting_date(row: Any) -> date | None:
    raw_date = _field(row, "date")
    if isinstance(raw_date, datetime):
        return raw_date.astimezone(UTC).date() if raw_date.tzinfo else raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    try:
        return date.fromisoformat(str(raw_date)[:10])
    except ValueError:
        return None


def resolve_crypto_fee_evidence(
    *,
    symbol: str,
    side: str,
    filled_qty: Decimal,
    activities: list[Any],
    posting_date: date | str,
    filled_leg_count: int,
    filled_dates: set[date],
    terminal_filled: bool,
) -> CryptoFeeEvidence:
    """Resolve fees only when symbol/date identifies one terminal-filled leg."""
    if not activities:
        return CryptoFeeEvidence(None, None, None, "fee_pending_eod")
    if not terminal_filled:
        return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
    if filled_leg_count != 1:
        return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
    fee_total = Decimal("0")
    currency: str | None = None
    seen: set[str] = set()
    normalized_symbol = symbol.upper().replace("/", "")
    expected_date = (
        posting_date.isoformat()
        if isinstance(posting_date, date)
        else str(posting_date)
    )
    if filled_dates != {date.fromisoformat(expected_date[:10])}:
        return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
    pair = symbol.upper().split("/")
    if len(pair) != 2:
        return CryptoFeeEvidence(None, None, None, "fee_currency_ambiguous")
    base, quote = pair
    for row in activities:
        activity_id = _field(row, "id")
        activity_type = str(_field(row, "activity_type") or "").upper()
        if not activity_id or str(activity_id) in seen:
            return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
        seen.add(str(activity_id))
        activity_date = activity_posting_date(row)
        if activity_date is None or activity_date.isoformat() != expected_date:
            return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
        row_symbol = str(_field(row, "symbol") or "").upper().replace("/", "")
        if row_symbol != normalized_symbol:
            return CryptoFeeEvidence(None, None, None, "fee_activity_unattributable")
        if str(_field(row, "status") or "").lower() != "executed":
            return CryptoFeeEvidence(None, None, None, "fee_activity_unreadable")
        try:
            if activity_type == "CFEE":
                raw_fee = _field(row, "qty")
                fee_currency = base if side == "buy" else quote
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
