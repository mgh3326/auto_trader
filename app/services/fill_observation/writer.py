"""Default-off transactional writer for immutable fill observations."""

from __future__ import annotations

import os
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.fill_observation import FillObservation, FillSettlementEnrichment
from app.services.fill_observation.contracts import (
    BrokerFillEvidence,
    FillObservationIdentity,
    FillObservationWriteResult,
    FillObservationWriteStatus,
    FillSettlementStatus,
    NormalizedBrokerFillEvidence,
    NormalizedFillSettlement,
)
from app.services.fill_observation.errors import (
    FillObservationIdentityConflict,
    InvalidFillEvidence,
    NonMonotonicFillCumulative,
)
from app.services.fill_observation.identity import (
    derive_fill_observation_identity,
    has_positive_fill,
    normalize_fill_evidence,
)
from app.services.fill_observation.repository import FillObservationRepository

FILL_OBSERVATION_WRITER_ENABLED_ENV = "FILL_OBSERVATION_WRITER_ENABLED"
DEFAULT_FILL_PROJECTIONS: tuple[str, ...] = ("legacy_dual_read_validation.v1",)


class _ObservationRepository(Protocol):
    async def lock_order_scope(self, lock_key: int) -> None: ...

    async def find_by_identity(
        self,
        observation_identity: str,
    ) -> FillObservation | None: ...

    async def recorded_quantity(
        self,
        evidence: NormalizedBrokerFillEvidence,
    ) -> Decimal: ...

    async def append(
        self,
        *,
        evidence: NormalizedBrokerFillEvidence,
        identity: FillObservationIdentity,
        fill_delta_quantity: Decimal,
        projection_names: tuple[str, ...],
    ) -> tuple[FillObservation, int]: ...

    async def latest_settlement(
        self,
        fill_observation_id: int,
    ) -> FillSettlementEnrichment | None: ...

    async def append_settlement(
        self,
        *,
        fill_observation_id: int,
        evidence: NormalizedBrokerFillEvidence,
        settlement: NormalizedFillSettlement,
        revision: int,
    ) -> FillSettlementEnrichment: ...


