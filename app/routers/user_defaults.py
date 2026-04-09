"""User Trade Defaults Router — 사용자 기본 거래 설정 API"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.routers.dependencies import get_user_from_request
from app.services.symbol_trade_settings_service import UserTradeDefaultsService

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])

USER_DEFAULTS_UPDATABLE_FIELDS = {
    "crypto_default_buy_amount",
    "crypto_min_order_amount",
    "equity_kr_default_buy_quantity",
    "equity_us_default_buy_quantity",
    "equity_us_default_buy_amount",
}


class UserTradeDefaultsUpdate(BaseModel):
    """사용자 기본 설정 업데이트 요청"""

    crypto_default_buy_amount: float | None = Field(
        None, gt=0, description="암호화폐 기본 매수 금액 (KRW)"
    )
    crypto_min_order_amount: float | None = Field(
        None, gt=0, description="암호화폐 최소 주문 금액 (KRW)"
    )
    equity_kr_default_buy_quantity: float | None = Field(
        None, ge=0, description="국내주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_quantity: float | None = Field(
        None, ge=0, description="해외주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_amount: float | None = Field(
        None, ge=0, description="해외주식 기본 매수 금액 (USD, 0이면 매수 안함)"
    )


class UserTradeDefaultsResponse(BaseModel):
    """사용자 기본 설정 응답"""

    id: int
    user_id: int
    crypto_default_buy_amount: float
    crypto_min_order_amount: float
    equity_kr_default_buy_quantity: float | None
    equity_us_default_buy_quantity: float | None
    equity_us_default_buy_amount: float | None
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def _build_defaults_response(defaults) -> UserTradeDefaultsResponse:
    """UserTradeDefaults 모델 → 응답 변환"""
    return UserTradeDefaultsResponse(
        id=defaults.id,
        user_id=defaults.user_id,
        crypto_default_buy_amount=float(defaults.crypto_default_buy_amount),
        crypto_min_order_amount=float(defaults.crypto_min_order_amount),
        equity_kr_default_buy_quantity=float(defaults.equity_kr_default_buy_quantity)
        if defaults.equity_kr_default_buy_quantity
        else None,
        equity_us_default_buy_quantity=float(defaults.equity_us_default_buy_quantity)
        if defaults.equity_us_default_buy_quantity
        else None,
        equity_us_default_buy_amount=float(defaults.equity_us_default_buy_amount)
        if defaults.equity_us_default_buy_amount
        else None,
        is_active=defaults.is_active,
        created_at=str(defaults.created_at),
        updated_at=str(defaults.updated_at),
    )


@router.get("/user-defaults", response_model=UserTradeDefaultsResponse)
async def get_user_defaults(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 조회"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)
    defaults = await service.get_or_create(user.id)
    return _build_defaults_response(defaults)


@router.put("/user-defaults", response_model=UserTradeDefaultsResponse)
async def update_user_defaults(
    request_data: UserTradeDefaultsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 업데이트"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)

    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    invalid_fields = set(update_data) - USER_DEFAULTS_UPDATABLE_FIELDS
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

    defaults = await service.update_settings(user.id, update_data)
    return _build_defaults_response(defaults)
