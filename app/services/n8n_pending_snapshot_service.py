"""Pending snapshot service — save snapshots and resolve status."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.review import PendingSnapshot

logger = logging.getLogger(__name__)

_INSTRUMENT_MAP = {
    "crypto": "crypto",
    "equity_kr": "equity_kr",
    "equity_us": "equity_us",
    "kr": "equity_kr",
    "us": "equity_us",
}

_VALID_RESOLUTIONS = {"filled", "cancelled", "expired"}


async def save_pending_snapshots(
    session: AsyncSession,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    saved = 0
    errors: list[dict[str, Any]] = []
    snapshot_date = now_kst()

    for item in items:
        try:
            instrument = _INSTRUMENT_MAP.get(
                item.get("instrument_type", ""), item.get("instrument_type", "")
            )
            snapshot = PendingSnapshot(
                snapshot_date=snapshot_date,
                symbol=item.get("symbol", ""),
                instrument_type=instrument,
                side=item.get("side", "buy"),
                order_price=item.get("order_price", 0),
                quantity=item.get("quantity", 0),
                current_price=item.get("current_price"),
                gap_pct=item.get("gap_pct"),
                days_pending=item.get("days_pending"),
                account=item.get("account", ""),
                order_id=item.get("order_id"),
                resolved_as="pending",
            )
            session.add(snapshot)
            saved += 1
        except Exception as exc:
            logger.warning("Failed to save snapshot: %s", exc)
            errors.append({"order_id": item.get("order_id"), "error": str(exc)})

    await session.commit()
    return {"saved_count": saved, "errors": errors}


async def resolve_pending_snapshots(
    session: AsyncSession,
    resolutions: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = 0
    not_found = 0
    errors: list[dict[str, Any]] = []
    resolved_at = now_kst()

    for item in resolutions:
        order_id = item.get("order_id")
        account = item.get("account")
        resolved_as = item.get("resolved_as", "")

        if resolved_as not in _VALID_RESOLUTIONS:
            errors.append(
                {
                    "order_id": order_id,
                    "error": f"Invalid resolved_as: {resolved_as}. Must be one of {_VALID_RESOLUTIONS}",
                }
            )
            continue

        try:
            stmt = (
                update(PendingSnapshot)
                .where(
                    PendingSnapshot.account == account,
                    PendingSnapshot.order_id == order_id,
                    PendingSnapshot.resolved_as == "pending",
                )
                .values(resolved_as=resolved_as, resolved_at=resolved_at)
            )
            result = await session.execute(stmt)

            if result.rowcount > 0:
                resolved += 1
            else:
                not_found += 1
        except Exception as exc:
            logger.warning("Failed to resolve snapshot %s: %s", order_id, exc)
            errors.append({"order_id": order_id, "error": str(exc)})

    await session.commit()
    return {
        "resolved_count": resolved,
        "not_found_count": not_found,
        "errors": errors,
    }