def fill_observation_writer_enabled() -> bool:
    """Read the additive rollback flag; absence and unknown values are off."""
    return os.getenv(FILL_OBSERVATION_WRITER_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _normalize_projection_names(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        if not name:
            raise InvalidFillEvidence("projection_name must not be blank")
        if len(name) > 128:
            raise InvalidFillEvidence("projection_name exceeds 128 characters")
        if name not in seen:
            normalized.append(name)
            seen.add(name)
    if not normalized:
        raise InvalidFillEvidence("at least one projection_name is required")
    return tuple(normalized)


async def _record_settlement(
    repository: _ObservationRepository,
    *,
    fill_observation_id: int,
    evidence: NormalizedBrokerFillEvidence,
    settlement: NormalizedFillSettlement,
) -> tuple[FillSettlementStatus, int | None]:
    """Append a settlement revision only when the values actually changed.

    This never updates the immutable observation and never produces a fill
    delta. The caller already holds the order-scope advisory lock, so the
    revision number is assigned under serialized access for the order.
    """
    if not settlement.has_values:
        return FillSettlementStatus.ABSENT, None
    latest = await repository.latest_settlement(fill_observation_id)
    if latest is not None and latest.settlement_hash == settlement.settlement_hash:
        return FillSettlementStatus.UNCHANGED, latest.revision
    revision = 1 if latest is None else latest.revision + 1
    await repository.append_settlement(
        fill_observation_id=fill_observation_id,
        evidence=evidence,
        settlement=settlement,
        revision=revision,
    )
    return FillSettlementStatus.RECORDED, revision


async def _record_with_repository(
    repository: _ObservationRepository,
    *,
    evidence: NormalizedBrokerFillEvidence,
    identity: FillObservationIdentity,
    projection_names: tuple[str, ...],
) -> FillObservationWriteResult:
    """Execute one append plan inside the caller's open transaction."""
    await repository.lock_order_scope(identity.order_lock_key)
    existing = await repository.find_by_identity(identity.value)
    if existing is not None:
        if existing.fill_fact_hash != identity.fill_fact_hash:
            raise FillObservationIdentityConflict(
                "fill observation identity already has a different broker fill fact"
            )
        # The stable fill fact matches, so this is the same fill re-observed.
        # Settlement drift is preserved as a new revision instead of a conflict.
        settlement_status, settlement_revision = await _record_settlement(
            repository,
            fill_observation_id=existing.id,
            evidence=evidence,
            settlement=identity.settlement,
        )
        return FillObservationWriteResult(
            status=FillObservationWriteStatus.DUPLICATE,
            observation_identity=identity.value,
            observation_id=existing.id,
            fill_delta_quantity=Decimal(0),
            outbox_count=0,
            settlement_status=settlement_status,
            settlement_revision=settlement_revision,
        )

    if evidence.cumulative_quantity is not None:
        recorded_quantity = await repository.recorded_quantity(evidence)
        if evidence.cumulative_quantity < recorded_quantity:
            raise NonMonotonicFillCumulative(
                "broker cumulative fill quantity regressed below durable deltas"
            )
        delta = evidence.cumulative_quantity - recorded_quantity
    else:
        delta = evidence.fill_quantity or Decimal(0)

    if delta <= 0:
        return FillObservationWriteResult(
            status=FillObservationWriteStatus.NO_DELTA,
            observation_identity=identity.value,
            observation_id=None,
            fill_delta_quantity=Decimal(0),
            outbox_count=0,
        )

    observation, outbox_count = await repository.append(
        evidence=evidence,
        identity=identity,
        fill_delta_quantity=delta,
        projection_names=projection_names,
    )
    settlement_status, settlement_revision = await _record_settlement(
        repository,
        fill_observation_id=observation.id,
        evidence=evidence,
        settlement=identity.settlement,
    )
    return FillObservationWriteResult(
        status=FillObservationWriteStatus.INSERTED,
        observation_identity=identity.value,
        observation_id=observation.id,
        fill_delta_quantity=delta,
        outbox_count=outbox_count,
        settlement_status=settlement_status,
        settlement_revision=settlement_revision,
    )


class FillObservationWriter:
    """Own the atomic observation + outbox transaction.

    No existing reconcile path instantiates this writer in ROB-1195. The
    additive env flag is false by default so deploying the foundation performs
    zero writes until a separately approved consumer migration wires it.
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] = AsyncSessionLocal,
        *,
        enabled: bool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._enabled = (
            fill_observation_writer_enabled() if enabled is None else enabled
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def write(
        self,
        evidence: BrokerFillEvidence,
        *,
        projection_names: tuple[str, ...] = DEFAULT_FILL_PROJECTIONS,
    ) -> FillObservationWriteResult:
        normalized = normalize_fill_evidence(evidence)
        identity = derive_fill_observation_identity(normalized)
        if not has_positive_fill(normalized):
            return FillObservationWriteResult(
                status=FillObservationWriteStatus.NO_FILL_EVIDENCE,
                observation_identity=identity.value,
                observation_id=None,
                fill_delta_quantity=Decimal(0),
                outbox_count=0,
            )
        if not self._enabled:
            return FillObservationWriteResult(
                status=FillObservationWriteStatus.WRITER_DISABLED,
                observation_identity=identity.value,
                observation_id=None,
                fill_delta_quantity=Decimal(0),
                outbox_count=0,
            )

        normalized_projections = _normalize_projection_names(projection_names)
        async with self._session_factory() as session:
            async with session.begin():
                repository = FillObservationRepository(session)
                return await _record_with_repository(
                    repository,
                    evidence=normalized,
                    identity=identity,
                    projection_names=normalized_projections,
                )


__all__ = [
    "DEFAULT_FILL_PROJECTIONS",
    "FILL_OBSERVATION_WRITER_ENABLED_ENV",
    "FillObservationWriter",
    "fill_observation_writer_enabled",
]
