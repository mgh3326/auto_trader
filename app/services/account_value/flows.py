"""Operator-recorded external cash flows."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_value import AccountExternalFlow
from app.services.account_value.identity import valid_account_market


@dataclass(frozen=True)
class ExternalFlow:
    account: str
    market: str
    currency: str
    amount: Decimal
    day: date
    flow_id: UUID


async def record_flow(
    session: AsyncSession, flow: ExternalFlow, *, commit: bool = False
) -> str:
    if flow.market != "account" or not valid_account_market(flow.account, flow.market):
        raise ValueError("invalid account or market")
    if (
        flow.currency not in {"KRW", "USD"}
        or not flow.amount.is_finite()
        or flow.amount == 0
        or not isinstance(flow.flow_id, UUID)
    ):
        raise ValueError("invalid currency, flow amount or flow ID")
    if not commit:
        return "preview"
    values = {
        "flow_id": flow.flow_id,
        "account": flow.account,
        "market": flow.market,
        "currency": flow.currency,
        "amount": flow.amount,
        "day": flow.day,
    }
    statement = (
        insert(AccountExternalFlow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["flow_id"])
        .returning(AccountExternalFlow.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        existing = (
            await session.execute(
                select(AccountExternalFlow).where(
                    AccountExternalFlow.flow_id == flow.flow_id
                )
            )
        ).scalar_one()
        if any(getattr(existing, key) != value for key, value in values.items()):
            await session.rollback()
            raise ValueError("flow ID already belongs to a different flow")
    await session.commit()
    return "inserted" if inserted is not None else "already_present"
