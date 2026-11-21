from datetime import datetime
from typing import Any

from sqlalchemy import TIMESTAMP, Boolean, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UpbitSymbolUniverse(Base):
    __tablename__ = "upbit_symbol_universe"
    __table_args__ = (
        Index(
            "ix_upbit_symbol_universe_quote_is_active",
            "quote_currency",
            "is_active",
        ),
        Index(
            "ix_upbit_symbol_universe_base_is_active",
            "base_currency",
            "is_active",
        ),
    )

    market: Mapped[str] = mapped_column(String(20), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(10), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(20), nullable=False)
    korean_name: Mapped[str] = mapped_column(String(100), nullable=False)
    english_name: Mapped[str] = mapped_column(String(100), nullable=False)
    market_warning: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NONE"
    )
    # hk 6026: Upbit's market_event{warning,caution{...}} as last observed by the
    # universe sync (None when Upbit did not state it). ``market_warning`` above
    # is now a legacy projection of ``market_event.warning`` only.
    market_event: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    market_event_observed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
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
