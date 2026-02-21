from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UpbitSymbolUniverse(Base):
    __tablename__ = "upbit_symbol_universe"
    __table_args__ = (
        Index(
            "ix_upbit_symbol_universe_market_is_active",
            "market",
            "is_active",
        ),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    korean_name: Mapped[str] = mapped_column(String(100), nullable=False)
    english_name: Mapped[str] = mapped_column(String(100), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    market_warning: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NONE"
    )
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
