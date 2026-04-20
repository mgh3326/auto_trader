from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SellCondition(Base):
    __tablename__ = "sell_conditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    price_threshold: Mapped[float] = mapped_column(Numeric(18, 2))
    stoch_rsi_threshold: Mapped[float] = mapped_column(Numeric(6, 2), default=80.0)
    foreign_days: Mapped[int] = mapped_column(Integer, default=2)
    rsi_high: Mapped[float] = mapped_column(Numeric(6, 2), default=70.0)
    rsi_low: Mapped[float] = mapped_column(Numeric(6, 2), default=65.0)
    bb_upper_ref: Mapped[float] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<SellCondition(symbol={self.symbol}, name={self.name}, active={self.is_active})>"
