from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CryptoInsightSnapshot(Base):
    __tablename__ = "crypto_insight_snapshots"
    __table_args__ = (
        Index(
            "uq_crypto_insight_snapshots_global_identity",
            "metric",
            "provider",
            "snapshot_at",
            unique=True,
            postgresql_where="(symbol IS NULL)",
        ),
        Index(
            "uq_crypto_insight_snapshots_symbol_identity",
            "metric",
            "provider",
            "symbol",
            "snapshot_at",
            unique=True,
            postgresql_where="(symbol IS NOT NULL)",
        ),
        Index("ix_crypto_insight_snapshots_metric_at", "metric", "snapshot_at"),
        Index("ix_crypto_insight_snapshots_provider_at", "provider", "snapshot_at"),
        Index(
            "ix_crypto_insight_snapshots_symbol_metric_at",
            "symbol",
            "metric",
            "snapshot_at",
            postgresql_where="(symbol IS NOT NULL)",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    metric: Mapped[str] = mapped_column(String(48), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 10), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    freshness_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
