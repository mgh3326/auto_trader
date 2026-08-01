"""Internal SQLAlchemy repository for the ROB-1195 service boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.fill_observation import (
    FillObservation,
    FillProjectionCursor,
    FillProjectionOutbox,
    FillSettlementEnrichment,
)
from app.services.fill_observation.contracts import (
    FillObservationIdentity,
    NormalizedBrokerFillEvidence,
    NormalizedFillSettlement,
)
from app.services.fill_observation.identity import derive_projection_delivery_key


class FillObservationRepository:
    """The only ORM construction surface for immutable fill observations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def lock_order_scope(self, lock_key: int) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def find_by_identity(
        self,
        observation_identity: str,
    ) -> FillObservation | None:
        return await self.session.scalar(
            select(FillObservation).where(
                FillObservation.observation_identity == observation_identity
            )
        )

    async def recorded_quantity(
        self,
        evidence: NormalizedBrokerFillEvidence,
    ) -> Decimal:
        value = await self.session.scalar(
            select(func.coalesce(func.sum(FillObservation.fill_delta_quantity), 0))
            .where(FillObservation.broker == evidence.broker)
            .where(FillObservation.account_ref == evidence.account_ref)
            .where(FillObservation.account_mode == evidence.account_mode)
            .where(FillObservation.venue == evidence.venue)
            .where(FillObservation.order_id == evidence.order_id)
        )
        return Decimal(str(value or 0))

    async def append(
        self,
        *,
        evidence: NormalizedBrokerFillEvidence,
        identity: FillObservationIdentity,
        fill_delta_quantity: Decimal,
        projection_names: tuple[str, ...],
    ) -> tuple[FillObservation, int]:
        observation = FillObservation(
            observation_identity=identity.value,
            identity_kind=identity.kind,
            broker=evidence.broker,
            account_ref=evidence.account_ref,
            account_mode=evidence.account_mode,
            venue=evidence.venue,
            order_id=evidence.order_id,
            instrument_type=evidence.instrument_type,
            symbol=evidence.symbol,
            side=evidence.side,
            currency=evidence.currency,
            broker_fill_sequence=evidence.broker_fill_sequence,
            cumulative_quantity=evidence.cumulative_quantity,
            reported_fill_quantity=evidence.fill_quantity,
            fill_delta_quantity=fill_delta_quantity,
            average_price=evidence.average_price,
            last_fill_price=evidence.last_fill_price,
            cumulative_notional=evidence.cumulative_notional,
            fee_total=evidence.fee_total,
            evidence_source=evidence.evidence_source,
            evidence_ref=evidence.evidence_ref,
            fill_fact_hash=identity.fill_fact_hash,
            observed_at=evidence.observed_at,
            filled_at=evidence.filled_at,
            correlation_id=evidence.correlation_id,
        )
        self.session.add(observation)
        await self.session.flush()

        outbox_rows = [
            FillProjectionOutbox(
                delivery_key=derive_projection_delivery_key(
                    projection_name=projection_name,
                    observation_identity=identity.value,
                ),
                projection_name=projection_name,
                partition_key=identity.partition_key,
                fill_observation_id=observation.id,
                state="pending",
                attempt_count=0,
            )
            for projection_name in projection_names
        ]
        self.session.add_all(outbox_rows)
        await self.session.flush()
        return observation, len(outbox_rows)

    async def latest_settlement(
        self,
        fill_observation_id: int,
    ) -> FillSettlementEnrichment | None:
        """Return the highest settlement revision for one observation."""
        return await self.session.scalar(
            select(FillSettlementEnrichment)
            .where(FillSettlementEnrichment.fill_observation_id == fill_observation_id)
            .order_by(FillSettlementEnrichment.revision.desc())
            .limit(1)
        )

    async def append_settlement(
        self,
        *,
        fill_observation_id: int,
        evidence: NormalizedBrokerFillEvidence,
        settlement: NormalizedFillSettlement,
        revision: int,
    ) -> FillSettlementEnrichment:
        """Append one settlement revision without touching the observation."""
        enrichment = FillSettlementEnrichment(
            fill_observation_id=fill_observation_id,
            revision=revision,
            settlement_hash=settlement.settlement_hash,
            cumulative_quantity=settlement.cumulative_quantity,
            reported_fill_quantity=settlement.reported_fill_quantity,
            average_price=settlement.average_price,
            last_fill_price=settlement.last_fill_price,
            cumulative_notional=settlement.cumulative_notional,
            fee_total=settlement.fee_total,
            filled_at=settlement.filled_at,
            evidence_source=evidence.evidence_source,
            evidence_ref=evidence.evidence_ref,
            observed_at=evidence.observed_at,
        )
        self.session.add(enrichment)
        await self.session.flush()
        return enrichment


