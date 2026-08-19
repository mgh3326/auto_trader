"""ROB-1286 §101차 ① — durable repricing claims (additive).

One row = one attempt to re-judge one watch fire. Additive: no existing
table or column changes, and ``review.investment_watch_events`` stays
read-only to this feature.

Three constraints carry the safety, and each closes a specific r2 finding:

``uq_watch_event_repricing_claims_event_generation``
    ``UNIQUE (event_uuid, generation)``. A generation can be claimed once,
    so two ticks racing on the same fire cannot both win -- the loser gets
    an ``IntegrityError``, not a second claim.

``uq_watch_event_repricing_claims_active_symbol``
    ``UNIQUE (symbol) WHERE state = 'started'``. r2 NEW BLOCKER 2: the
    in-memory store enforced per-symbol concurrency by reading an
    ``active_symbols()`` snapshot, so two ticks that both saw an empty
    snapshot both spawned on ``005930``. A partial unique index makes
    "one live session per symbol" a property of the database rather than
    of when each process happened to read.

``owner_token`` + ``generation``
    r2 NEW BLOCKER 1: finalisation used to take only ``event_uuid``, so a
    claimant whose lease had already rolled over could terminate the *new*
    claimant's row. Every write is fenced on
    ``(event_uuid, generation, owner_token)``; a stale owner matches no row
    and its update affects zero rows.

``state`` is checked in the database as well as the enum, because the
partial unique index above is only meaningful if ``'started'`` cannot be
spelled some other way.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Spelled here as literals rather than imported from the service layer so
# the table definition does not depend on service import order. The
# equality with ``ClaimLifecycle`` is asserted by a test.
_LIFECYCLE_STATES = (
    "started",
    "proposal_created",
    "rejected_with_reason",
    "expired_unprocessed",
)
_STATES_SQL = ", ".join(f"'{state}'" for state in _LIFECYCLE_STATES)


class WatchEventRepricingClaim(Base):
    """A fenced, leased claim on one ``investment_watch_events`` fire."""

    __tablename__ = "watch_event_repricing_claims"
    __table_args__ = (
        UniqueConstraint(
            "event_uuid",
            "generation",
            name="uq_watch_event_repricing_claims_event_generation",
        ),
        CheckConstraint(
            f"state IN ({_STATES_SQL})",
            name="state",
        ),
        CheckConstraint(
            "generation >= 1",
            name="generation",
        ),
        # A terminal row must carry its evidence, and only the matching one.
        CheckConstraint(
            "(state <> 'proposal_created' OR "
            " (proposal_id IS NOT NULL AND length(btrim(proposal_id)) > 0)) AND "
            "(state <> 'rejected_with_reason' OR "
            " (rejection_reason IS NOT NULL AND "
            "  length(btrim(rejection_reason)) > 0))",
            name="terminal_evidence",
        ),
        # One live session per symbol -- enforced by the database, not by a
        # snapshot read. See the module docstring.
        Index(
            "uq_watch_event_repricing_claims_active_symbol",
            "symbol",
            unique=True,
            postgresql_where=text("state = 'started'"),
        ),
        Index(
            "ix_watch_event_repricing_claims_event",
            "event_uuid",
        ),
        Index(
            "ix_watch_event_repricing_claims_state_lease",
            "state",
            "lease_expires_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)

    # Fencing token pair. ``generation`` increments per re-claim after a
    # TTL rollover; ``owner_token`` identifies the exact claimant.
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    owner_token: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )

    claimed_by: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'started'")
    )

    # Terminal evidence. Exactly one is set, per the CHECK above.
    proposal_id: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    claimed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    finalised_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
