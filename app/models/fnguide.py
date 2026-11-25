"""FnGuide public CompanyInfo snapshots (hk 6508).

Append-only per (symbol, page, observed_date). All writes must go through
``app.services.fnguide_consensus.writer``; nothing else inserts into these tables.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class FnguideSnapshot(Base):
    __tablename__ = "fnguide_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "page",
            "observed_date",
            name="uq_fnguide_snapshots_symbol_page_date",
        ),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_fnguide_snapshots_symbol"),
        CheckConstraint(
            "page IN ('Consensus', 'Invest', 'Finance')",
            name="ck_fnguide_snapshots_page",
        ),
        CheckConstraint(
            "(status = 'parsed' AND value_count > 0)"
            " OR (status = 'no_values' AND reason IS NOT NULL AND value_count = 0)",
            name="ck_fnguide_snapshots_status_reason",
        ),
        CheckConstraint(
            "raw_embedded IS NOT NULL OR raw_reason IS NOT NULL",
            name="ck_fnguide_snapshots_raw_or_reason",
        ),
        CheckConstraint(
            "value_count >= 0 AND report_count >= 0 AND skipped_items >= 0"
            " AND unparsed_blocks >= 0 AND duplicate_keys >= 0",
            name="ck_fnguide_snapshots_counts",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(6), nullable=False)
    page: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_date: Mapped[date] = mapped_column(Date, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_title: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(48))
    headers: Mapped[dict | None] = mapped_column(JSONB)
    raw_embedded: Mapped[dict | None] = mapped_column(JSONB)
    raw_reason: Mapped[str | None] = mapped_column(String(32))
    value_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_count: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_items: Mapped[int] = mapped_column(Integer, nullable=False)
    unparsed_blocks: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_keys: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class FnguideValue(Base):
    __tablename__ = "fnguide_values"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "section",
            "item_name",
            "item_code",
            "item_qualifier",
            "column_key",
            name="uq_fnguide_values_natural",
        ),
        CheckConstraint(
            "(value_text IS NULL) <> (value_reason IS NULL)",
            name="ck_fnguide_values_text_or_reason",
        ),
        CheckConstraint(
            "value_text IS NULL OR value_text ~ '^-?[0-9]+(\\.[0-9]+)?$'",
            name="ck_fnguide_values_exact_decimal",
        ),
        CheckConstraint(
            "value_reason IS NULL OR value_reason IN ('missing', 'unparsable')",
            name="ck_fnguide_values_reason",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fnguide_snapshots.id"), nullable=False
    )
    section: Mapped[str] = mapped_column(String(64), nullable=False)
    item_name: Mapped[str] = mapped_column(Text, nullable=False)
    item_code: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=""
    )
    item_qualifier: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    column_key: Mapped[str] = mapped_column(String(32), nullable=False)
    column_label: Mapped[str | None] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_reason: Mapped[str | None] = mapped_column(String(16))
    unit: Mapped[str | None] = mapped_column(Text)
    digit: Mapped[int | None] = mapped_column(SmallInteger)


class FnguideReport(Base):
    __tablename__ = "fnguide_reports"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "observed_date", "report_hash", name="uq_fnguide_reports_natural"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fnguide_snapshots.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(6), nullable=False)
    observed_date: Mapped[date] = mapped_column(Date, nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_date: Mapped[date | None] = mapped_column(Date)
    broker: Mapped[str | None] = mapped_column(Text)
    analyst: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    source_report_id: Mapped[str | None] = mapped_column(Text)
