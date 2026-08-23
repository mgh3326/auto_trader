"""Append-only observation log of live fanout screener picks.

Prospective bakeoff scoring reads these rows. The logger lives outside
``buy_candidate_fanout`` so that module's no-write contract stays intact.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ScreenerPickLog(Base):
    """One source-ranked symbol observed from a fanout return."""

    __tablename__ = "screener_pick_log"
    __table_args__ = (
        UniqueConstraint(
            "call_id",
            "source",
            "symbol",
            name="uq_screener_pick_log_call_source_symbol",
        ),
        CheckConstraint(
            "market IN ('kr','us','crypto')",
            name="market",
        ),
        CheckConstraint(
            "length(btrim(source)) > 0",
            name="source_nonempty",
        ),
        CheckConstraint(
            "length(btrim(symbol)) > 0",
            name="symbol_nonempty",
        ),
        CheckConstraint(
            "rank IS NULL OR rank >= 1",
            name="rank_positive",
        ),
        CheckConstraint(
            "source_limit IS NULL OR source_limit >= 1",
            name="limit_positive",
        ),
        CheckConstraint(
            "decision_price_text IS NULL OR "
            "decision_price_text ~ '^-?[0-9]+(\\.[0-9]+)?$'",
            name="price_decimal_text",
        ),
        CheckConstraint(
            "fanout_code_sha256 ~ '^[0-9a-f]{64}$'",
            name="code_sha256",
        ),
        CheckConstraint(
            "jsonb_typeof(source_params) = 'object'",
            name="source_params_object",
        ),
        Index(
            "ix_screener_pick_log_recorded_at",
            "recorded_at",
        ),
        Index(
            "ix_screener_pick_log_market_source_recorded",
            "market",
            "source",
            "recorded_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    call_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    recorded_at_kst: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    family: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_price_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sort_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sort_order: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_preset: Mapped[str | None] = mapped_column(Text, nullable=True)
    fanout_version: Mapped[str] = mapped_column(Text, nullable=False)
    fanout_code_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    source_params: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
