"""Append-only typed strategy-memory service (ROB-1115).

This module has one write operation (INSERT) and read-only projections. It has
no UPDATE/DELETE operation and imports no broker, order, fill, or scheduler
surface. PostgreSQL triggers independently reject row mutation and truncation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_backtest import ResearchStrategyExperiment
from app.models.strategy_learning_event import ResearchStrategyLearningEvent
from app.schemas.strategy_learning_event import (
    FailureFingerprint,
    StrategyLearningEventRecord,
    StrategyLearningEventRequest,
    StrategyLearningFailureClass,
    StrategyLearningPayload,
    canonical_event_request_payload,
)
from app.services.research_canonical_hash import (
    canonical_sha256,
    decode_canonical,
    encode_canonical,
)

_REQUEST_SCHEMA_ID = "strategy_learning_event_request.v1"
_EVENT_SCHEMA_ID = "strategy_learning_event.v1"


class StrategyLearningEventError(Exception):
    """Base error for strategy-memory persistence."""


class LearningEventExperimentNotFound(StrategyLearningEventError):
    """A non-null experiment id is not registered."""


class LearningEventIdempotencyConflict(StrategyLearningEventError):
    """An idempotency key was reused for a different semantic request."""


class StoredLearningEventInvalid(StrategyLearningEventError):
    """Persisted canonical data is malformed or disagrees with its hashes."""


def compute_learning_event_request_hash(
    request: StrategyLearningEventRequest,
) -> str:
    """Canonical SHA-256 over every semantic input.

    The idempotency key is deliberately excluded: it is a lookup key, while the
    request hash detects attempts to reuse that key with changed semantics.
    """
    return canonical_sha256(
        {
            "schema_id": _REQUEST_SCHEMA_ID,
            "request": canonical_event_request_payload(request),
        }
    )


def derive_memory_event_id(*, idempotency_key: str, request_hash: str) -> str:
    """Canonical identity for one append intent, using the ROB-846 authority."""
    return canonical_sha256(
        {
            "schema_id": _EVENT_SCHEMA_ID,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
        }
    )


async def _find_by_idempotency(
    session: AsyncSession, idempotency_key: str
) -> ResearchStrategyLearningEvent | None:
    return await session.scalar(
        select(ResearchStrategyLearningEvent).where(
            ResearchStrategyLearningEvent.idempotency_key == idempotency_key
        )
    )


async def _find_by_memory_event_id(
    session: AsyncSession, memory_event_id: str
) -> ResearchStrategyLearningEvent | None:
    return await session.scalar(
        select(ResearchStrategyLearningEvent).where(
            ResearchStrategyLearningEvent.memory_event_id == memory_event_id
        )
    )


def _replay_or_conflict(
    row: ResearchStrategyLearningEvent,
    *,
    request_hash: str,
    memory_event_id: str,
) -> ResearchStrategyLearningEvent:
    if row.request_hash != request_hash:
        raise LearningEventIdempotencyConflict(
            "idempotency key already belongs to a different semantic request"
        )
    if row.memory_event_id != memory_event_id:
        raise StoredLearningEventInvalid(
            "stored memory_event_id disagrees with canonical request identity"
        )
    return row


async def record_learning_event(
    session: AsyncSession,
    request: StrategyLearningEventRequest,
) -> ResearchStrategyLearningEvent:
    """Append one event or replay the existing identical idempotent write."""
    request_hash = compute_learning_event_request_hash(request)
    memory_event_id = derive_memory_event_id(
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
    )

    if request.experiment_id is not None:
        registered = await session.scalar(
            select(ResearchStrategyExperiment.experiment_id).where(
                ResearchStrategyExperiment.experiment_id == request.experiment_id
            )
        )
        if registered is None:
            raise LearningEventExperimentNotFound(
                f"experiment_id {request.experiment_id!r} is not registered; "
                "use experiment_id=None only for an explicitly unregistered track"
            )

    existing = await _find_by_idempotency(session, request.idempotency_key)
    if existing is not None:
        return _replay_or_conflict(
            existing,
            request_hash=request_hash,
            memory_event_id=memory_event_id,
        )

    row = ResearchStrategyLearningEvent(
        memory_event_id=memory_event_id,
        experiment_id=request.experiment_id,
        stage=request.stage,
        verdict=request.verdict,
        failure_class=request.failure_class,
        reason_codes=list(request.reason_codes),
        evidence_refs=list(request.evidence_refs),
        failure_fingerprint=encode_canonical(
            request.failure_fingerprint.model_dump(mode="python")
        ),
        learning_payload=encode_canonical(
            request.learning_payload.model_dump(mode="python")
        ),
        idempotency_key=request.idempotency_key,
        request_hash=request_hash,
        actor_id=request.actor_id,
        actor_role=request.actor_role,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = await _find_by_idempotency(session, request.idempotency_key)
        if winner is None:
            winner = await _find_by_memory_event_id(session, memory_event_id)
        if winner is None:
            raise
        return _replay_or_conflict(
            winner,
            request_hash=request_hash,
            memory_event_id=memory_event_id,
        )
    return row


def _decode_mapping(ast: Any, *, field: str) -> dict[str, Any]:
    try:
        decoded = decode_canonical(ast)
    except (TypeError, ValueError) as exc:
        raise StoredLearningEventInvalid(
            f"stored {field} is not a valid ROB-846 canonical AST: {exc}"
        ) from exc
    if type(decoded) is not dict:
        raise StoredLearningEventInvalid(f"stored {field} must decode to a dict")
    return decoded


def to_learning_event_record(
    row: ResearchStrategyLearningEvent,
) -> StrategyLearningEventRecord:
    """Strictly decode and verify a persisted event."""
    fingerprint_raw = _decode_mapping(
        row.failure_fingerprint, field="failure_fingerprint"
    )
    learning_raw = _decode_mapping(row.learning_payload, field="learning_payload")
    try:
        fingerprint = FailureFingerprint.model_validate(fingerprint_raw)
        learning = StrategyLearningPayload.model_validate(learning_raw)
    except ValueError as exc:
        raise StoredLearningEventInvalid(
            f"stored strategy learning event violates the typed contract: {exc}"
        ) from exc

    record = StrategyLearningEventRecord(
        memory_event_id=row.memory_event_id,
        experiment_id=row.experiment_id,
        stage=row.stage,
        verdict=row.verdict,
        failure_class=row.failure_class,
        reason_codes=list(row.reason_codes),
        evidence_refs=list(row.evidence_refs),
        failure_fingerprint=fingerprint,
        learning_payload=learning,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        created_at=row.created_at,
    )
    expected_request = StrategyLearningEventRequest(
        experiment_id=record.experiment_id,
        stage=record.stage,
        verdict=record.verdict,
        failure_class=record.failure_class,
        reason_codes=record.reason_codes,
        evidence_refs=record.evidence_refs,
        failure_fingerprint=record.failure_fingerprint,
        learning_payload=record.learning_payload,
        idempotency_key=record.idempotency_key,
        actor_id=record.actor_id,
        actor_role=record.actor_role,
    )
    expected_request_hash = compute_learning_event_request_hash(expected_request)
    expected_event_id = derive_memory_event_id(
        idempotency_key=row.idempotency_key,
        request_hash=expected_request_hash,
    )
    if (
        row.request_hash != expected_request_hash
        or row.memory_event_id != expected_event_id
    ):
        raise StoredLearningEventInvalid(
            "stored learning event hashes disagree with its canonical payload"
        )
    return record


async def get_memory(
    session: AsyncSession, experiment_id: str
) -> list[StrategyLearningEventRecord]:
    """Return one experiment's complete memory in append order."""
    rows = (
        await session.scalars(
            select(ResearchStrategyLearningEvent)
            .where(ResearchStrategyLearningEvent.experiment_id == experiment_id)
            .order_by(
                ResearchStrategyLearningEvent.created_at,
                ResearchStrategyLearningEvent.id,
            )
        )
    ).all()
    return [to_learning_event_record(row) for row in rows]


