"""
Symbol Trade Settings Router

종목별 분할 매수 수량 설정 CRUD API
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.routers.dependencies import get_user_from_request
from app.services.symbol_trade_settings_service import SymbolTradeSettingsService
from app.services.us_symbol_universe_service import (
    USSymbolUniverseLookupError,
    get_us_exchange_by_symbol,
)

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])

SYMBOL_SETTINGS_UPDATABLE_FIELDS = {
    "buy_quantity_per_order",
    "buy_price_levels",
    "exchange_code",
    "is_active",
    "note",
}


# Pydantic 모델
class SymbolSettingsCreate(BaseModel):
    """설정 생성 요청"""

    symbol: str = Field(..., description="종목 코드 (005930, AAPL, BTC 등)")
    instrument_type: InstrumentType = Field(
        ..., description="상품 타입 (equity_kr, equity_us, crypto)"
    )
    buy_quantity_per_order: float = Field(..., gt=0, description="주문당 매수 수량")
    buy_price_levels: int = Field(
        default=4,
        ge=1,
        le=4,
        description="주문할 가격대 수 (1~4). 1: appropriate_buy_min만, 4: 전체 4개",
    )
    exchange_code: str | None = Field(
        None, description="해외주식 거래소 코드 (NASD, NYSE 등)"
    )
    note: str | None = Field(None, description="메모")


class SymbolSettingsUpdate(BaseModel):
    """설정 업데이트 요청"""

    buy_quantity_per_order: float | None = Field(
        None, gt=0, description="주문당 매수 수량"
    )
    buy_price_levels: int | None = Field(
        None, ge=1, le=4, description="주문할 가격대 수 (1~4)"
    )
    exchange_code: str | None = Field(None, description="해외주식 거래소 코드")
    is_active: bool | None = Field(None, description="활성화 여부")
    note: str | None = Field(None, description="메모")


class SymbolSettingsResponse(BaseModel):
    """설정 응답"""

    id: int
    symbol: str
    instrument_type: str
    buy_quantity_per_order: float
    buy_price_levels: int
    exchange_code: str | None
    is_active: bool
    note: str | None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def _build_settings_response(s) -> SymbolSettingsResponse:
    """SymbolTradeSettings 모델 → 응답 변환"""
    return SymbolSettingsResponse(
        id=s.id,
        symbol=s.symbol,
        instrument_type=s.instrument_type.value,
        buy_quantity_per_order=float(s.buy_quantity_per_order),
        buy_price_levels=s.buy_price_levels,
        exchange_code=s.exchange_code,
        is_active=s.is_active,
        note=s.note,
        created_at=str(s.created_at),
        updated_at=str(s.updated_at),
    )


@router.get("/symbols", response_model=list[SymbolSettingsResponse])
async def get_all_settings(
    request: Request,
    active_only: bool = True,
    instrument_type: InstrumentType | None = None,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 종목 설정 조회"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    if instrument_type:
        settings_list = await service.get_by_type(instrument_type, user.id, active_only)
    else:
        settings_list = await service.get_all(user.id, active_only)

    return [_build_settings_response(s) for s in settings_list]


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

    return _build_settings_response(settings_obj)


@router.post(
    "/symbols",
    response_model=SymbolSettingsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_settings(
    request_data: SymbolSettingsCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 생성"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    existing = await service.get_by_symbol(request_data.symbol, user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settings already exist for symbol: {request_data.symbol}",
        )

    exchange_code = request_data.exchange_code
    if request_data.instrument_type == InstrumentType.equity_us and not exchange_code:
        try:
            exchange_code = await get_us_exchange_by_symbol(request_data.symbol)
        except USSymbolUniverseLookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    settings_obj = await service.create(
        user_id=user.id,
        symbol=request_data.symbol,
        instrument_type=request_data.instrument_type,
        buy_quantity_per_order=request_data.buy_quantity_per_order,
        buy_price_levels=request_data.buy_price_levels,
        exchange_code=exchange_code,
        note=request_data.note,
    )

    return _build_settings_response(settings_obj)


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

    existing = await service.get_by_symbol(symbol, user.id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    invalid_fields = set(update_data) - SYMBOL_SETTINGS_UPDATABLE_FIELDS
    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid fields for update: {', '.join(sorted(invalid_fields))}",
        )

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    settings_obj = await service.update_settings(symbol, update_data, user.id)

    if settings_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings for symbol '{symbol}' not found",
        )

    return _build_settings_response(settings_obj)


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
