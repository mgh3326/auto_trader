from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KRSymbolUniverse(Base):
    __tablename__ = "kr_symbol_universe"
    __table_args__ = (
        Index(
            "ix_kr_symbol_universe_exchange_is_active_nxt",
            "exchange",
            "is_active",
            "nxt_eligible",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    nxt_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sector_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("symbol_sectors.id"), nullable=True
    )
    sector_updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    security_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_common_share: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    listing_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    shares_outstanding: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 0), nullable=True
    )
    leverage_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    krx_trading_suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    nxt_trading_suspended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    toss_master_updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
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
