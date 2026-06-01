"""ROB-407 — 제네릭 live 주문 accepted-only ledger + evidence-gated reconcile.

US/해외(equity_us)·crypto(crypto) live 주문 전용. KR domestic은 kis_live_ledger.py 유지.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.models.review import LiveOrderLedger
from app.mcp_server.tooling.kis_live_ledger import _order_session_factory, _to_float
from app.mcp_server.tooling.order_journal import (
    _close_journals_on_sell,
    _create_trade_journal_for_buy,
    _link_journal_to_fill,
    _save_order_fill,
)
from app.services.brokers.kis.mock_scalping_exec.fill_evidence import (
    FillEvidence,
    FillVerdict,
)

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _save_live_order_ledger(
    *,
    broker: str,
    account_scope: str,
    market: str,
    symbol: str,
    exchange: str | None,
    market_symbol: str | None,
    side: str,
    order_kind: str,
    quantity: float | None,
    price: float | None,
    amount: float | None,
    currency: str | None,
    order_no: str | None,
    order_time: str | None,
    status: str,
    response_code: str | None,
    response_message: str | None,
    raw_response: dict[str, Any] | None,
    reason: str | None,
    thesis: str | None,
    strategy: str | None,
    target_price: float | None,
    stop_loss: float | None,
    min_hold_days: int | None,
    notes: str | None,
    exit_reason: str | None,
    indicators_snapshot: dict[str, Any] | None,
) -> int:
    async with _order_session_factory()() as db:
        row = LiveOrderLedger(
            trade_date=datetime.now(timezone.utc),
            broker=broker,
            account_scope=account_scope,
            market=market,
            symbol=symbol,
            exchange=exchange,
            market_symbol=market_symbol,
            side=side,
            order_kind=order_kind,
            quantity=_to_decimal(quantity),
            price=_to_decimal(price),
            amount=_to_decimal(amount),
            currency=currency,
            order_no=order_no,
            order_time=order_time,
            status=status,
            lifecycle_state="accepted" if status == "accepted" else "rejected",
            response_code=response_code,
            response_message=response_message,
            raw_response=raw_response,
            reason=reason,
            thesis=thesis,
            strategy=strategy,
            target_price=_to_decimal(target_price),
            stop_loss=_to_decimal(stop_loss),
            min_hold_days=min_hold_days,
            notes=notes,
            exit_reason=exit_reason,
            indicators_snapshot=indicators_snapshot,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def _load_live_ledger_row(ledger_id: int) -> LiveOrderLedger | None:
    async with _order_session_factory()() as db:
        row = await db.get(LiveOrderLedger, ledger_id)
        if row is not None:
            db.expunge(row)
        return row


def _derive_live_send_status(*, rt_cd: str | None, order_no: str | None) -> str:
    """rt_cd=='0' (또는 order_no 존재) → accepted, 그 외 rejected."""
    if rt_cd is not None and str(rt_cd) not in ("0", ""):
        return "rejected"
    if order_no:
        return "accepted"
    return "rejected" if rt_cd not in (None, "0", "") else "accepted"
