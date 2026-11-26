"""Read-only facts projection for summaries and alerts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_market_daily import (
    KRMarketBreadth,
    KRMarketDepositCredit,
    KRMarketInvestorNet,
)


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


async def read_market_daily_context(
    session: AsyncSession, today: date
) -> dict[str, Any]:
    """Return current breadth, today's investor groups, and latest deposit/credit."""
    breadth_rows = (
        (
            await session.execute(
                select(KRMarketBreadth).order_by(KRMarketBreadth.observed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_breadth: dict[str, dict[str, Any] | None] = {"KOSPI": None, "KOSDAQ": None}
    for row in breadth_rows:
        if row.market in latest_breadth and latest_breadth[row.market] is None:
            latest_breadth[row.market] = {
                "trade_date": row.trade_date.isoformat(),
                "observed_at": row.observed_at.isoformat(),
                "session_label": row.session_label,
                "up": row.up_count,
                "down": row.down_count,
                "flat": row.flat_count,
                "total": row.total_count,
                "fetched": row.fetched_count,
                "complete": row.complete,
            }

    investor_rows = (
        (
            await session.execute(
                select(KRMarketInvestorNet)
                .where(KRMarketInvestorNet.trade_date == today)
                .order_by(KRMarketInvestorNet.market, KRMarketInvestorNet.investor_code)
            )
        )
        .scalars()
        .all()
    )
    investor_groups: dict[str, list[dict[str, Any]] | None] = {
        "KOSPI": None,
        "KOSDAQ": None,
    }
    for row in investor_rows:
        if row.market not in investor_groups:
            continue
        if investor_groups[row.market] is None:
            investor_groups[row.market] = []
        investor_groups[row.market].append(
            {
                "investor_code": row.investor_code,
                "investor_name": row.investor_name,
                "net_amount": _decimal(row.net_amount),
                "buy_amount": _decimal(row.buy_amount),
                "sell_amount": _decimal(row.sell_amount),
                "buy_quantity": _decimal(row.buy_quantity),
                "sell_quantity": _decimal(row.sell_quantity),
                "amount_unit": row.amount_unit,
                "quantity_unit": row.quantity_unit,
            }
        )

    deposit = (
        (
            await session.execute(
                select(KRMarketDepositCredit)
                .order_by(KRMarketDepositCredit.bizdate.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    deposit_value = (
        None
        if deposit is None
        else {
            "bizdate": deposit.bizdate.isoformat(),
            "customer_deposit": _decimal(deposit.customer_deposit),
            "credit_loan": _decimal(deposit.credit_loan),
            "credit_loan_diff": _decimal(deposit.credit_loan_diff),
            "beneficiary_certificate": _decimal(deposit.beneficiary_certificate),
            "amount_unit": deposit.amount_unit,
        }
    )
    breadth_missing = all(value is None for value in latest_breadth.values())
    breadth_partial = any(value is None for value in latest_breadth.values())
    investors_missing = all(value is None for value in investor_groups.values())
    investors_partial = any(value is None for value in investor_groups.values())
    return {
        "latest_breadth": {
            "value": latest_breadth,
            "reason": "breadth_missing"
            if breadth_missing
            else "breadth_market_missing"
            if breadth_partial
            else None,
        },
        "investor_groups": {
            "value": investor_groups,
            "reason": "investor_groups_missing"
            if investors_missing
            else "investor_market_missing"
            if investors_partial
            else None,
        },
        "deposit_credit": {
            "value": deposit_value,
            "reason": "deposit_credit_missing" if deposit_value is None else None,
        },
    }
