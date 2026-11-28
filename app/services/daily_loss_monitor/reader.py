"""Ledger read for one account (SELECT only, in-effect rows only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.services.daily_loss_monitor.accounts import AccountScope
from app.services.daily_loss_monitor.rules import Fill

AUTHORITATIVE_SOURCES: tuple[str, ...] = ("reconciler", "manual_import")


@dataclass(frozen=True)
class AccountFills:
    history: list[Fill]
    today: list[Fill]
    #: Today's websocket orders no authoritative row covers yet.
    unreconciled_orders: frozenset[str]

    @property
    def provisional_unreconciled(self) -> bool:
        return bool(self.unreconciled_orders)


async def load_account_fills(
    session: AsyncSession, scope: AccountScope, *, day_start: datetime
) -> AccountFills:
    rows = (
        (
            await session.execute(
                select(ExecutionLedger)
                .where(
                    execution_ledger_in_effect(),
                    ExecutionLedger.broker == scope.broker,
                    ExecutionLedger.account_mode == "live",
                    ExecutionLedger.instrument_type == scope.instrument_type,
                )
                .order_by(ExecutionLedger.filled_at, ExecutionLedger.id)
            )
        )
        .scalars()
        .all()
    )
    history: list[Fill] = []
    today: list[Fill] = []
    covered: set[str] = set()
    provisional_today: set[str] = set()
    for row in rows:
        order = str(row.broker_order_id).lstrip("0")
        if row.source not in AUTHORITATIVE_SOURCES:
            if row.filled_at >= day_start:
                provisional_today.add(order)
            continue
        covered.add(order)
        fill = Fill(
            symbol=row.symbol,
            side=row.side,
            quantity=row.filled_qty,
            price=row.filled_price,
            fee=None if row.fee_basis in (None, "not_provided") else row.fee_amount,
            tax=row.tax_amount,
            fee_currency=row.fee_currency,
            currency=row.currency,
            filled_at=row.filled_at,
            fx_krw=None,
        )
        (today if row.filled_at >= day_start else history).append(fill)
    return AccountFills(
        history=history,
        today=today,
        unreconciled_orders=frozenset(provisional_today - covered),
    )
