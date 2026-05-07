"""Research report metadata models (ROB-140).

Compact metadata only. Full PDF/report bodies and full extracted text MUST NOT be
stored. All writes must go through ResearchReportsRepository.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ResearchReportIngestionRun(Base):
    __tablename__ = "research_report_ingestion_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_uuid", name="uq_research_report_ingestion_runs_run_uuid"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    payload_version: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    report_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    inserted_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    errors: Mapped[list | dict | None] = mapped_column(JSONB)
    flags: Mapped[list | dict | None] = mapped_column(JSONB)
    copyright_notice: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchReport(Base):
    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_research_reports_dedup_key"),
        Index("ix_research_reports_published_at", "published_at"),
        Index(
            "ix_research_reports_source_published_at",
            "source",
            "published_at",
        ),
        Index(
            "ix_research_reports_symbol_candidates_gin",
            "symbol_candidates",
            postgresql_using="gin",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_report_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    analyst: Mapped[str | None] = mapped_column(Text)
    published_at_text: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    summary_text: Mapped[str | None] = mapped_column(Text)
    detail_url: Mapped[str | None] = mapped_column(Text)
    detail_title: Mapped[str | None] = mapped_column(Text)
    detail_subtitle: Mapped[str | None] = mapped_column(Text)
    detail_excerpt: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    pdf_filename: Mapped[str | None] = mapped_column(Text)
    pdf_sha256: Mapped[str | None] = mapped_column(Text)
    pdf_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    pdf_page_count: Mapped[int | None] = mapped_column(Integer)
    pdf_text_length: Mapped[int | None] = mapped_column(Integer)
    symbol_candidates: Mapped[list | None] = mapped_column(JSONB)
    raw_text_policy: Mapped[str | None] = mapped_column(Text)
    attribution_publisher: Mapped[str | None] = mapped_column(Text)
    attribution_copyright_notice: Mapped[str | None] = mapped_column(Text)
    attribution_full_text_exported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    attribution_pdf_body_exported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("research_report_ingestion_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
