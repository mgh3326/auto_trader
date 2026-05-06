"""ROB-118 — Order preview session ORM models.

All writes must go through OrderPreviewSessionService.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class OrderPreviewSession(Base):
    __tablename__ = "order_preview_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    preview_uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    research_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[str] = mapped_column(String(16), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    dry_run_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dry_run_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    approval_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    legs: Mapped[list[OrderPreviewLeg]] = relationship(
        "OrderPreviewLeg",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="OrderPreviewLeg.leg_index",
    )
    executions: Mapped[list[OrderExecutionRequest]] = relationship(
        "OrderExecutionRequest",
        back_populates="session",
        cascade="all, delete-orphan",
    )


class OrderPreviewLeg(Base):
    __tablename__ = "order_preview_leg"
    __table_args__ = (
        UniqueConstraint("session_id", "leg_index", name="uq_preview_leg_session_idx"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("order_preview_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="limit")
    estimated_value: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    estimated_fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    expected_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    dry_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dry_run_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    session: Mapped[OrderPreviewSession] = relationship(
        "OrderPreviewSession", back_populates="legs"
    )


class OrderExecutionRequest(Base):
    __tablename__ = "order_execution_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("order_preview_session.id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("order_preview_leg.id", ondelete="CASCADE"),
        nullable=False,
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[OrderPreviewSession] = relationship(
        "OrderPreviewSession", back_populates="executions"
    )
    leg: Mapped[OrderPreviewLeg] = relationship("OrderPreviewLeg")

    @property
    def leg_index(self) -> int:
        return self.leg.leg_index
