"""
Symbol Trade Settings Router

종목별 분할 매수 수량 설정 API
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    calculate_estimated_order_cost,
)
from app.services.stock_info_service import StockAnalysisService

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])


# Pydantic 모델
class SymbolSettingsCreate(BaseModel):
    """설정 생성 요청"""

    symbol: str = Field(..., description="종목 코드 (005930, AAPL, BTC 등)")
    instrument_type: InstrumentType = Field(
        ..., description="상품 타입 (equity_kr, equity_us, crypto)"
    )
    buy_quantity_per_order: float = Field(
        ..., gt=0, description="주문당 매수 수량"
    )
    exchange_code: Optional[str] = Field(
        None, description="해외주식 거래소 코드 (NASD, NYSE 등)"
    )
    note: Optional[str] = Field(None, description="메모")


class SymbolSettingsUpdate(BaseModel):
    """설정 업데이트 요청"""

    buy_quantity_per_order: Optional[float] = Field(
        None, gt=0, description="주문당 매수 수량"
    )
    exchange_code: Optional[str] = Field(
        None, description="해외주식 거래소 코드"
    )
    is_active: Optional[bool] = Field(None, description="활성화 여부")
    note: Optional[str] = Field(None, description="메모")


class SymbolSettingsResponse(BaseModel):
    """설정 응답"""

    id: int
    symbol: str
    instrument_type: str
    buy_quantity_per_order: float
    exchange_code: Optional[str]
    is_active: bool
    note: Optional[str]
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class EstimatedCostResponse(BaseModel):
    """예상 비용 응답"""

    symbol: str
    quantity_per_order: float
    buy_prices: List[dict]
    total_orders: int
    total_quantity: float
    total_cost: float
    currency: str


class AllEstimatedCostResponse(BaseModel):
    """전체 예상 비용 응답"""

    symbols: List[EstimatedCostResponse]
    grand_total_cost: float
    total_symbols: int


# API 엔드포인트
@router.get("/", response_model=List[SymbolSettingsResponse])
async def get_all_settings(
    active_only: bool = True,
    instrument_type: Optional[InstrumentType] = None,
    db: AsyncSession = Depends(get_db),
):
    """모든 종목 설정 조회"""
    service = SymbolTradeSettingsService(db)

    if instrument_type:
        settings_list = await service.get_by_type(instrument_type, active_only)
    else:
        settings_list = await service.get_all(active_only)

    return [
        SymbolSettingsResponse(
            id=s.id,
            symbol=s.symbol,
            instrument_type=s.instrument_type.value,
            buy_quantity_per_order=float(s.buy_quantity_per_order),
            exchange_code=s.exchange_code,
            is_active=s.is_active,
            note=s.note,
            created_at=str(s.created_at),
            updated_at=str(s.updated_at),
        )
        for s in settings_list
    ]


@router.get("/{symbol}", response_model=SymbolSettingsResponse)
async def get_settings_by_symbol(
    symbol: str,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목 설정 조회"""
    service = SymbolTradeSettingsService(db)
    settings_obj = await service.get_by_symbol(symbol)

    if not settings_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    return SymbolSettingsResponse(
        id=settings_obj.id,
        symbol=settings_obj.symbol,
        instrument_type=settings_obj.instrument_type.value,
        buy_quantity_per_order=float(settings_obj.buy_quantity_per_order),
        exchange_code=settings_obj.exchange_code,
        is_active=settings_obj.is_active,
        note=settings_obj.note,
        created_at=str(settings_obj.created_at),
        updated_at=str(settings_obj.updated_at),
    )


@router.post("/", response_model=SymbolSettingsResponse, status_code=status.HTTP_201_CREATED)
async def create_settings(
    request: SymbolSettingsCreate,
    db: AsyncSession = Depends(get_db),
):
    """종목 설정 생성"""
    service = SymbolTradeSettingsService(db)

    # 이미 존재하는지 확인
    existing = await service.get_by_symbol(request.symbol)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settings already exist for symbol: {request.symbol}",
        )

    settings_obj = await service.create(
        symbol=request.symbol,
        instrument_type=request.instrument_type,
        buy_quantity_per_order=request.buy_quantity_per_order,
        exchange_code=request.exchange_code,
        note=request.note,
    )

    return SymbolSettingsResponse(
        id=settings_obj.id,
        symbol=settings_obj.symbol,
        instrument_type=settings_obj.instrument_type.value,
        buy_quantity_per_order=float(settings_obj.buy_quantity_per_order),
        exchange_code=settings_obj.exchange_code,
        is_active=settings_obj.is_active,
        note=settings_obj.note,
        created_at=str(settings_obj.created_at),
        updated_at=str(settings_obj.updated_at),
    )


