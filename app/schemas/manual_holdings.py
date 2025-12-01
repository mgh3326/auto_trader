"""
Manual Holdings Schemas

수동 잔고 관리 및 통합 포트폴리오 관련 Pydantic 스키마
"""
from datetime import datetime
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.manual_holdings import BrokerType, MarketType
from app.services.trading_price_service import PriceStrategy


# =============================================================================
# Broker Account Schemas
# =============================================================================

class BrokerAccountCreate(BaseModel):
    """브로커 계좌 생성 요청"""
    broker_type: BrokerType
    account_name: str = Field(default="기본 계좌", max_length=100)
    is_mock: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "broker_type": "toss",
                "account_name": "토스 메인계좌",
                "is_mock": False
            }
        }
    )


class BrokerAccountUpdate(BaseModel):
    """브로커 계좌 수정 요청"""
    account_name: Optional[str] = Field(None, max_length=100)
    is_mock: Optional[bool] = None
    is_active: Optional[bool] = None


class BrokerAccountResponse(BaseModel):
    """브로커 계좌 응답"""
    id: int
    user_id: int
    broker_type: BrokerType
    account_name: str
    is_mock: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# Manual Holding Schemas
# =============================================================================

class ManualHoldingCreate(BaseModel):
    """수동 보유 종목 등록 요청"""
    broker_type: BrokerType = Field(description="브로커 타입")
    account_name: str = Field(default="기본 계좌", description="계좌 이름")
    ticker: str = Field(..., min_length=1, max_length=20, description="종목코드")
    market_type: MarketType = Field(..., description="시장 타입")
    quantity: float = Field(..., gt=0, description="보유 수량")
    avg_price: float = Field(..., gt=0, description="평균 매수가")
    display_name: Optional[str] = Field(None, max_length=100, description="표시명")

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        return v.strip().upper()

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "broker_type": "toss",
                "account_name": "기본 계좌",
                "ticker": "005930",
                "market_type": "KR",
                "quantity": 10,
                "avg_price": 74000,
                "display_name": "삼성전자"
            }
        }
    )


class ManualHoldingUpdate(BaseModel):
    """수동 보유 종목 수정 요청"""
    quantity: Optional[float] = Field(None, gt=0)
    avg_price: Optional[float] = Field(None, gt=0)
    display_name: Optional[str] = Field(None, max_length=100)


class ManualHoldingResponse(BaseModel):
    """수동 보유 종목 응답"""
    id: int
    broker_account_id: int
    broker_type: Optional[BrokerType] = None
    account_name: Optional[str] = None
    ticker: str
    market_type: MarketType
    quantity: float
    avg_price: float
    display_name: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ManualHoldingBulkCreate(BaseModel):
    """수동 보유 종목 일괄 등록 요청"""
    broker_type: BrokerType
    account_name: str = "기본 계좌"
    holdings: List[Dict[str, Any]] = Field(
        ...,
        description="보유 종목 목록",
        json_schema_extra={
            "example": [
                {
                    "ticker": "005930",
                    "market_type": "KR",
                    "quantity": 10,
                    "avg_price": 74000,
                    "display_name": "삼성전자"
                }
            ]
        }
    )


# =============================================================================
# Stock Alias Schemas
# =============================================================================

class StockAliasCreate(BaseModel):
    """종목 별칭 등록 요청"""
    ticker: str = Field(..., min_length=1, max_length=20)
    market_type: MarketType
    alias: str = Field(..., min_length=1, max_length=100)
    source: str = Field(default="user", max_length=50)

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        return v.strip().upper()


class StockAliasResponse(BaseModel):
    """종목 별칭 응답"""
    id: int
    ticker: str
    market_type: MarketType
    alias: str
    source: str

    model_config = ConfigDict(from_attributes=True)


class StockAliasSearchResult(BaseModel):
    """종목 별칭 검색 결과"""
    aliases: List[StockAliasResponse]
    total: int


# =============================================================================
# Portfolio Schemas
# =============================================================================

class HoldingInfoResponse(BaseModel):
    """단일 브로커 보유 정보"""
    broker: str
    quantity: float
    avg_price: float


