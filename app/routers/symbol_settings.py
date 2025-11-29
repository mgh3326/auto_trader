"""
Symbol Trade Settings Router

종목별 분할 매수 수량 설정 API
사용자별 기본 거래 설정도 관리
"""
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType, User
from app.auth.dependencies import get_current_user
from app.auth.web_router import get_current_user_from_session
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    UserTradeDefaultsService,
    calculate_estimated_order_cost,
)
from app.services.stock_info_service import StockAnalysisService

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])


async def get_user_from_request(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """웹 세션 또는 API 토큰에서 사용자 조회"""
    # 먼저 request.state.user 확인 (AuthMiddleware에서 설정)
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    # 세션에서 사용자 조회 시도
    user = await get_current_user_from_session(request, db)
    if user:
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


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


# 사용자 기본 설정 Pydantic 모델
class UserTradeDefaultsUpdate(BaseModel):
    """사용자 기본 설정 업데이트 요청"""

    crypto_default_buy_amount: Optional[float] = Field(
        None, gt=0, description="암호화폐 기본 매수 금액 (KRW)"
    )
    crypto_min_order_amount: Optional[float] = Field(
        None, gt=0, description="암호화폐 최소 주문 금액 (KRW)"
    )
    equity_kr_default_buy_quantity: Optional[float] = Field(
        None, ge=0, description="국내주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_quantity: Optional[float] = Field(
        None, ge=0, description="해외주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_amount: Optional[float] = Field(
        None, ge=0, description="해외주식 기본 매수 금액 (USD, 0이면 매수 안함)"
    )


class UserTradeDefaultsResponse(BaseModel):
    """사용자 기본 설정 응답"""

    id: int
    user_id: int
    crypto_default_buy_amount: float
    crypto_min_order_amount: float
    equity_kr_default_buy_quantity: Optional[float]
    equity_us_default_buy_quantity: Optional[float]
    equity_us_default_buy_amount: Optional[float]
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


# ==========================================
# 사용자 기본 설정 API 엔드포인트
# ==========================================

@router.get("/user-defaults", response_model=UserTradeDefaultsResponse)
async def get_user_defaults(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 조회"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)
    defaults = await service.get_or_create(user.id)

    return UserTradeDefaultsResponse(
        id=defaults.id,
        user_id=defaults.user_id,
        crypto_default_buy_amount=float(defaults.crypto_default_buy_amount),
        crypto_min_order_amount=float(defaults.crypto_min_order_amount),
        equity_kr_default_buy_quantity=float(defaults.equity_kr_default_buy_quantity) if defaults.equity_kr_default_buy_quantity else None,
        equity_us_default_buy_quantity=float(defaults.equity_us_default_buy_quantity) if defaults.equity_us_default_buy_quantity else None,
        equity_us_default_buy_amount=float(defaults.equity_us_default_buy_amount) if defaults.equity_us_default_buy_amount else None,
        is_active=defaults.is_active,
        created_at=str(defaults.created_at),
        updated_at=str(defaults.updated_at),
    )


@router.put("/user-defaults", response_model=UserTradeDefaultsResponse)
async def update_user_defaults(
    request_data: UserTradeDefaultsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 업데이트"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)

    # None이 아닌 필드만 업데이트
    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    defaults = await service.update_settings(user.id, update_data)

    return UserTradeDefaultsResponse(
        id=defaults.id,
        user_id=defaults.user_id,
        crypto_default_buy_amount=float(defaults.crypto_default_buy_amount),
        crypto_min_order_amount=float(defaults.crypto_min_order_amount),
        equity_kr_default_buy_quantity=float(defaults.equity_kr_default_buy_quantity) if defaults.equity_kr_default_buy_quantity else None,
        equity_us_default_buy_quantity=float(defaults.equity_us_default_buy_quantity) if defaults.equity_us_default_buy_quantity else None,
        equity_us_default_buy_amount=float(defaults.equity_us_default_buy_amount) if defaults.equity_us_default_buy_amount else None,
        is_active=defaults.is_active,
        created_at=str(defaults.created_at),
        updated_at=str(defaults.updated_at),
    )


# ==========================================
# 종목별 설정 API 엔드포인트
# ==========================================
@router.get("/symbols", response_model=List[SymbolSettingsResponse])
async def get_all_settings(
    request: Request,
    active_only: bool = True,
    instrument_type: Optional[InstrumentType] = None,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 종목 설정 조회"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    if instrument_type:
        settings_list = await service.get_by_type(instrument_type, user.id, active_only)
    else:
        settings_list = await service.get_all(user.id, active_only)

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


@router.get("/symbols/{symbol}", response_model=SymbolSettingsResponse)
async def get_settings_by_symbol(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 특정 종목 설정 조회"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)
    settings_obj = await service.get_by_symbol(symbol, user.id)

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


@router.post("/symbols", response_model=SymbolSettingsResponse, status_code=status.HTTP_201_CREATED)
async def create_settings(
    request_data: SymbolSettingsCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 생성"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    # 이미 존재하는지 확인 (같은 사용자 + 같은 종목)
    existing = await service.get_by_symbol(request_data.symbol, user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settings already exist for symbol: {request_data.symbol}",
        )

    settings_obj = await service.create(
        user_id=user.id,
        symbol=request_data.symbol,
        instrument_type=request_data.instrument_type,
        buy_quantity_per_order=request_data.buy_quantity_per_order,
        exchange_code=request_data.exchange_code,
        note=request_data.note,
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


@router.put("/symbols/{symbol}", response_model=SymbolSettingsResponse)
async def update_settings(
    symbol: str,
    request_data: SymbolSettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 업데이트"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    # 존재 여부 확인
    existing = await service.get_by_symbol(symbol, user.id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    # None이 아닌 필드만 업데이트
    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    settings_obj = await service.update_settings(symbol, update_data, user.id)

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


@router.delete("/symbols/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_settings(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 삭제"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    deleted = await service.delete_settings(symbol, user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )


@router.get("/symbols/{symbol}/estimated-cost", response_model=EstimatedCostResponse)
async def get_estimated_cost(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 예상 매수 비용 계산

    AI 분석 결과의 4개 매수 가격을 기반으로 예상 비용을 계산합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    # 설정 조회 (사용자별)
    settings_obj = await settings_service.get_by_symbol(symbol, user.id)
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


@router.get("/symbols/all/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_all_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 활성 종목 예상 매수 비용 합계

    설정된 모든 종목에 대해 예상 비용을 계산하고 합계를 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    # 현재 사용자의 모든 활성 설정 조회
    all_settings = await settings_service.get_all(user.id, active_only=True)

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
