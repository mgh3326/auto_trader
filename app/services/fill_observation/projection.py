"""Durable claim/retry/cursor operations for future fill projections."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.fill_observation.contracts import FillProjectionDelivery
from app.services.fill_observation.errors import (
    FillProjectionCursorRegression,
    FillProjectionDeliveryError,
    FillProjectionLeaseMismatch,
)
from app.services.fill_observation.identity import derive_projection_lock_key
from app.services.fill_observation.repository import FillProjectionRepository


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _projection_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128:
        raise FillProjectionDeliveryError(
            "projection_name must contain 1 to 128 characters"
        )
    return normalized


class FillProjectionQueue:
    """Service-layer writer for only the new outbox/cursor tables.

    This class is intentionally not wired to a scheduler or existing consumer.
    A future projection worker can claim, retry, and complete deliveries while
    preserving the observation row as immutable authority.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
        *,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._token_factory = token_factory

    async def claim(
        self,
        *,
        projection_name: str,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[FillProjectionDelivery]:
        projection = _projection_name(projection_name)
        if not 1 <= limit <= 500:
            raise FillProjectionDeliveryError("limit must be between 1 and 500")
        if not 1 <= lease_seconds <= 3600:
            raise FillProjectionDeliveryError(
                "lease_seconds must be between 1 and 3600"
            )

        now = self._clock()
        token = self._token_factory()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                repository = FillProjectionRepository(session)
                rows = await repository.claim_ready(
                    projection_name=projection,
                    now=now,
                    lease_token=token,
                    lease_expires_at=lease_expires_at,
                    limit=limit,
                )
                deliveries: list[FillProjectionDelivery] = []
                for row in rows:
                    observation = await repository.get_observation(
                        row.fill_observation_id
                    )
                    if observation is None:
                        raise FillProjectionDeliveryError(
                            "outbox row references a missing fill observation"
                        )
                    deliveries.append(
                        FillProjectionDelivery(
                            outbox_id=row.id,
                            delivery_key=row.delivery_key,
                            projection_name=row.projection_name,
                            partition_key=row.partition_key,
                            fill_observation_id=row.fill_observation_id,
                            observation_identity=observation.observation_identity,
                            attempt_count=row.attempt_count,
                            lease_token=token,
                        )
                    )
                return deliveries

    async def complete(
        self,
        *,
        outbox_id: int,
        lease_token: uuid.UUID,
    ) -> None:
        now = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                repository = FillProjectionRepository(session)
                outbox = await repository.get_outbox_for_update(outbox_id)
                if (
                    outbox is None
                    or outbox.state != "processing"
                    or outbox.lease_token != lease_token
                ):
                    raise FillProjectionLeaseMismatch(
                        "outbox completion does not own the active lease"
                    )
                observation = await repository.get_observation(
                    outbox.fill_observation_id
                )
                if observation is None:
                    raise FillProjectionDeliveryError(
                        "outbox row references a missing fill observation"
                    )

                lock_key = derive_projection_lock_key(
                    projection_name=outbox.projection_name,
                    partition_key=outbox.partition_key,
                )
                await repository.lock_projection_partition(lock_key)
                cursor = await repository.get_cursor_for_update(
                    projection_name=outbox.projection_name,
                    partition_key=outbox.partition_key,
                )
                if cursor is None:
                    repository.add_cursor(
                        projection_name=outbox.projection_name,
                        partition_key=outbox.partition_key,
                        observation=observation,
                        now=now,
                    )
                elif observation.id > cursor.last_fill_observation_id:
                    cursor.last_fill_observation_id = observation.id
                    cursor.last_observation_identity = observation.observation_identity
                    cursor.advanced_at = now
                    cursor.updated_at = now
                elif (
                    observation.id == cursor.last_fill_observation_id
                    and observation.observation_identity
                    != cursor.last_observation_identity
                ):
                    raise FillProjectionDeliveryError(
                        "projection cursor identity disagrees with its observation"
                    )
                elif observation.id < cursor.last_fill_observation_id:
                    raise FillProjectionCursorRegression(
                        "projection delivery is older than the durable cursor"
                    )

                outbox.state = "succeeded"
                outbox.lease_token = None
                outbox.lease_expires_at = None
                outbox.last_error = None
                outbox.completed_at = now
                outbox.updated_at = now
                await repository.flush()

    async def retry(
        self,
        *,
        outbox_id: int,
        lease_token: uuid.UUID,
        error: str,
        retry_after_seconds: int = 60,
    ) -> None:
        if not 0 <= retry_after_seconds <= 86_400:
            raise FillProjectionDeliveryError(
                "retry_after_seconds must be between 0 and 86400"
            )
        normalized_error = str(error or "").strip()
        if not normalized_error:
            raise FillProjectionDeliveryError("retry error must not be blank")

        now = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                repository = FillProjectionRepository(session)
                outbox = await repository.get_outbox_for_update(outbox_id)
                if (
                    outbox is None
                    or outbox.state != "processing"
                    or outbox.lease_token != lease_token
                ):
                    raise FillProjectionLeaseMismatch(
                        "outbox retry does not own the active lease"
                    )
                outbox.state = "retry"
                outbox.lease_token = None
                outbox.lease_expires_at = None
                outbox.last_error = normalized_error[:4000]
                outbox.completed_at = None
                outbox.available_at = now + timedelta(seconds=retry_after_seconds)
                outbox.updated_at = now
                await repository.flush()


__all__ = ["FillProjectionQueue"]