class FillProjectionRepository:
    """Mutable outbox/cursor primitives; callers own the transaction."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim_ready(
        self,
        *,
        projection_name: str,
        now: datetime,
        lease_token: uuid.UUID,
        lease_expires_at: datetime,
        limit: int,
    ) -> list[FillProjectionOutbox]:
        predecessor = aliased(FillProjectionOutbox)
        eligible = or_(
            and_(
                FillProjectionOutbox.state.in_(("pending", "retry")),
                FillProjectionOutbox.available_at <= now,
            ),
            and_(
                FillProjectionOutbox.state == "processing",
                FillProjectionOutbox.lease_expires_at <= now,
            ),
        )
        unfinished_predecessor = (
            select(predecessor.id)
            .where(predecessor.projection_name == FillProjectionOutbox.projection_name)
            .where(predecessor.partition_key == FillProjectionOutbox.partition_key)
            .where(
                predecessor.fill_observation_id
                < FillProjectionOutbox.fill_observation_id
            )
            .where(predecessor.state != "succeeded")
            .exists()
        )
        rows = list(
            (
                await self.session.scalars(
                    select(FillProjectionOutbox)
                    .where(FillProjectionOutbox.projection_name == projection_name)
                    .where(eligible)
                    .where(~unfinished_predecessor)
                    .order_by(FillProjectionOutbox.fill_observation_id.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for row in rows:
            row.state = "processing"
            row.attempt_count += 1
            row.lease_token = lease_token
            row.lease_expires_at = lease_expires_at
            row.completed_at = None
            row.updated_at = now
        await self.session.flush()
        return rows

    async def get_outbox_for_update(
        self, outbox_id: int
    ) -> FillProjectionOutbox | None:
        return await self.session.scalar(
            select(FillProjectionOutbox)
            .where(FillProjectionOutbox.id == outbox_id)
            .with_for_update()
        )

    async def get_observation(self, observation_id: int) -> FillObservation | None:
        return await self.session.get(FillObservation, observation_id)

    async def lock_projection_partition(self, lock_key: int) -> None:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    async def get_cursor_for_update(
        self,
        *,
        projection_name: str,
        partition_key: str,
    ) -> FillProjectionCursor | None:
        return await self.session.scalar(
            select(FillProjectionCursor)
            .where(FillProjectionCursor.projection_name == projection_name)
            .where(FillProjectionCursor.partition_key == partition_key)
            .with_for_update()
        )

    def add_cursor(
        self,
        *,
        projection_name: str,
        partition_key: str,
        observation: FillObservation,
        now: datetime,
    ) -> FillProjectionCursor:
        cursor = FillProjectionCursor(
            projection_name=projection_name,
            partition_key=partition_key,
            last_fill_observation_id=observation.id,
            last_observation_identity=observation.observation_identity,
            advanced_at=now,
            updated_at=now,
        )
        self.session.add(cursor)
        return cursor

    async def flush(self) -> None:
        await self.session.flush()


__all__ = ["FillObservationRepository", "FillProjectionRepository"]
