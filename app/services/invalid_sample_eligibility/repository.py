"""ROB-1036 — SQLAlchemy access for the eligibility / cleanup-binding tables.

Service-internal. Import it from
``app.services.invalid_sample_eligibility.service`` only; the static boundary
test rejects any other importer, following the same service-private repository
rule as ROB-298 and ROB-816.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invalid_sample_eligibility import (
    InvalidSampleCleanupBinding,
    InvalidSampleCleanupLifecycleEvent,
    SampleEligibilityDecision,
)


class InvalidSampleEligibilityRepository:
    """Reads and appends; it never updates or deletes a row."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_decisions(
        self, *, subject_kind: str, subject_ref: str
    ) -> list[SampleEligibilityDecision]:
        result = await self._db.execute(
            select(SampleEligibilityDecision)
            .where(
                SampleEligibilityDecision.subject_kind == subject_kind,
                SampleEligibilityDecision.subject_ref == subject_ref,
            )
            .order_by(SampleEligibilityDecision.revision_no)
        )
        return list(result.scalars().all())

    async def list_decisions_for_refs(
        self, *, subject_kind: str, subject_refs: Sequence[str]
    ) -> list[SampleEligibilityDecision]:
        if not subject_refs:
            return []
        result = await self._db.execute(
            select(SampleEligibilityDecision)
            .where(
                SampleEligibilityDecision.subject_kind == subject_kind,
                SampleEligibilityDecision.subject_ref.in_(list(subject_refs)),
            )
            .order_by(
                SampleEligibilityDecision.subject_ref,
                SampleEligibilityDecision.revision_no,
            )
        )
        return list(result.scalars().all())

    async def add_decision(self, values: dict[str, Any]) -> SampleEligibilityDecision:
        row = SampleEligibilityDecision(**values)
        self._db.add(row)
        await self._db.flush()
        return row

    async def get_binding_by_hash(
        self, binding_hash: str
    ) -> InvalidSampleCleanupBinding | None:
        result = await self._db.execute(
            select(InvalidSampleCleanupBinding).where(
                InvalidSampleCleanupBinding.binding_hash == binding_hash
            )
        )
        return result.scalars().first()

    async def get_binding_by_client_order_id(
        self, client_order_id: str
    ) -> InvalidSampleCleanupBinding | None:
        result = await self._db.execute(
            select(InvalidSampleCleanupBinding).where(
                InvalidSampleCleanupBinding.client_order_id == client_order_id
            )
        )
        return result.scalars().first()

    async def add_binding(self, values: dict[str, Any]) -> InvalidSampleCleanupBinding:
        row = InvalidSampleCleanupBinding(**values)
        self._db.add(row)
        await self._db.flush()
        return row

    async def get_lifecycle_event(
        self, *, binding_hash: str, event_kind: str, evidence_hash: str
    ) -> InvalidSampleCleanupLifecycleEvent | None:
        result = await self._db.execute(
            select(InvalidSampleCleanupLifecycleEvent).where(
                InvalidSampleCleanupLifecycleEvent.binding_hash == binding_hash,
                InvalidSampleCleanupLifecycleEvent.event_kind == event_kind,
                InvalidSampleCleanupLifecycleEvent.evidence_hash == evidence_hash,
            )
        )
        return result.scalars().first()

    async def add_lifecycle_event(
        self, values: dict[str, Any]
    ) -> InvalidSampleCleanupLifecycleEvent:
        row = InvalidSampleCleanupLifecycleEvent(**values)
        self._db.add(row)
        await self._db.flush()
        return row


__all__ = ["InvalidSampleEligibilityRepository"]
