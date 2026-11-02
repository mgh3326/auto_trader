"""Strategy attribution and durable send fences for paper/demo orders only."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

VENUES = (
    "alpaca_paper",
    "alpaca_paper_crypto",
    "binance_spot_demo",
    "binance_futures_demo",
)
OPEN_STATUSES = (
    "planned",
    "sending",
    "uncertain",
    "accepted",
    "partially_filled",
    "filled",
)
STATUSES = (*OPEN_STATUSES, "canceled", "rejected", "closed")


class MockStrategyOrder(Base):
    __tablename__ = "mock_strategy_orders"
    __table_args__ = (
        CheckConstraint(f"venue IN {VENUES!r}", name="paper_demo_venue"),
        CheckConstraint("side IN ('buy','sell','short','cover')", name="side"),
        CheckConstraint(f"status IN {STATUSES!r}", name="status"),
        CheckConstraint(
            "(qty IS NOT NULL OR notional IS NOT NULL) AND "
            "(qty IS NULL OR (qty > 0 AND qty < 'Infinity'::numeric)) AND "
            "(notional IS NULL OR (notional > 0 AND notional < 'Infinity'::numeric))",
            name="positive_size",
        ),
        Index("ix_mock_strategy_due", "venue", "account_ref", "exit_due_at"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(128))
    venue: Mapped[str] = mapped_column(String(32))
    account_ref: Mapped[str] = mapped_column(String(128))
    episode_id: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    notional: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="planned")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
