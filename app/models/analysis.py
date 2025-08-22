from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float
from sqlalchemy.sql import func

from app.models.base import Base


class StockAnalysisResult(Base):
    """주식 분석 결과를 저장하는 테이블"""
    __tablename__ = "stock_analysis_results"

    id = Column(Integer, primary_key=True, index=True)
    
    # 기본 정보
    symbol = Column(String(50), nullable=False, index=True, comment="종목 코드/심볼")
    name = Column(String(100), nullable=False, comment="종목명")
    instrument_type = Column(String(50), nullable=False, comment="상품 유형 (stock, crypto 등)")
    model_name = Column(String(100), nullable=False, comment="사용된 AI 모델명")
    
    # 분석 결과
    decision = Column(String(20), nullable=False, comment="투자 결정 (buy, hold, sell)")
    confidence = Column(Integer, nullable=False, comment="분석 신뢰도 (0-100)")
    
    # 가격 분석
    appropriate_buy_min = Column(Float, nullable=True, comment="적절한 매수 범위 최소값")
    appropriate_buy_max = Column(Float, nullable=True, comment="적절한 매수 범위 최대값")
    appropriate_sell_min = Column(Float, nullable=True, comment="적절한 매도 범위 최소값")
    appropriate_sell_max = Column(Float, nullable=True, comment="적절한 매도 범위 최대값")
    buy_hope_min = Column(Float, nullable=True, comment="매수 희망 범위 최소값")
    buy_hope_max = Column(Float, nullable=True, comment="매수 희망 범위 최대값")
    sell_target_min = Column(Float, nullable=True, comment="매도 목표 범위 최소값")
    sell_target_max = Column(Float, nullable=True, comment="매도 목표 범위 최대값")
    
    # 근거 및 상세 분석
    reasons = Column(Text, nullable=True, comment="분석 근거 (JSON 형태로 저장)")
    detailed_text = Column(Text, nullable=True, comment="상세 분석 텍스트")
    
    # 원본 프롬프트
    prompt = Column(Text, nullable=False, comment="원본 프롬프트")
    
    # 메타데이터
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="생성 시간")
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), comment="수정 시간")
    
    def __repr__(self):
        return f"<StockAnalysisResult(id={self.id}, symbol='{self.symbol}', name='{self.name}', decision='{self.decision}')>"