async def get_lineage(
    session: AsyncSession, strategy_key: str
) -> list[StrategyLearningEventRecord]:
    """Return registered lineage memory ordered parent-version then append order."""
    rows = (
        await session.scalars(
            select(ResearchStrategyLearningEvent)
            .join(
                ResearchStrategyExperiment,
                ResearchStrategyExperiment.experiment_id
                == ResearchStrategyLearningEvent.experiment_id,
            )
            .where(ResearchStrategyExperiment.strategy_key == strategy_key)
            .order_by(
                ResearchStrategyExperiment.created_at,
                ResearchStrategyExperiment.id,
                ResearchStrategyLearningEvent.created_at,
                ResearchStrategyLearningEvent.id,
            )
        )
    ).all()
    return [to_learning_event_record(row) for row in rows]


async def search_failures(
    session: AsyncSession,
    *,
    market: str,
    horizon: str,
    failure_class: StrategyLearningFailureClass,
) -> list[StrategyLearningEventRecord]:
    """Search exact failure fingerprints, newest first.

    The fingerprint stays in canonical AST form instead of duplicating market /
    horizon as top-level columns not present in the issue spec. P0 therefore
    narrows by indexed ``failure_class`` in SQL and applies the two exact typed
    fingerprint dimensions after strict decode.
    """
    rows = (
        await session.scalars(
            select(ResearchStrategyLearningEvent)
            .where(ResearchStrategyLearningEvent.failure_class == failure_class)
            .order_by(
                ResearchStrategyLearningEvent.created_at.desc(),
                ResearchStrategyLearningEvent.id.desc(),
            )
        )
    ).all()
    records = [to_learning_event_record(row) for row in rows]
    return [
        record
        for record in records
        if record.failure_fingerprint.market == market
        and record.failure_fingerprint.horizon == horizon
    ]


__all__ = [
    "LearningEventExperimentNotFound",
    "LearningEventIdempotencyConflict",
    "StoredLearningEventInvalid",
    "StrategyLearningEventError",
    "compute_learning_event_request_hash",
    "derive_memory_event_id",
    "get_lineage",
    "get_memory",
    "record_learning_event",
    "search_failures",
    "to_learning_event_record",
]
