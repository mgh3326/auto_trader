"""ROB-816 order_proposals SOT ledger models (review schema).

All writes go through app.services.order_proposals.OrderProposalsService.
The DB CHECK constraints validate the string bag only; the transition graph is
enforced in app.services.order_proposals.state_machine.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.services.order_proposals.state_machine import GROUP_STATES, RUNG_STATES

_MARKETS = "'equity_kr','equity_us','crypto','forex','index'"
_ACCOUNT_MODES = "'kis_live','kis_mock','toss_live','upbit','db_simulated'"
_GROUP_STATES_SQL = ",".join(f"'{s}'" for s in sorted(GROUP_STATES))
_RUNG_STATES_SQL = ",".join(f"'{s}'" for s in sorted(RUNG_STATES))


class OrderProposal(Base):
    __tablename__ = "order_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_order_proposals_proposal_id"),
        CheckConstraint(f"market IN ({_MARKETS})", name="order_proposals_market"),
        CheckConstraint(
            f"account_mode IN ({_ACCOUNT_MODES})",
            name="order_proposals_account_mode",
        ),
        CheckConstraint("side IN ('buy','sell')", name="order_proposals_side"),
        CheckConstraint(
            "order_type IN ('limit','market')", name="order_proposals_order_type"
        ),
        CheckConstraint(
            "action IS NULL OR action IN ('place','replace','cancel')",
            name="order_proposals_action",
        ),
        CheckConstraint(
            f"lifecycle_state IN ({_GROUP_STATES_SQL})",
            name="order_proposals_lifecycle_state",
        ),
        CheckConstraint(
            "approval_dispatch_state IS NULL OR "
            "approval_dispatch_state IN "
            "('pending','sent_current','sent_superseded','failed',"
            "'partial_failed','failed_superseded')",
            name="order_proposals_approval_dispatch_state",
        ),
        CheckConstraint(
            "approval_dispatch_card_kind IS NULL OR "
            "approval_dispatch_card_kind IN "
            "('manual','reconfirm','auto_veto','loss_cut_confirmation')",
            name="order_proposals_approval_dispatch_card_kind",
        ),
        Index("ix_order_proposals_root", "root_proposal_id"),
        Index("ix_order_proposals_state", "lifecycle_state"),
        Index("ix_order_proposals_symbol", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    root_proposal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    supersedes_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    superseded_by_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    no_resubmit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    void_reason: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    proposer: Mapped[str] = mapped_column(Text, nullable=False)
    thesis: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[dict | None] = mapped_column(JSONB)
    broker_account_id: Mapped[str | None] = mapped_column(Text)
    lot_context: Mapped[dict | None] = mapped_column(JSONB)
    action: Mapped[str | None] = mapped_column(Text)
    target_broker_order_id: Mapped[str | None] = mapped_column(Text)
    exit_intent: Mapped[str | None] = mapped_column(Text)
    exit_reason: Mapped[str | None] = mapped_column(Text)
    retrospective_id: Mapped[int | None] = mapped_column(BigInteger)
    approval_issue_id: Mapped[str | None] = mapped_column(Text)
    lifecycle_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="proposed"
    )
    correlation_id: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    source_asof: Mapped[dict | None] = mapped_column(JSONB)
    approval_nonce: Mapped[str | None] = mapped_column(Text)
    approval_nonce_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_dispatch_state: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    approval_dispatch_attempted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_dispatch_failure_code: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_payload_chars: Mapped[int | None] = mapped_column(BigInteger)
    approval_dispatch_card_kind: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_membership_revision: Mapped[int | None] = mapped_column(Integer)
    approval_dispatch_membership_digest: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_published_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approved_by_telegram_user_id: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    commit_lease_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
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


class OrderProposalRung(Base):
    __tablename__ = "order_proposal_rungs"
    __table_args__ = (
        UniqueConstraint(
            "proposal_pk", "rung_index", name="uq_order_proposal_rungs_pk_index"
        ),
        CheckConstraint("side IN ('buy','sell')", name="order_proposal_rungs_side"),
        CheckConstraint(
            f"state IN ({_RUNG_STATES_SQL})", name="order_proposal_rungs_state"
        ),
        Index("ix_order_proposal_rungs_proposal_pk", "proposal_pk"),
        Index("ix_order_proposal_rungs_broker_order_id", "broker_order_id"),
        Index("ix_order_proposal_rungs_correlation_id", "correlation_id"),
        Index("ix_order_proposal_rungs_state", "state"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposals.id", ondelete="CASCADE"),
        nullable=False,
    )
    rung_index: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[object] = mapped_column(Numeric(38, 12), nullable=False)
    limit_price: Mapped[object | None] = mapped_column(Numeric(38, 12))
    notional: Mapped[object | None] = mapped_column(Numeric(38, 12))
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending_approval"
    )
    approval_hash_digest: Mapped[str | None] = mapped_column(Text)
    approval_revision: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    filled_qty: Mapped[object | None] = mapped_column(Numeric(38, 12))
    void_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrderProposalApprovalDispatchAttempt(Base):
    __tablename__ = "order_proposal_approval_dispatch_attempts"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", name="uq_order_proposal_approval_dispatch_attempt_id"
        ),
        CheckConstraint(
            "state IN ('pending','sent_current','sent_superseded','failed',"
            "'partial_failed','failed_superseded')",
            name="order_proposal_approval_dispatch_attempt_state",
        ),
        CheckConstraint(
            "card_kind IN ('manual','reconfirm','auto_veto','loss_cut_confirmation')",
            name="order_proposal_approval_dispatch_attempt_card_kind",
        ),
        Index(
            "ix_order_proposal_approval_dispatch_attempts_proposal",
            "proposal_pk",
            "attempted_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    proposal_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    payload_chars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    context_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    status_code: Mapped[int | None] = mapped_column(Integer)
    telegram_error_code: Mapped[int | None] = mapped_column(Integer)
    error_classification: Mapped[str | None] = mapped_column(Text)
    failure_code: Mapped[str | None] = mapped_column(Text)
    card_kind: Mapped[str] = mapped_column(Text, nullable=False)
    membership_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrderProposalApprovalBatch(Base):
    __tablename__ = "order_proposal_approval_batches"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", name="uq_order_proposal_approval_batches_batch_id"
        ),
        CheckConstraint(
            "summary_dispatch_state IN ('idle','sending','sent')",
            name="order_proposal_approval_batches_summary_state",
        ),
        CheckConstraint(
            "approval_dispatch_state IS NULL OR "
            "approval_dispatch_state IN "
            "('pending','sent_current','sent_superseded','failed',"
            "'partial_failed','failed_superseded')",
            name="order_proposal_approval_batches_dispatch_state",
        ),
        Index(
            "ix_order_proposal_approval_batches_chat_window",
            "chat_id",
            "window_closes_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chat_id: Mapped[str] = mapped_column(Text, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    window_closes_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    approval_nonce: Mapped[str] = mapped_column(Text, nullable=False)
    approval_nonce_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approved_by_telegram_user_id: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    summary_message_id: Mapped[int | None] = mapped_column(BigInteger)
    summary_dispatch_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="idle"
    )
    summary_dispatch_lease_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_dispatch_state: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    approval_dispatch_attempted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_dispatch_published_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    approval_dispatch_failure_code: Mapped[str | None] = mapped_column(Text)
    approval_dispatch_payload_chars: Mapped[int | None] = mapped_column(BigInteger)
    telegram_status_code: Mapped[int | None] = mapped_column(Integer)
    telegram_error_code: Mapped[int | None] = mapped_column(Integer)
    error_classification: Mapped[str | None] = mapped_column(Text)
    membership_revision: Mapped[int | None] = mapped_column(Integer)
    membership_digest: Mapped[str | None] = mapped_column(Text)
    membership_frozen_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
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


class OrderProposalApprovalBatchMember(Base):
    __tablename__ = "order_proposal_approval_batch_members"
    __table_args__ = (
        UniqueConstraint(
            "batch_pk", "proposal_pk", name="uq_order_proposal_batch_member"
        ),
        UniqueConstraint(
            "proposal_pk",
            "approval_nonce_snapshot",
            name="uq_order_proposal_batch_member_nonce",
        ),
        Index("ix_order_proposal_batch_members_batch_pk", "batch_pk"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposal_approval_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposal_pk: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.order_proposals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_nonce_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    approval_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    membership_revision: Mapped[int | None] = mapped_column(Integer)
    approval_dispatch_attempt_id_snapshot: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )
    approval_membership_revision_snapshot: Mapped[int | None] = mapped_column(Integer)
    approval_membership_digest_snapshot: Mapped[str | None] = mapped_column(Text)
    approval_card_kind_snapshot: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    result_detail: Mapped[dict | None] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    added_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
