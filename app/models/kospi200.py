"""KOSPI200 종목 구성요소 모델"""

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.models.base import Base


class Kospi200Constituent(Base):
    """KOSPI200 구성종목 정보 테이블"""

    __tablename__ = "kospi200_constituents"

    id = Column(Integer, primary_key=True, index=True)
    stock_code = Column(
        String(10),
        unique=True,
        nullable=False,
        index=True,
        comment="종목코드",
    )
    stock_name = Column(String(100), nullable=False, comment="종목명")
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="현재 구성종목 여부",
    )
    market_cap = Column(
        Float,
        nullable=True,
        comment="시가총액 (억원)",
    )
    weight = Column(
        Float,
        nullable=True,
        comment="지수 비중 (%)",
    )
    sector = Column(
        String(50),
        nullable=True,
        comment="섹터",
    )
    added_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="구성종목 등록일",
    )
    removed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="구성종목 제외일",
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="데이터 생성 시간"
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        comment="데이터 수정 시간",
    )

    def __repr__(self):
        return f"<Kospi200Constituent(code='{self.stock_code}', name='{self.stock_name}', active={self.is_active})>"
