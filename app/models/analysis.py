from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import Base


class StockInfo(Base):
    """주식 종목 기본 정보 테이블 (마스터 데이터)"""

    __tablename__ = "stock_info"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(
        String(50), unique=True, nullable=False, index=True, comment="종목 코드/심볼"
    )
    name = Column(String(100), nullable=False, comment="종목명")
    instrument_type = Column(
        String(50),
        nullable=False,
        comment="상품 유형 (equity_kr, equity_us, crypto 등)",
    )
    exchange = Column(String(50), nullable=True, comment="거래소")
    sector = Column(String(100), nullable=True, comment="섹터/업종")
    market_cap = Column(Float, nullable=True, comment="시가총액")
    is_active = Column(Boolean, default=True, nullable=False, comment="활성화 여부")
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="생성 시간"
    )
    updated_at = Column(
        DateTime(timezone=True), onupdate=func.now(), comment="수정 시간"
    )

    # 관계 설정
    analysis_results = relationship("StockAnalysisResult", back_populates="stock_info")

    def __repr__(self):
        return f"<StockInfo(symbol='{self.symbol}', name='{self.name}', type='{self.instrument_type}')>"


class StockAnalysisResult(Base):
    """주식 분석 결과를 저장하는 테이블"""

    __tablename__ = "stock_analysis_results"

    id = Column(Integer, primary_key=True, index=True)

    # 주식 정보와 연결
    stock_info_id = Column(
        Integer, ForeignKey("stock_info.id"), nullable=False, comment="주식 정보 ID"
    )

    # 분석 결과
    model_name = Column(String(100), nullable=False, comment="사용된 AI 모델명")

    decision = Column(String(20), nullable=False, comment="투자 결정 (buy, hold, sell)")
    confidence = Column(Integer, nullable=False, comment="분석 신뢰도 (0-100)")

    # 가격 분석
    appropriate_buy_min = Column(
        Float, nullable=True, comment="적절한 매수 범위 최소값"
    )
    appropriate_buy_max = Column(
        Float, nullable=True, comment="적절한 매수 범위 최대값"
    )
    appropriate_sell_min = Column(
        Float, nullable=True, comment="적절한 매도 범위 최소값"
    )
    appropriate_sell_max = Column(
        Float, nullable=True, comment="적절한 매도 범위 최대값"
    )
    buy_hope_min = Column(Float, nullable=True, comment="매수 희망 범위 최소값")
    buy_hope_max = Column(Float, nullable=True, comment="매수 희망 범위 최대값")
    sell_target_min = Column(Float, nullable=True, comment="매도 목표 범위 최소값")
    sell_target_max = Column(Float, nullable=True, comment="매도 목표 범위 최대값")

    # 근거 및 상세 분석
    reasons = Column(JSONB, nullable=True, comment="분석 근거 (JSON 형태로 저장)")
    detailed_text = Column(Text, nullable=True, comment="상세 분석 텍스트")

    # 원본 프롬프트
    prompt = Column(Text, nullable=False, comment="원본 프롬프트")

    # 메타데이터
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), comment="생성 시간"
    )
    updated_at = Column(
        DateTime(timezone=True), onupdate=func.now(), comment="수정 시간"
    )

    # 관계 설정
    stock_info = relationship("StockInfo", back_populates="analysis_results")

    def __repr__(self):
        return f"<StockAnalysisResult(id={self.id}, stock_info_id={self.stock_info_id}, decision='{self.decision}')>"
