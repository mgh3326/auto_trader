"""Additive, non-economic outcomes for Phase 0 fill/watch context consumption."""

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
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Keep SQL-facing values local so Base metadata has no service import-order
# dependency. A contract test asserts equality with the domain enum.
_CONTEXTUAL_STATUSES = (
    "context_only_no_action",
    "stale_input",
    "failed_processing",
    "needs_human",
)
_REASONS = (
    "context_recorded",
    "close_condition_recorded",
    "stale_market_snapshot",
    "stale_position_snapshot",
    "artifact_declared_failed",
    "route_unavailable",
    "event_uuid_conflict",
)
_NEXT_ACTIONS = (
    "none",
    "refresh_context",
    "inspect_artifact",
    "await_route",
    "operator_review",
)
_STATUS_SQL = ", ".join(f"'{value}'" for value in _CONTEXTUAL_STATUSES)
_REASON_SQL = ", ".join(f"'{value}'" for value in _REASONS)
_NEXT_ACTION_SQL = ", ".join(f"'{value}'" for value in _NEXT_ACTIONS)


class FillWatchContextOutcome(Base):
    """One queryable, context-only outcome for one canonical transport UUID.

    ``transport_event_uuid`` is the transport dedupe identity.  The economic
    root is deliberately only indexed, never unique, so independent partial
    and full fills retain their own outcomes while still being coalescible at
    read time.
    """

    __tablename__ = "fill_watch_context_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "transport_event_uuid",
            name="uq_fill_watch_context_outcomes_transport_event_uuid",
        ),
        CheckConstraint(
            "transport_event_id = transport_event_uuid::text",
            name="canonical_transport_event_id",
        ),
        CheckConstraint(
            "responsible_consumer = 'fill_watch_context'",
            name="responsible_consumer",
        ),
        CheckConstraint("event_kind IN ('fill', 'watch')", name="event_kind"),
        CheckConstraint(
            f"contextual_status IN ({_STATUS_SQL})",
            name="contextual_status",
        ),
        CheckConstraint(f"reason IN ({_REASON_SQL})", name="reason"),
        CheckConstraint(f"next_action IN ({_NEXT_ACTION_SQL})", name="next_action"),
        CheckConstraint(
            "length(btrim(economic_root_ref)) > 0",
            name="economic_root_ref",
        ),
        CheckConstraint(
            "jsonb_typeof(order_refs) = 'array' AND jsonb_array_length(order_refs) >= 1",
            name="order_refs",
        ),
        CheckConstraint("delivery_count >= 1", name="delivery_count"),
        CheckConstraint("conflict_count >= 0", name="conflict_count"),
        CheckConstraint(
            "semantic_digest ~ '^[0-9a-f]{64}$'",
            name="semantic_digest",
        ),
        Index(
            "ix_fill_watch_context_outcomes_economic_root_ref",
            "economic_root_ref",
        ),
        Index(
            "ix_fill_watch_context_outcomes_contextual_status",
            "contextual_status",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transport_event_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    transport_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    responsible_consumer: Mapped[str] = mapped_column(Text, nullable=False)
    event_kind: Mapped[str] = mapped_column(Text, nullable=False)
    input_as_of: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    economic_root_ref: Mapped[str] = mapped_column(Text, nullable=False)
    order_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    contextual_status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    next_action: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_digest: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    conflict_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["FillWatchContextOutcome"]
