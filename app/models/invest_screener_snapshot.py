from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InvestScreenerSnapshot(Base):
    __tablename__ = "invest_screener_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            name="uq_invest_screener_snapshots_market_symbol_date",
        ),
        CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_invest_screener_snapshots_market",
        ),
        CheckConstraint(
            "source IN ('kis', 'yahoo')",
            name="ck_invest_screener_snapshots_source",
        ),
        Index(
            "ix_invest_screener_snapshots_market_date",
            "market",
            "snapshot_date",
        ),
        Index(
            "ix_invest_screener_snapshots_market_streak",
            "market",
            "consecutive_up_days",
            postgresql_where="consecutive_up_days IS NOT NULL",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    latest_close: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    consecutive_up_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week_change_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    closes_window: Mapped[list] = mapped_column(JSONB, nullable=False)
    daily_volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
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
