"""hk 5441 — record what the broker said about one sent child order (B4, B30).

The only writer of ``review.execution_child_order_evidence``. A row never
changes a journal row: ``sending`` and ``uncertain`` stay what they are. It is
the positive evidence other readers need before they treat an order as ended
(``strategy_sleeves.intent_orders``).

Only a reading the broker actually returned is recorded. "Not found" and a
failed read are not evidence of anything and write nothing: the child stays
unresolved (design §1.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_mandates import (
    BROKER_ORDER_STATES,
    ExecutionChildOrderEvidence,
)
from app.services.strategy_sleeves.intent_orders import FINAL_BROKER_STATES


@dataclass(frozen=True)
class OrderReading:
    """One Upbit order as read. ``None`` from the parser means "no evidence"."""

    order_uuid: str
    state: str
    executed_volume: Decimal
    remaining_volume: Decimal | None

    @property
    def final(self) -> bool:
        return self.state in FINAL_BROKER_STATES


def _volume(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def parse_order_reading(detail: Any) -> OrderReading | None:
    """An Upbit order body to a reading, or ``None`` when it proves nothing.

    Every field must be present and readable. A body with no uuid, an unknown
    state or an unreadable executed volume is not evidence.
    """
    if not isinstance(detail, dict):
        return None
    uuid = str(detail.get("uuid") or "").strip()
    state = str(detail.get("state") or "").strip()
    executed = _volume(detail.get("executed_volume"))
    if not uuid or state not in BROKER_ORDER_STATES or executed is None:
        return None
    return OrderReading(
        order_uuid=uuid,
        state=state,
        executed_volume=executed,
        remaining_volume=_volume(detail.get("remaining_volume")),
    )


async def record_child_order_evidence(
    session: AsyncSession,
    *,
    child_id: int,
    reading: OrderReading,
    observed_at: datetime,
    recorded_by: str,
) -> str:
    """Insert or advance one evidence row. The caller commits.

    Returns ``inserted``, ``updated``, ``unchanged`` or ``refused``. A final row
    is never replaced, an order uuid never changes and an executed volume never
    goes down; the DB trigger holds the same three rules.
    """
    row = await session.scalar(
        select(ExecutionChildOrderEvidence)
        .where(ExecutionChildOrderEvidence.child_id == child_id)
        .with_for_update()
    )
    if row is None:
        session.add(
            ExecutionChildOrderEvidence(
                child_id=child_id,
                order_uuid=reading.order_uuid,
                broker_state=reading.state,
                executed_volume=reading.executed_volume,
                remaining_volume=reading.remaining_volume,
                observed_at=observed_at,
                recorded_by=recorded_by[:128],
            )
        )
        await session.flush()
        return "inserted"
    if (
        row.broker_state in FINAL_BROKER_STATES
        or row.order_uuid != reading.order_uuid
        or reading.executed_volume < Decimal(str(row.executed_volume))
    ):
        same = (
            row.order_uuid == reading.order_uuid
            and row.broker_state == reading.state
            and Decimal(str(row.executed_volume)) == reading.executed_volume
        )
        return "unchanged" if same else "refused"
    row.broker_state = reading.state
    row.executed_volume = reading.executed_volume
    row.remaining_volume = reading.remaining_volume
    row.observed_at = observed_at
    row.recorded_by = recorded_by[:128]
    await session.flush()
    return "updated"
