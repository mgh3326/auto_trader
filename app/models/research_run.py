"""Research Run snapshot ORM models (ROB-24).

Read-only / decision-support persistence. These rows store candidates,
pending-reconciliation outputs, and source-freshness metadata for KR/NXT
preparation. They never represent broker order state.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.trading import InstrumentType


class ResearchRunStatus(enum.StrEnum):
    open = "open"
    closed = "closed"
    archived = "archived"


class ResearchRunStage(enum.StrEnum):
    preopen = "preopen"
    intraday = "intraday"
    nxt_aftermarket = "nxt_aftermarket"
    us_open = "us_open"


class ResearchRunMarketScope(enum.StrEnum):
    kr = "kr"
    us = "us"
    crypto = "crypto"


class ResearchRunCandidateKind(enum.StrEnum):
    pending_order = "pending_order"
    holding = "holding"
    screener_hit = "screener_hit"
    proposed = "proposed"
    other = "other"


_RECON_CLASSIFICATIONS = (
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
)
_NXT_CLASSIFICATIONS = (
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
)


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'closed', 'archived')",
            name="research_runs_status_allowed",
        ),
        CheckConstraint(
            "stage IN ('preopen', 'intraday', 'nxt_aftermarket', 'us_open')",
            name="research_runs_stage_allowed",
        ),
        CheckConstraint(
            "market_scope IN ('kr', 'us', 'crypto')",
            name="research_runs_market_scope_allowed",
        ),
        Index(
            "ix_research_runs_user_generated_at",
            "user_id",
            "generated_at",
            postgresql_using="btree",
            postgresql_ops={"generated_at": "DESC"},
        ),
        Index(
            "ix_research_runs_market_stage_generated_at",
            "market_scope",
            "stage",
            "generated_at",
            postgresql_ops={"generated_at": "DESC"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    market_scope: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    source_profile: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_name: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    market_brief: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_freshness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    advisory_links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
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

    candidates: Mapped[list[ResearchRunCandidate]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    reconciliations: Mapped[list[ResearchRunPendingReconciliation]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ResearchRunCandidate(Base):
    __tablename__ = "research_run_candidates"
    __table_args__ = (
        CheckConstraint(
            "side IN ('buy','sell','none')",
            name="research_run_candidates_side_allowed",
        ),
        CheckConstraint(
            "candidate_kind IN ('pending_order','holding','screener_hit','proposed','other')",
            name="research_run_candidates_kind_allowed",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence BETWEEN 0 AND 100)",
            name="research_run_candidates_confidence_range",
        ),
        Index(
            "ix_research_run_candidates_run_symbol",
            "research_run_id",
            "symbol",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    candidate_uuid: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, index=True, default=uuid4
    )
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    candidate_kind: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    proposed_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    confidence: Mapped[int | None] = mapped_column(SmallInteger)
    rationale: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    source_freshness: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
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

    run: Mapped[ResearchRun] = relationship(back_populates="candidates")


class ResearchRunPendingReconciliation(Base):
    __tablename__ = "research_run_pending_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "side IN ('buy','sell')",
            name="research_run_pending_reconciliations_side_allowed",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="research_run_pending_reconciliations_market_allowed",
        ),
        CheckConstraint(
            "classification IN ("
            "'maintain','near_fill','too_far','chasing_risk',"
            "'data_mismatch','kr_pending_non_nxt','unknown_venue','unknown')",
            name="research_run_pending_reconciliations_classification_allowed",
        ),
        CheckConstraint(
            "nxt_classification IS NULL OR nxt_classification IN ("
            "'buy_pending_at_support','buy_pending_too_far','buy_pending_actionable',"
            "'sell_pending_near_resistance','sell_pending_too_optimistic',"
            "'sell_pending_actionable','non_nxt_pending_ignore_for_nxt',"
            "'holding_watch_only','data_mismatch_requires_review','unknown')",
            name="research_run_pending_reconciliations_nxt_classification_allowed",
        ),
        Index(
            "ix_research_run_pending_reconciliations_run_symbol",
            "research_run_id",
            "symbol",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_run_candidates.id", ondelete="SET NULL")
    )
    order_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    nxt_classification: Mapped[str | None] = mapped_column(Text)
    nxt_actionable: Mapped[bool | None] = mapped_column(Boolean)
    gap_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    decision_support: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[ResearchRun] = relationship(back_populates="reconciliations")
