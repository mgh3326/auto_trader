"""Investment per-dimension analyst reports ORM (ROB-306).

Hermes-authored analyst reports on the DIMENSION axis (market/news/fundamentals/
sentiment), mirroring the symbol axis in
``app.models.investment_symbol_intermediate_reports``. ``symbol`` is nullable:
NULL = market-wide (Market); set = per-symbol (future News/Fundamentals).

    UNIQUE(run_uuid, dimension, market, symbol, artifact_version)

Hermes writes the prose (push-only, no in-process LLM); auto_trader validates,
caps confidence by freshness, and persists.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DIMENSIONS: tuple[str, ...] = ("market", "news", "fundamentals", "sentiment")
STANCES: tuple[str, ...] = ("bullish", "neutral", "bearish")
MARKETS: tuple[str, ...] = ("kr", "us", "crypto")


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class InvestmentDimensionReport(Base):
    __tablename__ = "investment_dimension_reports"
    __table_args__ = (
        CheckConstraint(
            f"dimension IN ({_sql_in(DIMENSIONS)})",
            name="ck_investment_dimension_reports_dimension",
        ),
        CheckConstraint(
            f"market IN ({_sql_in(MARKETS)})",
            name="ck_investment_dimension_reports_market",
        ),
        CheckConstraint(
            f"stance IS NULL OR stance IN ({_sql_in(STANCES)})",
            name="ck_investment_dimension_reports_stance",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 100)",
            name="ck_investment_dimension_reports_confidence_range",
        ),
        UniqueConstraint(
            "run_uuid",
            "dimension",
            "market",
            "symbol",
            "artifact_version",
            name="uq_investment_dimension_reports_run_dim_market_symbol_ver",
        ),
        Index("ix_investment_dimension_reports_run_uuid", "run_uuid"),
        Index("ix_investment_dimension_reports_run_dimension", "run_uuid", "dimension"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dimension_report_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        unique=True,
        server_default=text("gen_random_uuid()"),
    )
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.investment_stage_runs.run_uuid", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_bundle_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL = market-wide (Market). Postgres treats NULLs as distinct in UNIQUE,
    # which is fine: a run has exactly one market-wide Market report per version.
    symbol: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    report_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_findings: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    signals: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stance: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_data: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    freshness_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    cited_snapshot_uuids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)),
        nullable=False,
        server_default=text("ARRAY[]::uuid[]"),
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
