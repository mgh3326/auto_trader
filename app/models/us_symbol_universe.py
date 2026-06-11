from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class USSymbolUniverse(Base):
    __tablename__ = "us_symbol_universe"
    __table_args__ = (
        Index(
            "ix_us_symbol_universe_exchange_is_active",
            "exchange",
            "is_active",
        ),
        Index(
            "ix_us_symbol_universe_common_active_symbol",
            "is_common_stock",
            "is_active",
            "symbol",
            postgresql_where=text("is_common_stock IS TRUE AND is_active IS TRUE"),
        ),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    name_kr: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    name_en: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_common_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sector_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("symbol_sectors.id"), nullable=True
    )
    sector_updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
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
