"""Per-lot exit plans and mechanical loss-cut decisions (ROB-1460).

Both tables are append-only. A plan changes by adding a version. The only writer
is ``app/services/mechanical_loss_cut/store.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

NOW_SQL = text("now()")


class LotExitPlan(Base):
    """One version of the exit plan for an account + market + symbol position."""

    __tablename__ = "lot_exit_plans"
    __table_args__ = (
        UniqueConstraint(
            "account_mode",
            "market",
            "symbol",
            "version",
            name="uq_lot_exit_plans_lot_version",
        ),
        CheckConstraint(
            "account_mode IN ('kis_live','toss_live','upbit')",
            name="account_mode",
        ),
        CheckConstraint(
            "market IN ('equity_kr','equity_us','crypto')",
            name="market",
        ),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("event IN ('entry','add','backfill')", name="event"),
        CheckConstraint(
            "stop_basis IN ('support','pct_floor','volatility')",
            name="stop_basis",
        ),
        CheckConstraint("stop_price > 0", name="stop_positive"),
        CheckConstraint("reference_price > 0", name="reference_positive"),
        CheckConstraint("average_cost > 0", name="average_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("max_adds >= 0 AND adds_used >= 0", name="adds"),
        CheckConstraint("backfilled = (event = 'backfill')", name="backfilled"),
        Index(
            "ix_lot_exit_plans_lot_latest",
            "account_mode",
            "market",
            "symbol",
            text("version DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    stop_basis: Mapped[str] = mapped_column(Text, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    add_ladder: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    max_adds: Mapped[int] = mapped_column(Integer, nullable=False)
    adds_used: Mapped[int] = mapped_column(Integer, nullable=False)
    add_verdict: Mapped[str | None] = mapped_column(Text)
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    rung_index: Mapped[int | None] = mapped_column(Integer)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
    )


class MechanicalLossCutDecision(Base):
    """One would-sell. ``trigger_key`` makes it at most once per trigger."""

    __tablename__ = "mechanical_loss_cut_decisions"
    __table_args__ = (
        UniqueConstraint(
            "mode",
            "account_mode",
            "market",
            "symbol",
            "trigger_key",
            name="uq_mechanical_loss_cut_decisions_trigger",
        ),
        CheckConstraint("mode IN ('shadow','live')", name="mode"),
        CheckConstraint(
            "(mode = 'live') = (proposal_id IS NOT NULL)",
            name="live_has_proposal",
        ),
        CheckConstraint(
            "intended_quantity > 0 AND intended_limit_price > 0",
            name="order_positive",
        ),
        Index(
            "ix_mechanical_loss_cut_decisions_lot_session",
            "mode",
            "account_mode",
            "market",
            "symbol",
            text("session DESC"),
        ),
        Index("ix_mechanical_loss_cut_decisions_session", "session"),
        Index(
            "ix_mechanical_loss_cut_decisions_proposal",
            "proposal_id",
            unique=True,
            postgresql_where=text("proposal_id IS NOT NULL"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    backfilled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trigger_family: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_key: Mapped[str] = mapped_column(Text, nullable=False)
    session: Mapped[date] = mapped_column(Date, nullable=False)
    close_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    average_cost: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    unrealized_pct: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    held_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    open_sell_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    protected_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    intended_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    intended_limit_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False
    )
    intended_notional: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    per_order_cap: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    within_per_order_cap: Mapped[bool] = mapped_column(Boolean, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    policy_content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
    )
