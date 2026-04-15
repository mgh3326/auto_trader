# app/models/trade_journal.py
"""Trade journal — investment thesis and strategy metadata."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trading import InstrumentType


class JournalStatus(enum.StrEnum):
    draft = "draft"
    active = "active"
    closed = "closed"
    stopped = "stopped"
    expired = "expired"


class TradeJournal(Base):
    __tablename__ = "trade_journals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','closed','stopped','expired')",
            name="trade_journals_status_allowed",
        ),
        CheckConstraint(
            "side IN ('buy','sell')",
            name="trade_journals_side",
        ),
        CheckConstraint(
            "account_type IN ('live','paper')",
            name="trade_journals_account_type",
        ),
        CheckConstraint(
            "NOT (account_type = 'live' AND paper_trade_id IS NOT NULL)",
            name="trade_journals_no_paper_trade_on_live",
        ),
        Index("ix_trade_journals_symbol_status", "symbol", "status"),
        Index("ix_trade_journals_created", "created_at"),
        Index("ix_trade_journals_account_type", "account_type"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Symbol info
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    side: Mapped[str] = mapped_column(Text, nullable=False, default="buy")

    # Price/quantity at recommendation time
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    # Strategy metadata (the core value!)
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str | None] = mapped_column(Text)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    min_hold_days: Mapped[int | None] = mapped_column(SmallInteger)
    hold_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # Indicator snapshot at entry
    indicators_snapshot: Mapped[dict | None] = mapped_column(JSONB)

    # Extensible metadata (e.g. paperclip_issue_id linkage)
    extra_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    # Status
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")

    # Link to review.trades (optional)
    trade_id: Mapped[int | None] = mapped_column(
        ForeignKey("review.trades.id", ondelete="SET NULL"),
    )

    # Exit info
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    exit_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(Text)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    # Meta
    account: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="live", server_default="live"
    )
    paper_trade_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("side", "buy")
        kwargs.setdefault("status", "draft")
        kwargs.setdefault("account_type", "live")
        super().__init__(**kwargs)
