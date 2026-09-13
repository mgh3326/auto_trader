"""Durable, per-UUID context consumption with no economic authority."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.models.fill_watch_context_outcome import FillWatchContextOutcome
from app.services.fill_watch_context.contracts import ContextArtifact
from app.services.fill_watch_context.domain import (
    ContextReason,
    ContextualStatus,
    NextAction,
    project_context_outcome,
)
from app.services.fill_watch_context.repository import FillWatchContextOutcomeRepository

__all__ = [
    "CONTEXT_RESPONSIBLE_CONSUMER",
    "ConsumptionReceipt",
    "DeliveryDisposition",
    "FillWatchContextOutcomeService",
    "StoredContextOutcome",
]

CONTEXT_RESPONSIBLE_CONSUMER = "fill_watch_context"


class DeliveryDisposition(StrEnum):
    """Transport-level result, deliberately separate from contextual status."""

    PERSISTED = "persisted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class StoredContextOutcome:
    """JSON-safe projection of the individually queryable durable row."""

    responsible_consumer: str
    input_as_of: datetime
    event_kind: str
    transport_event_uuid: str
    transport_event_id: str
    economic_root_ref: str
    order_refs: tuple[str, ...]
    contextual_status: str
    reason: str
    next_action: str
    delivery_count: int
    conflict_count: int

    @classmethod
    def from_row(cls, row: FillWatchContextOutcome) -> StoredContextOutcome:
        return cls(
            responsible_consumer=row.responsible_consumer,
            input_as_of=row.input_as_of,
            event_kind=row.event_kind,
            transport_event_uuid=str(row.transport_event_uuid),
            transport_event_id=row.transport_event_id,
            economic_root_ref=row.economic_root_ref,
            order_refs=tuple(str(value) for value in row.order_refs),
            contextual_status=row.contextual_status,
            reason=row.reason,
            next_action=row.next_action,
            delivery_count=row.delivery_count,
            conflict_count=row.conflict_count,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "responsible_consumer": self.responsible_consumer,
            "input_as_of": self.input_as_of.isoformat(),
            "event_kind": self.event_kind,
            "transport_event_uuid": self.transport_event_uuid,
            "transport_event_id": self.transport_event_id,
            "economic_root_ref": self.economic_root_ref,
            "order_refs": list(self.order_refs),
            "contextual_status": self.contextual_status,
            "reason": self.reason,
            "next_action": self.next_action,
            "delivery_count": self.delivery_count,
            "conflict_count": self.conflict_count,
        }


@dataclass(frozen=True)
class ConsumptionReceipt:
    """One transport ACK plus the persisted business-context outcome."""

    disposition: DeliveryDisposition
    outcome: StoredContextOutcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_ack": {
                "accepted": True,
                "persisted": self.disposition is DeliveryDisposition.PERSISTED,
                "disposition": self.disposition.value,
                "transport_event_uuid": self.outcome.transport_event_uuid,
            },
            "consumption": self.outcome.as_dict(),
        }


def _semantic_digest(artifact: ContextArtifact) -> str:
    """Digest semantic inputs, excluding incidental optional routing metadata."""
    lane_event = artifact.lane_event
    payload = {
        "lane_event": {
            "kind": lane_event.kind,
            "lane": lane_event.lane,
            "event_id": lane_event.event_id,
            "text": lane_event.text,
        },
        "context": artifact.context.model_dump(mode="json", exclude_none=True),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FillWatchContextOutcomeService:
    """Own all writes to ``review.fill_watch_context_outcomes``.

    A transaction commits exactly one UUID outcome. Replays increment delivery
    accounting. A same-UUID semantic conflict is intentionally fail-closed:
    it updates the existing logical outcome to ``needs_human`` rather than
    inserting a second row or silently choosing one artifact.
    """

    def __init__(self, repository: FillWatchContextOutcomeRepository) -> None:
        self._repository = repository

    async def consume(self, artifact: ContextArtifact) -> ConsumptionReceipt:
        event_uuid = uuid.UUID(artifact.lane_event.event_id)
        digest = _semantic_digest(artifact)
        existing = await self._repository.get_by_transport_uuid(
            event_uuid,
            for_update=True,
        )
        if existing is not None:
            return await self._record_replay(existing, digest)

        projection = project_context_outcome(artifact)
        context = artifact.context
        row = FillWatchContextOutcome(
            transport_event_uuid=event_uuid,
            transport_event_id=artifact.lane_event.event_id,
            responsible_consumer=CONTEXT_RESPONSIBLE_CONSUMER,
            event_kind=context.event_kind,
            input_as_of=context.input_as_of,
            economic_root_ref=context.economic_root_ref,
            order_refs=list(context.order_refs),
            contextual_status=projection.contextual_status.value,
            reason=projection.reason.value,
            next_action=projection.next_action.value,
            semantic_digest=digest,
            delivery_count=1,
            conflict_count=0,
        )
        self._repository.add(row)
        try:
            await self._repository.session.commit()
        except IntegrityError:
            # A concurrent owner inserted this UUID after our initial read.
            # The database uniqueness constraint remains the authority.
            await self._repository.session.rollback()
            winner = await self._repository.get_by_transport_uuid(
                event_uuid,
                for_update=True,
            )
            if winner is None:  # pragma: no cover - defensive database anomaly
                raise
            return await self._record_replay(winner, digest)
        return ConsumptionReceipt(
            disposition=DeliveryDisposition.PERSISTED,
            outcome=StoredContextOutcome.from_row(row),
        )

    async def get(self, transport_event_uuid: str) -> StoredContextOutcome | None:
        """Read one outcome by its canonical UUID; no account/broker lookup."""
        row = await self._repository.get_by_transport_uuid(
            uuid.UUID(transport_event_uuid)
        )
        return StoredContextOutcome.from_row(row) if row is not None else None

    async def outcomes_for_economic_root(
        self, economic_root_ref: str
    ) -> list[StoredContextOutcome]:
        """Coalesce at read time without sacrificing UUID-level outcomes."""
        rows = await self._repository.list_by_economic_root(economic_root_ref)
        return [StoredContextOutcome.from_row(row) for row in rows]

    async def _record_replay(
        self, row: FillWatchContextOutcome, digest: str
    ) -> ConsumptionReceipt:
        row.delivery_count += 1
        if row.semantic_digest == digest:
            disposition = DeliveryDisposition.DUPLICATE
        else:
            # Never overwrite the original root/order references with a
            # conflicting replay. The safe terminal context is human review.
            row.contextual_status = ContextualStatus.NEEDS_HUMAN.value
            row.reason = ContextReason.EVENT_UUID_CONFLICT.value
            row.next_action = NextAction.OPERATOR_REVIEW.value
            row.conflict_count += 1
            disposition = DeliveryDisposition.CONFLICT
        await self._repository.session.commit()
        return ConsumptionReceipt(
            disposition=disposition,
            outcome=StoredContextOutcome.from_row(row),
        )
