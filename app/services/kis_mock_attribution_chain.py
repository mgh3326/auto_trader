"""Read-only chain query: one correlation_id -> every stage that recorded it.

Read-only by construction. The module imports no insert/update/delete helper
and holds no session factory of its own; callers hand in a session. It exists
so "attribution is 100%" is a query someone can run and disagree with, rather
than a claim.

A chain is complete when the signal row, the order row, and at least one
lifecycle transition all resolve from the same key. Anything else is reported
as a named gap — a missing stage is the finding, not an empty result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISMockOrderLedger, KISMockSignalLedger

# Gap codes. Stable strings — tests and runbooks reference them by name.
GAP_SIGNAL_MISSING = "signal_missing"
GAP_ORDER_MISSING = "order_missing"
GAP_ORDER_UNATTRIBUTED = "order_unattributed"
GAP_RECONCILE_MISSING = "reconcile_missing"

# A row that never left the pre-send stages cannot be expected to carry a
# reconcile transition, so its absence is not a gap.
_PRE_RECONCILE_STATES = frozenset({"planned", "previewed", "failed", "anomaly"})


@dataclass(frozen=True, slots=True)
class ChainStage:
    name: str
    present: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttributionChain:
    correlation_id: str
    stages: tuple[ChainStage, ...]
    gaps: tuple[str, ...]
    strategy: str | None = None

    @property
    def unbroken(self) -> bool:
        return not self.gaps

    def stage(self, name: str) -> ChainStage | None:
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None


async def load_attribution_chain(
    db: AsyncSession, *, correlation_id: str
) -> AttributionChain:
    """Assemble every stage recorded under ``correlation_id``."""
    signal = await db.scalar(
        select(KISMockSignalLedger).where(
            KISMockSignalLedger.correlation_id == correlation_id
        )
    )
    order_rows = list(
        (
            await db.execute(
                select(KISMockOrderLedger)
                .where(KISMockOrderLedger.correlation_id == correlation_id)
                .order_by(KISMockOrderLedger.id.asc())
            )
        )
        .scalars()
        .all()
    )

    stages: list[ChainStage] = []
    gaps: list[str] = []

    stages.append(
        ChainStage(
            name="signal",
            present=signal is not None,
            detail=(
                {}
                if signal is None
                else {
                    "signal_ledger_id": signal.id,
                    "strategy": signal.strategy,
                    "signal_source": signal.signal_source,
                    "decision": signal.decision,
                    "outcome_state": signal.outcome_state,
                    "suppressed_reason": signal.suppressed_reason,
                }
            ),
        )
    )
    if signal is None:
        gaps.append(GAP_SIGNAL_MISSING)

    stages.append(
        ChainStage(
            name="order",
            present=bool(order_rows),
            detail={
                "ledger_ids": [row.id for row in order_rows],
                "order_nos": [row.order_no for row in order_rows],
                "lifecycle_states": [row.lifecycle_state for row in order_rows],
                "strategies": [row.strategy for row in order_rows],
            },
        )
    )

    # A signal that deliberately produced no order is a complete chain, not a
    # broken one — that is exactly the "decided not to trade" evidence the
    # signal ledger exists to keep.
    expects_order = signal is None or signal.decision == "order"
    if expects_order and not order_rows:
        gaps.append(GAP_ORDER_MISSING)
    if order_rows and any(not (row.strategy or "").strip() for row in order_rows):
        gaps.append(GAP_ORDER_UNATTRIBUTED)

    reconciled = [
        row
        for row in order_rows
        if (row.last_reconcile_detail or {}) or row.reconciled_at
    ]
    stages.append(
        ChainStage(
            name="reconcile",
            present=bool(reconciled),
            detail={
                "ledger_ids": [row.id for row in reconciled],
                "reconcile_details": [row.last_reconcile_detail for row in reconciled],
                "reconciled_at": [
                    row.reconciled_at.isoformat() if row.reconciled_at else None
                    for row in reconciled
                ],
            },
        )
    )
    awaiting_reconcile = [
        row for row in order_rows if row.lifecycle_state not in _PRE_RECONCILE_STATES
    ]
    if awaiting_reconcile and not reconciled:
        gaps.append(GAP_RECONCILE_MISSING)

    return AttributionChain(
        correlation_id=correlation_id,
        stages=tuple(stages),
        gaps=tuple(gaps),
        strategy=(
            signal.strategy
            if signal is not None
            else next(
                (row.strategy for row in order_rows if (row.strategy or "").strip()),
                None,
            )
        ),
    )
