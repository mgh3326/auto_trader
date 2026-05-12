from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketQuoteSnapshot(Base):
    __tablename__ = "market_quote_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "market",
            "symbol",
            "source",
            "snapshot_at",
            name="uq_market_quote_snapshots_market_symbol_source_at",
        ),
        CheckConstraint(
            "market IN ('kr', 'us', 'crypto')",
            name="ck_market_quote_snapshots_market",
        ),
        CheckConstraint(
            "source IN ('kis', 'yahoo', 'upbit', 'naver_finance')",
            name="ck_market_quote_snapshots_source",
        ),
        Index(
            "ix_market_quote_snapshots_market_symbol_at",
            "market",
            "symbol",
            "snapshot_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    previous_close: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    open: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    high: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    low: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