class ReferencePricesResponse(BaseModel):
    """참조 평단가 응답"""
    kis_avg: Optional[float] = None
    kis_quantity: int = 0
    toss_avg: Optional[float] = None
    toss_quantity: int = 0
    combined_avg: Optional[float] = None
    total_quantity: int = 0


class MergedHoldingResponse(BaseModel):
    """통합 보유 종목 응답"""
    ticker: str
    name: str
    market_type: str
    holdings: List[HoldingInfoResponse]
    kis_quantity: int
    kis_avg_price: float
    toss_quantity: int
    toss_avg_price: float
    other_quantity: int = 0
    other_avg_price: float = 0.0
    combined_avg_price: float
    total_quantity: int
    current_price: float
    evaluation: float
    profit_loss: float
    profit_rate: float
    # AI 분석 정보
    analysis_id: Optional[int] = None
    last_analysis_at: Optional[str] = None
    last_analysis_decision: Optional[str] = None
    analysis_confidence: Optional[int] = None
    # 거래 설정
    settings_quantity: Optional[float] = None
    settings_price_levels: Optional[int] = None
    settings_active: Optional[bool] = None


class MergedPortfolioResponse(BaseModel):
    """통합 포트폴리오 응답"""
    success: bool
    total_holdings: int
    krw_balance: Optional[float] = None
    usd_balance: Optional[float] = None
    total_evaluation: float
    total_profit_loss: float
    holdings: List[MergedHoldingResponse]


# =============================================================================
# Trading Schemas
# =============================================================================

class BuyOrderRequest(BaseModel):
    """매수 주문 요청"""
    ticker: str = Field(..., min_length=1, max_length=20)
    market_type: MarketType
    quantity: int = Field(..., gt=0, description="매수 수량")
    price_strategy: PriceStrategy = Field(
        default=PriceStrategy.current,
        description="가격 전략"
    )
    discount_percent: float = Field(
        default=0.0,
        ge=0,
        le=50,
        description="할인율 (lowest_minus_percent 전략용)"
    )
    manual_price: Optional[float] = Field(
        None,
        gt=0,
        description="수동 입력 가격"
    )
    dry_run: bool = Field(
        default=True,
        description="시뮬레이션 모드"
    )

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        return v.strip().upper()

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticker": "005930",
                "market_type": "KR",
                "quantity": 10,
                "price_strategy": "combined_avg",
                "discount_percent": 1.0,
                "manual_price": None,
                "dry_run": True
            }
        }
    )


class SellOrderRequest(BaseModel):
    """매도 주문 요청"""
    ticker: str = Field(..., min_length=1, max_length=20)
    market_type: MarketType
    quantity: int = Field(..., gt=0, description="매도 수량")
    price_strategy: PriceStrategy = Field(
        default=PriceStrategy.current,
        description="가격 전략"
    )
    profit_percent: float = Field(
        default=5.0,
        ge=0,
        le=100,
        description="목표 수익률 (avg_plus 전략용)"
    )
    manual_price: Optional[float] = Field(
        None,
        gt=0,
        description="수동 입력 가격"
    )
    dry_run: bool = Field(
        default=True,
        description="시뮬레이션 모드"
    )

    @field_validator("ticker")
    @classmethod
    def validate_ticker(cls, v: str) -> str:
        return v.strip().upper()

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ticker": "005930",
                "market_type": "KR",
                "quantity": 5,
                "price_strategy": "combined_avg_plus",
                "profit_percent": 5.0,
                "manual_price": None,
                "dry_run": True
            }
        }
    )


class ExpectedProfitResponse(BaseModel):
    """예상 수익 응답"""
    amount: float
    percent: float


class OrderSimulationResponse(BaseModel):
    """주문 시뮬레이션 응답"""
    status: str = Field(description="상태: simulated | submitted | failed")
    order_price: float
    price_source: str
    current_price: float
    reference_prices: ReferencePricesResponse
    expected_profit: Optional[Dict[str, ExpectedProfitResponse]] = None
    warning: Optional[str] = None
    error: Optional[str] = None
    # 실제 주문 시
    order_id: Optional[str] = None
    order_time: Optional[str] = None
