"""hk 5441 — what lets an ``upbit_live`` intent exist, and what stops it.

Design: ``docs/design/5438-go-execution-path.md`` §2 (S5-S7) and §4.

* :class:`ExecutionMandate` — the minimal mandate: this strategy version, in
  this sleeve, under these caps, until this time, in this mode. Append-only.
* :class:`ExecutionMandateRevocation` — one row ends a mandate. Append-only.
* :class:`ExecutionOperatorExit` — an operator's permission to sell out a
  position even when the sleeve is stopped or the mandate has ended (S6).
* :class:`ExecutionExitHandoff` — a symbol handed from the Go path to an
  approval card because the loss-sell guard refuses the sale (S5). Its state
  moves forward under a trigger; one handoff is open per sleeve and symbol.
* :class:`ExecutionAccountControl` — the account kill row (I7 b).
* :class:`ExecutionChildOrderEvidence` — what the broker said about one sent
  child order. It never changes a journal row; it lets a reader prove the order
  ended and how much of it traded.

The DB guards are in ``alembic/versions/20261120_hk5441_upbit_intent_gateway.py``.
Every table is written through its service only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

MANDATE_TERMS_VERSION = "execution-mandate/min-v1"
MANDATE_MODES: tuple[str, ...] = ("shadow", "live")
HANDOFF_STATES: tuple[str, ...] = ("open", "ready_for_card", "card_claimed", "closed")
ACCOUNT_CONTROL_MODES: tuple[str, ...] = ("ACTIVE", "ENTRY_HALT", "GLOBAL_FREEZE")
BROKER_ORDER_STATES: tuple[str, ...] = ("wait", "watch", "done", "cancel")


class ExecutionAccountControl(Base):
    __tablename__ = "execution_account_control"
    __table_args__ = (
        CheckConstraint("account IN ('upbit_live')", name="account"),
        CheckConstraint(
            "mode IN ('ACTIVE', 'ENTRY_HALT', 'GLOBAL_FREEZE')", name="mode"
        ),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 500 "
            "AND char_length(btrim(updated_by)) BETWEEN 1 AND 128",
            name="audit_nonblank",
        ),
        {"schema": "review"},
    )

    account: Mapped[str] = mapped_column(Text, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionMandate(Base):
    __tablename__ = "execution_mandates"
    __table_args__ = (
        CheckConstraint(
            "terms_version = 'execution-mandate/min-v1'", name="terms_version"
        ),
        CheckConstraint("account IN ('upbit_live')", name="account"),
        CheckConstraint("mode IN ('shadow', 'live')", name="mode"),
        CheckConstraint("strategy_code_sha ~ '^[0-9a-f]{40}$'", name="code_sha"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256"),
        CheckConstraint(
            "cardinality(allowed_symbols) BETWEEN 1 AND 32", name="allowed_symbols"
        ),
        CheckConstraint(
            "per_intent_max_notional_krw >= 5000 "
            "AND per_intent_max_notional_krw <= 1000000000000",
            name="per_intent_max_notional",
        ),
        CheckConstraint("max_open_intents BETWEEN 1 AND 64", name="max_open_intents"),
        CheckConstraint("price_band_bps BETWEEN 0 AND 500", name="price_band"),
        CheckConstraint(
            "max_intent_ttl > interval '0' AND max_intent_ttl <= interval '24 hours'",
            name="max_intent_ttl",
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        CheckConstraint("loss_sell_mode = 'human_card'", name="loss_sell_mode"),
        CheckConstraint(
            "mode <> 'live' OR (account_ref IS NOT NULL "
            "AND char_length(account_ref) BETWEEN 1 AND 200)",
            name="live_requires_account_ref",
        ),
        CheckConstraint(
            "char_length(btrim(strategy)) BETWEEN 1 AND 128 "
            "AND char_length(btrim(approval_ref)) BETWEEN 1 AND 200 "
            "AND char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="references_nonblank",
        ),
        Index("ix_execution_mandates_sleeve", "sleeve_id"),
        {"schema": "review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    terms_version: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_code_sha: Mapped[str] = mapped_column(Text, nullable=False)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sleeve_version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "review.strategy_sleeve_versions.id",
            ondelete="RESTRICT",
            name="fk_execution_mandates_sleeve_version",
        ),
        nullable=False,
    )
    account: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_symbols: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    per_intent_max_notional_krw: Mapped[Decimal] = mapped_column(
        Numeric(20, 0), nullable=False
    )
    max_open_intents: Mapped[int] = mapped_column(Integer, nullable=False)
    price_band_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_intent_ttl: Mapped[timedelta] = mapped_column(Interval, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    loss_sell_mode: Mapped[str] = mapped_column(Text, nullable=False)
    #: HMAC fingerprint of the Upbit credentials the mandate was approved for
    #: (B12). Never the credential itself.
    account_ref: Mapped[str | None] = mapped_column(Text)
    approval_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionMandateRevocation(Base):
    __tablename__ = "execution_mandate_revocations"
    __table_args__ = (
        CheckConstraint(
            "char_length(btrim(revoked_by)) BETWEEN 1 AND 128 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 500",
            name="audit_nonblank",
        ),
        {"schema": "review"},
    )

    mandate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_mandates.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    revoked_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionOperatorExit(Base):
    __tablename__ = "execution_operator_exits"
    __table_args__ = (
        CheckConstraint("symbol ~ '^KRW-[A-Z0-9]{2,10}$'", name="symbol"),
        CheckConstraint(
            "max_quantity > 0 AND max_quantity <= 1000000", name="max_quantity"
        ),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '1 hour'",
            name="expires_within_one_hour",
        ),
        CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 500 "
            "AND char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name="audit_nonblank",
        ),
        {"schema": "review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    mandate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_mandates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    max_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )


class ExecutionExitHandoff(Base):
    __tablename__ = "execution_exit_handoffs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'ready_for_card', 'card_claimed', 'closed')",
            name="state",
        ),
        CheckConstraint("symbol ~ '^KRW-[A-Z0-9]{2,10}$'", name="symbol"),
        CheckConstraint(
            "(state = 'card_claimed') = (proposal_id IS NOT NULL) "
            "AND (state = 'card_claimed') = (reserved_quantity IS NOT NULL)",
            name="claim_columns",
        ),
        CheckConstraint(
            "reserved_quantity IS NULL OR reserved_quantity > 0",
            name="reserved_quantity_positive",
        ),
        CheckConstraint("(state = 'closed') = (closed_at IS NOT NULL)", name="closed"),
        CheckConstraint(
            "claim_count >= 0 AND send_attempts >= 0 "
            "AND send_attempts_proven_unsent >= 0 "
            "AND send_attempts_proven_unsent <= send_attempts",
            name="counters",
        ),
        CheckConstraint(
            "char_length(btrim(open_reason)) BETWEEN 1 AND 64 "
            "AND char_length(btrim(opened_by)) BETWEEN 1 AND 128",
            name="audit_nonblank",
        ),
        Index(
            "uq_execution_exit_handoffs_open",
            "sleeve_id",
            "symbol",
            unique=True,
            postgresql_where=text("state <> 'closed'"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sleeve_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    mandate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_mandates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    open_reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``review.order_proposals.proposal_id`` of the card that holds the claim.
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reserved_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    claim_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    #: Committed before the claimed card may send; never lowered for a claim.
    send_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    send_attempts_proven_unsent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    #: The claimed card's fills could not be read or do not match the ledger
    #: (O2, O4): the whole sleeve is unmeasured while this is set.
    fills_unmeasured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    opened_by: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ExecutionChildOrderEvidence(Base):
    __tablename__ = "execution_child_order_evidence"
    __table_args__ = (
        CheckConstraint(
            "broker_state IN ('wait', 'watch', 'done', 'cancel')", name="broker_state"
        ),
        CheckConstraint(
            "executed_volume >= 0 "
            "AND (remaining_volume IS NULL OR remaining_volume >= 0)",
            name="volumes",
        ),
        CheckConstraint(
            "char_length(btrim(order_uuid)) BETWEEN 1 AND 64 "
            "AND char_length(btrim(recorded_by)) BETWEEN 1 AND 128",
            name="audit_nonblank",
        ),
        {"schema": "review"},
    )

    #: ``review.execution_child_requests.id``. Not a foreign key: the journal's
    #: own tests clear it with TRUNCATE, which a referencing table would block.
    #: The one writer only records a child it just read from the journal.
    child_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    broker_state: Mapped[str] = mapped_column(Text, nullable=False)
    executed_volume: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    remaining_volume: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    recorded_by: Mapped[str] = mapped_column(Text, nullable=False)
