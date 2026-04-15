"""Database model for market reports (daily brief, kr morning, crypto scan)."""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MarketReport(Base):
    __tablename__ = "market_reports"

    __table_args__ = (
        UniqueConstraint(
            "report_type",
            "report_date",
            "market",
            "user_id",
            name="uq_market_reports_type_date_market_user",
        ),
        Index("ix_market_reports_type_date", "report_type", "report_date"),
        Index("ix_market_reports_market", "market"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    report_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="리포트 타입 (daily_brief, kr_morning, crypto_scan)",
    )
    report_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="리포트 대상 날짜",
    )
    market: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="시장 (kr, us, crypto, all)",
    )
    title: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="리포트 제목",
    )
    content: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="구조화된 리포트 데이터",
    )
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="사람이 읽을 수 있는 요약",
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment="추가 메타데이터 (소스, 지표 등)",
    )

    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        comment="데이터 생성일시",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        nullable=True,
        comment="데이터 수정일시",
    )

    user = relationship("User", backref="market_reports")

    def __repr__(self) -> str:
        return (
            f"<MarketReport(id={self.id}, type='{self.report_type}', "
            f"date={self.report_date}, market='{self.market}')>"
        )