@router.put("/{symbol}", response_model=SymbolSettingsResponse)
async def update_settings(
    symbol: str,
    request: SymbolSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """종목 설정 업데이트"""
    service = SymbolTradeSettingsService(db)

    # 존재 여부 확인
    existing = await service.get_by_symbol(symbol)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    # None이 아닌 필드만 업데이트
    update_data = {k: v for k, v in request.model_dump().items() if v is not None}

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    settings_obj = await service.update_settings(symbol, update_data)

    return SymbolSettingsResponse(
        id=settings_obj.id,
        symbol=settings_obj.symbol,
        instrument_type=settings_obj.instrument_type.value,
        buy_quantity_per_order=float(settings_obj.buy_quantity_per_order),
        exchange_code=settings_obj.exchange_code,
        is_active=settings_obj.is_active,
        note=settings_obj.note,
        created_at=str(settings_obj.created_at),
        updated_at=str(settings_obj.updated_at),
    )


@router.delete("/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_settings(
    symbol: str,
    db: AsyncSession = Depends(get_db),
):
    """종목 설정 삭제"""
    service = SymbolTradeSettingsService(db)

    deleted = await service.delete_settings(symbol)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )


@router.get("/{symbol}/estimated-cost", response_model=EstimatedCostResponse)
async def get_estimated_cost(
    symbol: str,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 예상 매수 비용 계산

    AI 분석 결과의 4개 매수 가격을 기반으로 예상 비용을 계산합니다.
    """
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    # 설정 조회
    settings_obj = await settings_service.get_by_symbol(symbol)
    if not settings_obj or not settings_obj.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active settings not found for symbol: {symbol}",
        )

    # 분석 결과 조회
    analysis = await analysis_service.get_latest_analysis_by_symbol(symbol)
    if not analysis:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for symbol: {symbol}",
        )

    # 매수 가격 추출
    buy_prices = []
    if analysis.appropriate_buy_min is not None:
        buy_prices.append({"price_name": "appropriate_buy_min", "price": float(analysis.appropriate_buy_min)})
    if analysis.appropriate_buy_max is not None:
        buy_prices.append({"price_name": "appropriate_buy_max", "price": float(analysis.appropriate_buy_max)})
    if analysis.buy_hope_min is not None:
        buy_prices.append({"price_name": "buy_hope_min", "price": float(analysis.buy_hope_min)})
    if analysis.buy_hope_max is not None:
        buy_prices.append({"price_name": "buy_hope_max", "price": float(analysis.buy_hope_max)})

    if not buy_prices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No buy prices in analysis for symbol: {symbol}",
        )

    # 통화 결정
    currency = "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"

    # 예상 비용 계산
    result = calculate_estimated_order_cost(
        symbol=symbol,
        buy_prices=buy_prices,
        quantity_per_order=float(settings_obj.buy_quantity_per_order),
        currency=currency,
    )

    return EstimatedCostResponse(**result)


@router.get("/all/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_all_estimated_costs(
    db: AsyncSession = Depends(get_db),
):
    """모든 활성 종목의 예상 매수 비용 합계

    설정된 모든 종목에 대해 예상 비용을 계산하고 합계를 반환합니다.
    """
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    # 모든 활성 설정 조회
    all_settings = await settings_service.get_all(active_only=True)

    results = []
    grand_total = 0.0

    for settings_obj in all_settings:
        # 분석 결과 조회
        analysis = await analysis_service.get_latest_analysis_by_symbol(settings_obj.symbol)
        if not analysis:
            continue

        # 매수 가격 추출
        buy_prices = []
        if analysis.appropriate_buy_min is not None:
            buy_prices.append({"price_name": "appropriate_buy_min", "price": float(analysis.appropriate_buy_min)})
        if analysis.appropriate_buy_max is not None:
            buy_prices.append({"price_name": "appropriate_buy_max", "price": float(analysis.appropriate_buy_max)})
        if analysis.buy_hope_min is not None:
            buy_prices.append({"price_name": "buy_hope_min", "price": float(analysis.buy_hope_min)})
        if analysis.buy_hope_max is not None:
            buy_prices.append({"price_name": "buy_hope_max", "price": float(analysis.buy_hope_max)})

        if not buy_prices:
            continue

        # 통화 결정
        currency = "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"

        # 예상 비용 계산
        result = calculate_estimated_order_cost(
            symbol=settings_obj.symbol,
            buy_prices=buy_prices,
            quantity_per_order=float(settings_obj.buy_quantity_per_order),
            currency=currency,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
    )
