from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from app.services.execution_ledger.repository import ExecutionLedgerRepository

MAX_SQL_INT32 = 2_147_483_647


def _stable_int32_hash(seed: str) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) & MAX_SQL_INT32


def _parse_toss_fill_time(raw_order: dict[str, Any]) -> datetime:
    execution = dict(raw_order.get("execution") or {})
    for key in ("filledAt", "lastFilledAt", "filled_at", "last_filled_at"):
        value = execution.get(key)
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return (
                    parsed.astimezone(KST)
                    if parsed.tzinfo
                    else parsed.replace(tzinfo=KST)
                )
            except ValueError:
                pass
    ordered_at = raw_order.get("orderedAt")
    if ordered_at:
        try:
            parsed = datetime.fromisoformat(str(ordered_at).replace("Z", "+00:00"))
            return (
                parsed.astimezone(KST) if parsed.tzinfo else parsed.replace(tzinfo=KST)
            )
        except ValueError:
            pass
    return datetime.now(tz=KST)


def _venue_for_market(market: str) -> str:
    return "toss_kr" if market == "kr" else "toss_us"


def _instrument_for_market(market: str) -> str:
    return "equity_kr" if market == "kr" else "equity_us"


def build_toss_execution_ledger_upsert(
    row: Any,
    evidence: Any,
    *,
    previous_filled_qty: Decimal,
    delta: Decimal,
    avg_price: Decimal,
) -> ExecutionLedgerUpsert:
    broker_order_id = str(row.broker_order_id)
    cumulative = previous_filled_qty + delta
    fill_seq = _stable_int32_hash(
        f"{broker_order_id}:{previous_filled_qty.normalize()}:{cumulative.normalize()}"
    )
    currency = row.currency or ("KRW" if row.market == "kr" else "USD")
    raw_order = dict(evidence.raw_order or {})
    return ExecutionLedgerUpsert(
        broker="toss",
        account_mode="live",
        venue=_venue_for_market(row.market),
        instrument_type=_instrument_for_market(row.market),
        symbol=row.symbol,
        raw_symbol=row.symbol,
        side=str(row.side).lower(),
        broker_order_id=broker_order_id,
        fill_seq=fill_seq,
        filled_qty=delta,
        filled_price=avg_price,
        filled_notional=delta * avg_price,
        fee_amount=evidence.fee_total,
        fee_currency=currency,
        filled_at=_parse_toss_fill_time(raw_order),
        currency=currency,
        correlation_id=getattr(row, "correlation_id", None),
        source="reconciler",
        raw_payload_json={
            "source": "toss_reconcile_orders",
            "toss_live_order_ledger_id": row.id,
            "previous_filled_qty": str(previous_filled_qty),
            "broker_cumulative_filled_qty": str(evidence.filled_qty),
            "raw_order": raw_order,
        },
    )


async def upsert_toss_execution_fill(
    db: AsyncSession,
    row: Any,
    evidence: Any,
    *,
    previous_filled_qty: Decimal,
    delta: Decimal,
    avg_price: Decimal,
) -> tuple[str, int]:
    fill = build_toss_execution_ledger_upsert(
        row,
        evidence,
        previous_filled_qty=previous_filled_qty,
        delta=delta,
        avg_price=avg_price,
    )
    return await ExecutionLedgerRepository(db).upsert_fill(fill)
