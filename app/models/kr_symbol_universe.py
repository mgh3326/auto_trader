from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
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
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
