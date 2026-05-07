"""Market events foundation models (ROB-128).

Stores ingested market-wide events (US earnings via Finnhub, KR DART disclosures,
crypto exchange notices, etc.) plus per-day ingestion state for retryable partitions.

All writes must go through MarketEventsRepository. No direct SQL writes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

from app.models.base import Base


class MarketEvent(Base):
    __tablename__ = "market_events"
    __table_args__ = (
        Index(
            "uq_market_events_source_event_id",
            "source",
            "category",
            "market",
            "source_event_id",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
        Index(
            "uq_market_events_natural_key",
            "source",
            "category",
            "market",
            text("coalesce(symbol, '')"),
            "event_date",
            text("coalesce(fiscal_year, 0)"),
            text("coalesce(fiscal_quarter, 0)"),
            unique=True,
            postgresql_where=text("source_event_id IS NULL"),
        ),
        Index("ix_market_events_event_date", "event_date"),
        Index("ix_market_events_symbol", "symbol"),
        Index(
            "ix_market_events_category_market_date", "category", "market", "event_date"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )

    category: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    symbol: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)

    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    release_time_utc: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    release_time_local: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=False)
    )
    source_timezone: Mapped[str | None] = mapped_column(Text)
    time_hint: Mapped[str | None] = mapped_column(Text)

    importance: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="scheduled"
    )

    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer)

    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB)

    fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketEventValue(Base):
    __tablename__ = "market_event_values"
    __table_args__ = (
        Index(
            "uq_market_event_values_event_metric_period",
            "event_id",
            "metric_name",
            text("coalesce(period, '')"),
            unique=True,
        ),
        Index("ix_market_event_values_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("market_events.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str | None] = mapped_column(Text)

    actual: Mapped[float | None] = mapped_column(Numeric(28, 8))
    forecast: Mapped[float | None] = mapped_column(Numeric(28, 8))
    previous: Mapped[float | None] = mapped_column(Numeric(28, 8))
    revised_previous: Mapped[float | None] = mapped_column(Numeric(28, 8))
    unit: Mapped[str | None] = mapped_column(Text)
    surprise: Mapped[float | None] = mapped_column(Numeric(28, 8))
    surprise_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))

    released_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketEventIngestionPartition(Base):
    __tablename__ = "market_event_ingestion_partitions"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "category",
            "market",
            "partition_date",
            name="uq_market_event_ingestion_partitions_source",
        ),
        Index(
            "ix_market_event_ingestion_partitions_status_date",
            "status",
            "partition_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    partition_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    event_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    source_request_hash: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
