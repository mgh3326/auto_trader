"""Append-only account snapshots; observations are supplied by read-only readers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_value import AccountValueSnapshot
from app.services.account_value.identity import valid_account_market


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: Decimal
    currency: str
    mark: Decimal | None
    mark_source: str | None
    mark_asof: datetime | None


@dataclass(frozen=True)
class Observation:
    account: str
    market: str
    day: date
    observed_at: datetime
    holdings: tuple[Holding, ...]
    cash: dict[str, Decimal | None]
    reasons: tuple[str, ...] = ()
    fx: dict = field(default_factory=dict)


class Reader(Protocol):
    async def read(self) -> Sequence[Observation]: ...


def _number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value.is_finite():
        raise ValueError("non-finite amount")
    return str(value)


def serialize(observation: Observation) -> dict:
    if not valid_account_market(observation.account, observation.market):
        raise ValueError("invalid account or market")
    if observation.observed_at.utcoffset() is None:
        raise ValueError("observed_at must have a timezone")
    holdings = []
    reasons = set(observation.reasons)
    for holding in observation.holdings:
        if (
            not holding.symbol
            or holding.quantity < 0
            or not holding.quantity.is_finite()
        ):
            raise ValueError("invalid holding")
        if holding.mark is not None and (
            not holding.mark.is_finite() or holding.mark <= 0
        ):
            raise ValueError("invalid mark")
        if holding.mark is None or not holding.mark_source or holding.mark_asof is None:
            reasons.add("mark_unknown:" + holding.symbol)
        elif holding.mark_asof.utcoffset() is None:
            raise ValueError("mark_asof must have a timezone")
        holdings.append(
            {
                "symbol": holding.symbol,
                "quantity": _number(holding.quantity),
                "currency": holding.currency,
                "mark": _number(holding.mark),
                "mark_source": holding.mark_source,
                "mark_asof": holding.mark_asof.isoformat()
                if holding.mark_asof
                else None,
            }
        )
    if observation.market == "account" and (
        not observation.cash
        or any(value is None for value in observation.cash.values())
    ):
        reasons.add("cash_unknown")
    return {
        "account": observation.account,
        "market": observation.market,
        "day": observation.day,
        "observed_at": observation.observed_at,
        "holdings": holdings,
        "cash": {
            currency: _number(value) for currency, value in observation.cash.items()
        },
        "reasons": sorted(reasons),
        "fx": observation.fx,
    }


async def capture(
    reader: Reader, session: AsyncSession, *, commit: bool = False
) -> list[dict]:
    """Preview observations or append each new account/market/day atomically."""
    rows = [serialize(item) for item in await reader.read()]
    identities = [(row["account"], row["market"], row["day"]) for row in rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate snapshot identity")
    if not commit or not rows:
        return [{**row, "write_status": "preview"} for row in rows]

    statement = (
        insert(AccountValueSnapshot)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["account", "market", "day"])
        .returning(
            AccountValueSnapshot.account,
            AccountValueSnapshot.market,
            AccountValueSnapshot.day,
        )
    )
    inserted = {tuple(item) for item in (await session.execute(statement)).all()}
    query = select(AccountValueSnapshot).where(
        tuple_(
            AccountValueSnapshot.account,
            AccountValueSnapshot.market,
            AccountValueSnapshot.day,
        ).in_(identities)
    )
    stored = {
        (item.account, item.market, item.day): item
        for item in (await session.execute(query)).scalars().all()
    }
    if len(stored) != len(identities):
        raise RuntimeError("snapshot readback incomplete")
    await session.commit()
    return [
        {
            "account": item.account,
            "market": item.market,
            "day": item.day,
            "observed_at": item.observed_at,
            "holdings": item.holdings,
            "cash": item.cash,
            "reasons": item.reasons,
            "fx": item.fx,
            "write_status": "inserted" if identity in inserted else "already_present",
        }
        for identity in identities
        for item in (stored[identity],)
    ]
