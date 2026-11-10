"""Operator-recorded external cash flows."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

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


async def record_flow(
    session: AsyncSession, flow: ExternalFlow, *, commit: bool = False
) -> None:
    if flow.market != "account" or not valid_account_market(flow.account, flow.market):
        raise ValueError("invalid account or market")
    if (
        flow.currency not in {"KRW", "USD"}
        or not flow.amount.is_finite()
        or flow.amount == 0
    ):
        raise ValueError("invalid currency or flow amount")
    if commit:
        session.add(
            AccountExternalFlow(
                account=flow.account,
                market=flow.market,
                currency=flow.currency,
                amount=flow.amount,
                day=flow.day,
            )
        )
        await session.commit()
