"""Append-only daily KR market close summary records."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import TIMESTAMP, Date, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KRMarketCloseSummary(Base):
    __tablename__ = "kr_market_close_summaries"

    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    interpretation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
