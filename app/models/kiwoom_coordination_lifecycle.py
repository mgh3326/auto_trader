"""Durable lifecycle evidence for the bounded Kiwoom mock writer.

The binary pre-send claim remains in ``review.order_send_intents``.  This table
stores the immutable lineage that precedes that claim and the typed dispatch
evidence that follows a possible broker mutation.  Claim rows and lifecycle
rows deliberately have separate lifetimes: terminal reconciliation may remove
the claim, while this evidence remains append-only for audit and restart
diagnosis.

All writes go through
``app.services.brokers.kiwoom.coordination_store.KiwoomCoordinationStore``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

_DISPATCH_KINDS_SQL = (
    "'acknowledged','definitive_without_broker_id',"
    "'lane_reported_uncertain','callback_failed','ack_attachment_failed'"
)
_CERTAINTIES_SQL = "'definitive','uncertain'"
_DISPATCH_ALL_OR_NONE_SQL = (
    "CASE WHEN dispatch_kind IS NULL THEN "
    "dispatch_envelope IS NULL AND mutation_certainty IS NULL "
    "AND claim_row_id IS NULL AND callback_failed IS NULL "
    "AND ack_attachment_failed IS NULL "
    "AND outer_cancellation_requested IS NULL "
    "ELSE dispatch_envelope IS NOT NULL "
    "AND mutation_certainty IS NOT NULL AND claim_row_id IS NOT NULL "
    "AND callback_failed IS NOT NULL AND ack_attachment_failed IS NOT NULL "
    "AND outer_cancellation_requested IS NOT NULL END"
)


class KiwoomCoordinationLifecycle(Base):
    """One immutable Kiwoom send lineage plus its durable dispatch evidence."""

    __tablename__ = "kiwoom_coordination_lifecycle"
    __table_args__ = (
        UniqueConstraint(
            "claim_account_scope",
            "idempotency_key",
            name="uq_kiwoom_coordination_scope_key",
        ),
        UniqueConstraint(
            "order_attempt_id",
            name="uq_kiwoom_coordination_order_attempt",
        ),
        UniqueConstraint(
            "claim_row_id",
            name="uq_kiwoom_coordination_claim_row",
        ),
        CheckConstraint(
            "lane_id = 'kr.kiwoom.mock'",
            name="lane_kr_kiwoom_mock",
        ),
        CheckConstraint(
            f"dispatch_kind IS NULL OR dispatch_kind IN ({_DISPATCH_KINDS_SQL})",
            name="dispatch_kind",
        ),
        CheckConstraint(
            f"mutation_certainty IS NULL OR mutation_certainty IN ({_CERTAINTIES_SQL})",
            name="mutation_certainty",
        ),
        CheckConstraint(
            _DISPATCH_ALL_OR_NONE_SQL,
            name="dispatch_all_or_none",
        ),
        CheckConstraint(
            "ack_envelope IS NULL OR broker_order_id IS NOT NULL",
            name="ack_envelope_has_broker_order_id",
        ),
        CheckConstraint(
            "CASE WHEN dispatch_kind = 'acknowledged' THEN "
            "broker_order_id IS NOT NULL AND ack_envelope IS NOT NULL "
            "ELSE true END",
            name="acknowledged_has_ack",
        ),
        Index(
            "ix_kiwoom_coordination_scope_dispatch",
            "claim_account_scope",
            "dispatch_kind",
        ),
        Index(
            "ix_kiwoom_coordination_broker_order_id",
            "broker_order_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lane_id: Mapped[str] = mapped_column(Text, nullable=False)
    physical_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_account_scope: Mapped[str] = mapped_column(Text, nullable=False)
    decision_intent_id: Mapped[str] = mapped_column(Text, nullable=False)
    execution_plan_id: Mapped[str] = mapped_column(Text, nullable=False)
    order_attempt_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    initial_envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # ``persist(ack_envelope)`` may commit before the typed dispatch write.  The
    # dispatch writer also carries the same envelope, so it can populate both
    # columns atomically even when this earlier best-effort write was absent.
    ack_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    dispatch_envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    dispatch_kind: Mapped[str | None] = mapped_column(Text)
    mutation_certainty: Mapped[str | None] = mapped_column(Text)
    claim_row_id: Mapped[int | None] = mapped_column(BigInteger)
    callback_failed: Mapped[bool | None] = mapped_column(Boolean)
    ack_attachment_failed: Mapped[bool | None] = mapped_column(Boolean)
    outer_cancellation_requested: Mapped[bool | None] = mapped_column(Boolean)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
