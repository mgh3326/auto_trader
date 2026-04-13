"""Paper trading (모의투자) models — paper schema."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.trading import InstrumentType


# ---------------------------------------------------------------------------
# paper.paper_accounts — virtual accounts with cash balances
# ---------------------------------------------------------------------------
class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (
        UniqueConstraint("name", name="uq_paper_accounts_name"),
        {"schema": "paper"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    cash_krw: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    cash_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    description: Mapped[str | None] = mapped_column(Text)
    strategy_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
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


# ---------------------------------------------------------------------------
# paper.paper_positions — current holdings per account+symbol
# ---------------------------------------------------------------------------
class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "symbol", name="uq_paper_positions_account_symbol"
        ),
        Index("ix_paper_positions_account_id", "account_id"),
        Index("ix_paper_positions_symbol", "symbol"),
        {"schema": "paper"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    total_invested: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---------------------------------------------------------------------------
# paper.paper_trades — executed (simulated) trade records
# ---------------------------------------------------------------------------
class PaperTrade(Base):
    __tablename__ = "paper_trades"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="paper_trades_side"),
        CheckConstraint(
            "order_type IN ('limit','market')", name="paper_trades_order_type"
        ),
        CheckConstraint("currency IN ('KRW','USD')", name="paper_trades_currency"),
        Index("ix_paper_trades_account_symbol", "account_id", "symbol"),
        Index("ix_paper_trades_account_executed_at", "account_id", "executed_at"),
        {"schema": "paper"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("paper.paper_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    executed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
