from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SymbolSector(Base):
    """ROB-512 갭3: 시장별 섹터 마스터 (KR=Naver upjong / US=yfinance industry).

    source_key가 안정 식별자 — KR은 Naver 업종번호(\"278\")라 업종명 개명에도
    identity 유지, US는 yfinance industry 영문 원문. 표시 규칙은
    name_kr ?? name_en ?? \"-\" (US 한글 매핑 미스는 name_kr=NULL, fake 금지).
    """

    __tablename__ = "symbol_sectors"
    __table_args__ = (
        UniqueConstraint(
            "market", "source", "source_key",
            name="uq_symbol_sectors_market_source_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name_kr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name_en: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False,
    )
