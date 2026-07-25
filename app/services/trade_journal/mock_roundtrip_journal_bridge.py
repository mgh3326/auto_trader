"""ROB-405 Slice A — bridge reconciled kis_mock roundtrips into trade_journals.

Pairs KISMockOrderLedger rows by correlation_id (ROB-402 watch→order link),
creates an active journal on the entry (buy) leg and closes it on the exit
(sell) leg with pnl_pct. Idempotent via trade_journals.correlation_id. Writes
ONLY account_type='mock' journals; live journals are untouched. Default off.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.models.review import KISMockOrderLedger
from app.models.trade_journal import TradeJournal
from app.services.brokers.kis.mock_scalping_exec.ledger_state import real_order_filter

logger = logging.getLogger(__name__)

_RECONCILED_STATES = ("fill", "reconciled")


async def sync_mock_roundtrip_journals(db, *, force: bool = False) -> dict[str, Any]:
    """Create/close trade_journals from reconciled kis_mock roundtrips."""
    if not force and not settings.MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED:
        return {"status": "disabled", "created": 0, "closed": 0}

    rows = (
        (
            await db.execute(
                select(KISMockOrderLedger)
                .where(
                    KISMockOrderLedger.account_mode == "kis_mock",
                    KISMockOrderLedger.correlation_id.is_not(None),
                    KISMockOrderLedger.lifecycle_state.in_(_RECONCILED_STATES),
                    # ROB-843 P2: never journal a control/reservation row.
                    real_order_filter(),
                )
                .order_by(
                    KISMockOrderLedger.trade_date.asc(), KISMockOrderLedger.id.asc()
                )
            )
        )
        .scalars()
        .all()
    )

    groups: dict[str, list[KISMockOrderLedger]] = {}
    for r in rows:
        groups.setdefault(r.correlation_id, []).append(r)

    created = 0
    closed = 0
    for cid, legs in groups.items():
        journal = (
            await db.execute(
                select(TradeJournal).where(TradeJournal.correlation_id == cid)
            )
        ).scalar_one_or_none()

        entry = next(
            (leg for leg in legs if leg.scalping_role == "entry" or leg.side == "buy"),
            None,
        )
        exit_leg = next(
            (leg for leg in legs if leg.scalping_role == "exit" or leg.side == "sell"),
            None,
        )

        if entry is not None and journal is None:
            journal = TradeJournal(
                symbol=entry.symbol,
                instrument_type=entry.instrument_type,
                side="buy",
                entry_price=entry.price,
                quantity=entry.quantity,
                amount=entry.amount,
                thesis=entry.thesis or "auto: kis_mock roundtrip",
                strategy=entry.strategy,
                account_type="mock",
                account="kis_mock",
                correlation_id=cid,
                status="active",
            )
            db.add(journal)
            await db.flush()
            created += 1

        if exit_leg is not None and journal is not None and journal.status == "active":
            journal.exit_price = exit_leg.price
            journal.exit_date = datetime.now(tz=UTC)
            journal.exit_reason = exit_leg.exit_reason or "roundtrip_exit"
            entry_price = journal.entry_price
            if entry_price and entry_price > 0:
                journal.pnl_pct = (
                    (Decimal(exit_leg.price) - entry_price) / entry_price * 100
                )
            detail = dict(journal.extra_metadata or {})
            detail["roundtrip_net_pnl"] = (
                str(exit_leg.net_pnl) if exit_leg.net_pnl is not None else None
            )
            detail["roundtrip_gross_pnl"] = (
                str(exit_leg.gross_pnl) if exit_leg.gross_pnl is not None else None
            )
            journal.extra_metadata = detail
            journal.status = "closed"
            closed += 1

    await db.commit()
    return {"status": "ok", "created": created, "closed": closed}
