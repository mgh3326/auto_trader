"""Broker-free revision history reads for protected positions."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import User
from app.services.protected_quantity_service import (
    ProtectedQuantityService,
    ProtectionKey,
)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


async def _actor_names(db: AsyncSession, actor_ids: set[int]) -> dict[int, str | None]:
    if not actor_ids:
        return {}
    rows = await db.execute(select(User).where(User.id.in_(actor_ids)))
    names: dict[int, str | None] = {}
    for user in rows.scalars():
        names[int(user.id)] = user.nickname or user.username
    return names


def _history_item(revision: Any, *, actor_name: str | None) -> dict[str, Any]:
    return {
        "revision": int(revision.revision),
        "action": str(revision.action),
        "previous_quantity": _decimal_text(
            None
            if revision.previous_quantity is None
            else Decimal(str(revision.previous_quantity))
        ),
        "new_quantity": _decimal_text(Decimal(str(revision.new_quantity))),
        "broker_held": _decimal_text(Decimal(str(revision.broker_held_observed))),
        "broker_sellable": _decimal_text(
            Decimal(str(revision.broker_sellable_observed))
        ),
        "broker_observed_at": revision.broker_observed_at.isoformat(),
        "reason": str(revision.reason),
        "actor_user_id": int(revision.actor_user_id),
        "actor": actor_name,
        "origin": str(revision.origin),
        "recorded_at": revision.recorded_at.isoformat(),
    }


async def read_protected_position_history(
    db: AsyncSession, *, key: ProtectionKey
) -> list[dict[str, Any]]:
    service = ProtectedQuantityService(db)
    revisions = await service.list_revisions(key=key)
    actor_names = await _actor_names(
        db, {int(revision.actor_user_id) for revision in revisions}
    )
    return [
        _history_item(revision, actor_name=actor_names.get(int(revision.actor_user_id)))
        for revision in revisions
    ]
