from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
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
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False)
    name_kr: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    name_en: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
