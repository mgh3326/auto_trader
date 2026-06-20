"""ROB-315 Phase 1 — daily scalping review-loop tables.

The **review layer** sits on top of the raw ``scalp_trade_analytics`` rows
(ROB-313). It is where human judgment lives: a daily rollup of the demo
scalping round-trips plus the operator's observation → root cause →
improvement → next-run plan, and the discrete review actions that follow.

Two tables, both written only through ``ScalpingReviewService``:

* ``scalping_daily_reviews`` — one row per ``(review_date, product,
  account_scope, session_tag)``. ``source_payload`` is the immutable rollup
  snapshot the draft was built from; the operator fields and ``decision`` /
  ``status`` are the only mutable human inputs. Raw analytics rows are never
  edited from here.
* ``scalping_review_actions`` — discrete follow-ups attached to a review.

Safety boundaries (ROB-315): ``account_scope`` is pinned to ``binance_demo``
so demo scalping review state can never be conflated with KIS/Upbit live
execution permissions. ``session_tag`` is ``NOT NULL DEFAULT ''`` so the
uniqueness key is well-defined (Postgres treats NULL as distinct, which would
silently break draft idempotency).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# Fixed account scope for this workstream — demo-only, never a live scope.
SCALPING_REVIEW_ACCOUNT_SCOPE = "binance_demo"

REVIEW_DECISIONS = ("review", "keep", "adjust", "pause", "disable")
REVIEW_STATUSES = ("draft", "reviewed", "locked")
ACTION_TYPES = (
    "parameter_change",
    "investigate",
    "pause",
    "resume",
    "add_guard",
    "data_quality",
    "no_change",
)
ACTION_STATUSES = ("open", "applied", "skipped", "superseded")


class ScalpingDailyReview(Base):
    """One daily review of the demo scalping loop for a product/session."""

    __tablename__ = "scalping_daily_reviews"
    __table_args__ = (
        UniqueConstraint(
            "review_date",
            "product",
            "account_scope",
            "session_tag",
            name="uq_scalping_daily_review_key",
        ),
        CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="scalping_daily_review_product",
        ),
        CheckConstraint(
            "account_scope = 'binance_demo'",
            name="scalping_daily_review_account_scope",
        ),
        CheckConstraint(
            "decision IN ('review','keep','adjust','pause','disable')",
            name="scalping_daily_review_decision",
        ),
        CheckConstraint(
            "status IN ('draft','reviewed','locked')",
            name="scalping_daily_review_status",
        ),
        Index("ix_scalping_daily_review_date", "review_date"),
        Index("ix_scalping_daily_review_product", "product"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    review_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    product: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=SCALPING_REVIEW_ACCOUNT_SCOPE
    )
    # NOT NULL DEFAULT '' so the uniqueness key is well-defined (see module doc).
    session_tag: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # --- Aggregate metrics (rolled up from scalp_trade_analytics) ---
    # trade_count counts fill-proven round-trips only; anomaly_count counts
    # rows with no derivable fill price. Avg/PnL fields are NULL ("n/a") when
    # no row carries the value rather than a misleading 0.
    trade_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    win_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    anomaly_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    gross_pnl_usdt: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True
    )
    net_pnl_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    benchmark_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    avg_slippage_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    avg_spread_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    avg_mae_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    avg_mfe_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    avg_holding_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason_counts: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    # --- Operator review (the only mutable human inputs) ---
    observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvement: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False, server_default="review")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")

    # Immutable rollup source snapshot the draft was built from.
    source_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    actions: Mapped[list[ScalpingReviewAction]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class ScalpingReviewAction(Base):
    """A discrete follow-up attached to a daily review."""

    __tablename__ = "scalping_review_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('parameter_change','investigate','pause','resume',"
            "'add_guard','data_quality','no_change')",
            name="scalping_review_action_type",
        ),
        CheckConstraint(
            "status IN ('open','applied','skipped','superseded')",
            name="scalping_review_action_status",
        ),
        Index("ix_scalping_review_action_review_id", "review_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "scalping_daily_reviews.id",
            name="fk_scalping_review_action_review_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_component: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    review: Mapped[ScalpingDailyReview] = relationship(back_populates="actions")
