import enum
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.trading import InstrumentType


class SessionStatus(enum.StrEnum):
    open = "open"
    closed = "closed"
    archived = "archived"


class ProposalKind(enum.StrEnum):
    trim = "trim"
    add = "add"
    enter = "enter"
    exit = "exit"
    pullback_watch = "pullback_watch"
    breakout_watch = "breakout_watch"
    avoid = "avoid"
    no_action = "no_action"
    other = "other"


class UserResponse(enum.StrEnum):
    pending = "pending"
    accept = "accept"
    reject = "reject"
    modify = "modify"
    partial_accept = "partial_accept"
    defer = "defer"


class ActionKind(enum.StrEnum):
    live_order = "live_order"
    paper_order = "paper_order"
    watch_alert = "watch_alert"
    no_action = "no_action"
    manual_note = "manual_note"


class TrackKind(enum.StrEnum):
    accepted_live = "accepted_live"
    accepted_paper = "accepted_paper"
    rejected_counterfactual = "rejected_counterfactual"
    analyst_alternative = "analyst_alternative"
    user_alternative = "user_alternative"


class OutcomeHorizon(enum.StrEnum):
    h1 = "1h"
    h4 = "4h"
    d1 = "1d"
    d3 = "3d"
    d7 = "7d"
    final = "final"


class TradingDecisionSession(Base):
    __tablename__ = "trading_decision_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'closed', 'archived')",
            name="trading_decision_sessions_status_allowed",
        ),
        Index(
            "ix_trading_decision_sessions_user_generated_at",
            "user_id",
            "generated_at",
            postgresql_using="btree",
            postgresql_ops={"generated_at": "DESC"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_profile: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_name: Mapped[str | None] = mapped_column(Text)
    market_scope: Mapped[str | None] = mapped_column(Text)
    market_brief: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    notes: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    proposals: Mapped[list["TradingDecisionProposal"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="TradingDecisionProposal.id",
    )


class TradingDecisionProposal(Base):
    __tablename__ = "trading_decision_proposals"
    __table_args__ = (
        CheckConstraint(
            "proposal_kind IN ('trim','add','enter','exit','pullback_watch','breakout_watch','avoid','no_action','other')",
            name="trading_decision_proposals_kind_allowed",
        ),
        CheckConstraint(
            "side IN ('buy','sell','none')",
            name="trading_decision_proposals_side_allowed",
        ),
        CheckConstraint(
            "user_response IN ('pending','accept','reject','modify','partial_accept','defer')",
            name="trading_decision_proposals_user_response_allowed",
        ),
        CheckConstraint(
            "(user_response = 'pending') = (responded_at IS NULL)",
            name="trading_decision_proposals_pending_response_invariant",
        ),
        Index(
            "ix_trading_decision_proposals_session_response",
            "session_id",
            "user_response",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("trading_decision_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    proposal_kind: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False, default="none")

    # original recommendation
    original_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    original_quantity_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    original_trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    original_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    original_currency: Mapped[str | None] = mapped_column(Text)
    original_rationale: Mapped[str | None] = mapped_column(Text)
    original_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # user response
    user_response: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", index=True
    )
    user_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    user_quantity_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    user_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    user_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    user_trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    user_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    user_note: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    session: Mapped[TradingDecisionSession] = relationship(back_populates="proposals")
    actions: Mapped[list["TradingDecisionAction"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    counterfactuals: Mapped[list["TradingDecisionCounterfactual"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    outcomes: Mapped[list["TradingDecisionOutcome"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )


class TradingDecisionAction(Base):
    __tablename__ = "trading_decision_actions"
    __table_args__ = (
        CheckConstraint(
            "action_kind IN ('live_order','paper_order','watch_alert','no_action','manual_note')",
            name="trading_decision_actions_kind_allowed",
        ),
        CheckConstraint(
            "(action_kind IN ('no_action', 'manual_note')) OR (external_order_id IS NOT NULL OR external_paper_id IS NOT NULL OR external_watch_id IS NOT NULL)",
            name="trading_decision_actions_external_id_required",
        ),
        Index(
            "ix_trading_decision_actions_external_order",
            "external_source",
            "external_order_id",
            postgresql_where="(external_order_id IS NOT NULL)",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("trading_decision_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_kind: Mapped[str] = mapped_column(Text, nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(Text)
    external_paper_id: Mapped[str | None] = mapped_column(Text)
    external_watch_id: Mapped[str | None] = mapped_column(Text)
    external_source: Mapped[str | None] = mapped_column(Text)
    payload_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    proposal: Mapped[TradingDecisionProposal] = relationship(back_populates="actions")


class TradingDecisionCounterfactual(Base):
    __tablename__ = "trading_decision_counterfactuals"
    __table_args__ = (
        CheckConstraint(
            "track_kind IN ('rejected_counterfactual','analyst_alternative','user_alternative','accepted_paper')",
            name="trading_decision_counterfactuals_kind_allowed",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("trading_decision_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    track_kind: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    baseline_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    proposal: Mapped[TradingDecisionProposal] = relationship(
        back_populates="counterfactuals"
    )
    outcomes: Mapped[list["TradingDecisionOutcome"]] = relationship(
        back_populates="counterfactual", cascade="all, delete-orphan"
    )


class TradingDecisionOutcome(Base):
    __tablename__ = "trading_decision_outcomes"
    __table_args__ = (
        CheckConstraint(
            "track_kind IN ('accepted_live','accepted_paper','rejected_counterfactual','analyst_alternative','user_alternative')",
            name="trading_decision_outcomes_track_kind_allowed",
        ),
        CheckConstraint(
            "horizon IN ('1h','4h','1d','3d','7d','final')",
            name="trading_decision_outcomes_horizon_allowed",
        ),
        CheckConstraint(
            "(track_kind = 'accepted_live') = (counterfactual_id IS NULL)",
            name="trading_decision_outcomes_accepted_live_requires_null_counterfactual",
        ),
        Index(
            "ix_trading_decision_outcomes_track_identity",
            "proposal_id",
            "counterfactual_id",
            "track_kind",
            "horizon",
            unique=True,
            postgresql_nulls_not_distinct=True,  # PG ≥ 15; NULLs equal → prevents duplicate accepted_live marks (see plan §4.6)
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("trading_decision_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    counterfactual_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_decision_counterfactuals.id", ondelete="CASCADE")
    )
    track_kind: Mapped[str] = mapped_column(Text, nullable=False)
    horizon: Mapped[str] = mapped_column(Text, nullable=False)
    price_at_mark: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    pnl_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    marked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    proposal: Mapped[TradingDecisionProposal] = relationship(back_populates="outcomes")
    counterfactual: Mapped[TradingDecisionCounterfactual | None] = relationship(
        back_populates="outcomes"
    )
