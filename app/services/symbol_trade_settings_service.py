"""
Symbol Trade Settings Service

종목별 거래 설정 CRUD 및 예상 비용 계산 서비스
사용자별 기본 거래 설정도 관리
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.symbol_trade_settings import SymbolTradeSettings, UserTradeDefaults
from app.models.trading import InstrumentType


class UserTradeDefaultsService:
    """사용자별 기본 거래 설정 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_user_id(self, user_id: int) -> Optional[UserTradeDefaults]:
        """사용자 ID로 기본 설정 조회"""
        result = await self.db.execute(
            select(UserTradeDefaults).where(UserTradeDefaults.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(self, user_id: int) -> UserTradeDefaults:
        """사용자 기본 설정 조회 또는 생성"""
        settings = await self.get_by_user_id(user_id)
        if not settings:
            settings = UserTradeDefaults(
                user_id=user_id,
                crypto_default_buy_amount=Decimal("10000"),
                crypto_min_order_amount=Decimal("5000"),
            )
            self.db.add(settings)
            await self.db.commit()
            await self.db.refresh(settings)
        return settings

    async def update_settings(
        self, user_id: int, update_data: Dict[str, Any]
    ) -> UserTradeDefaults:
        """사용자 기본 설정 업데이트"""
        # 숫자 필드를 Decimal로 변환
        decimal_fields = [
            "crypto_default_buy_amount",
            "crypto_min_order_amount",
            "equity_kr_default_buy_quantity",
            "equity_us_default_buy_quantity",
            "equity_us_default_buy_amount",
        ]
        for field in decimal_fields:
            if field in update_data and update_data[field] is not None:
                update_data[field] = Decimal(str(update_data[field]))

        # 설정이 없으면 생성
        settings = await self.get_or_create(user_id)

        # 업데이트 실행
        await self.db.execute(
            update(UserTradeDefaults)
            .where(UserTradeDefaults.user_id == user_id)
            .values(**update_data)
        )
        await self.db.commit()
        return await self.get_by_user_id(user_id)


class SymbolTradeSettingsService:
    """종목별 거래 설정 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        user_id: int,
        symbol: str,
        instrument_type: InstrumentType,
        buy_quantity_per_order: float,
        exchange_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> SymbolTradeSettings:
        """새로운 종목 설정 생성"""
        settings = SymbolTradeSettings(
            user_id=user_id,
            symbol=symbol,
            instrument_type=instrument_type,
            buy_quantity_per_order=Decimal(str(buy_quantity_per_order)),
            exchange_code=exchange_code,
            note=note,
            is_active=True,
        )
        self.db.add(settings)
        await self.db.commit()
        await self.db.refresh(settings)
        return settings

    async def get_by_symbol(
        self, symbol: str, user_id: Optional[int] = None
    ) -> Optional[SymbolTradeSettings]:
        """심볼로 설정 조회

        user_id가 제공되면 해당 사용자의 설정만 조회
        """
        query = select(SymbolTradeSettings).where(SymbolTradeSettings.symbol == symbol)
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    async def get_by_id(self, settings_id: int) -> Optional[SymbolTradeSettings]:
        """ID로 설정 조회"""
        result = await self.db.execute(
            select(SymbolTradeSettings).where(SymbolTradeSettings.id == settings_id)
        )
        return result.scalar_one_or_none()

    async def get_all(
        self, user_id: Optional[int] = None, active_only: bool = True
    ) -> List[SymbolTradeSettings]:
        """모든 설정 조회"""
        query = select(SymbolTradeSettings)
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        if active_only:
            query = query.where(SymbolTradeSettings.is_active == True)
        result = await self.db.execute(query.order_by(SymbolTradeSettings.symbol))
        return list(result.scalars().all())

    async def get_by_type(
        self,
        instrument_type: InstrumentType,
        user_id: Optional[int] = None,
        active_only: bool = True,
    ) -> List[SymbolTradeSettings]:
        """상품 타입별 설정 조회"""
        query = select(SymbolTradeSettings).where(
            SymbolTradeSettings.instrument_type == instrument_type
        )
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        if active_only:
            query = query.where(SymbolTradeSettings.is_active == True)
        result = await self.db.execute(query.order_by(SymbolTradeSettings.symbol))
        return list(result.scalars().all())

    async def update_settings(
        self, symbol: str, update_data: Dict[str, Any], user_id: Optional[int] = None
    ) -> Optional[SymbolTradeSettings]:
        """설정 업데이트"""
        # buy_quantity_per_order가 있으면 Decimal로 변환
        if "buy_quantity_per_order" in update_data:
            update_data["buy_quantity_per_order"] = Decimal(
                str(update_data["buy_quantity_per_order"])
            )

        query = (
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
        )
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)

        await self.db.execute(query.values(**update_data))
        await self.db.commit()
        return await self.get_by_symbol(symbol, user_id)

    async def delete_settings(
        self, symbol: str, user_id: Optional[int] = None
    ) -> bool:
        """설정 삭제"""
        query = delete(SymbolTradeSettings).where(SymbolTradeSettings.symbol == symbol)
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        result = await self.db.execute(query)
        await self.db.commit()
        return result.rowcount > 0

    async def deactivate(self, symbol: str, user_id: Optional[int] = None) -> bool:
        """설정 비활성화"""
        query = (
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
        )
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        result = await self.db.execute(query.values(is_active=False))
        await self.db.commit()
        return result.rowcount > 0

    async def activate(self, symbol: str, user_id: Optional[int] = None) -> bool:
        """설정 활성화"""
        query = (
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
        )
        if user_id is not None:
            query = query.where(SymbolTradeSettings.user_id == user_id)
        result = await self.db.execute(query.values(is_active=True))
        await self.db.commit()
        return result.rowcount > 0


def calculate_estimated_order_cost(
    symbol: str,
    buy_prices: List[Dict[str, float]],
    quantity_per_order: float,
    currency: str = "KRW",
) -> Dict[str, Any]:
    """
    예상 주문 비용 계산

    Args:
        symbol: 종목 코드
        buy_prices: 매수 가격 목록 [{"price_name": "...", "price": 50000}, ...]
        quantity_per_order: 주문당 수량
        currency: 통화 (KRW, USD)

    Returns:
        {
            "symbol": "005930",
            "quantity_per_order": 2,
            "buy_prices": [
                {"price_name": "...", "price": 50000, "quantity": 2, "cost": 100000},
            ],
            "total_orders": 2,
            "total_quantity": 4,
            "total_cost": 196000,
            "currency": "KRW"
        }
    """
    result_prices = []
    total_quantity = 0
    total_cost = 0.0

    for price_info in buy_prices:
        price = price_info["price"]
        price_name = price_info["price_name"]

        # 주식의 경우 정수 수량만 허용
        if currency == "KRW":
            qty = int(quantity_per_order)
        else:
            qty = quantity_per_order

        cost = price * qty

        result_prices.append(
            {
                "price_name": price_name,
                "price": price,
                "quantity": qty,
                "cost": cost,
            }
        )

        total_quantity += qty
        total_cost += cost

    return {
        "symbol": symbol,
        "quantity_per_order": quantity_per_order,
        "buy_prices": result_prices,
        "total_orders": len(buy_prices),
        "total_quantity": total_quantity,
        "total_cost": total_cost,
        "currency": currency,
    }


async def get_buy_quantity_for_symbol(
    db: AsyncSession,
    symbol: str,
    price: float,
    user_id: Optional[int] = None,
    fallback_amount: Optional[float] = None,
) -> Optional[int]:
    """
    종목의 매수 수량 조회 (국내/해외 주식용)

    설정이 있으면 설정된 수량 반환
    설정이 없으면:
      - fallback_amount가 있으면 금액 기반 계산
      - 없으면 None 반환 (매수하지 않음)

    Args:
        db: DB 세션
        symbol: 종목 코드
        price: 현재 가격
        user_id: 사용자 ID (optional)
        fallback_amount: 설정이 없을 때 사용할 금액 (KRW/USD), None이면 매수 안함

    Returns:
        매수 수량 (정수) 또는 None (매수하지 않음)
    """
    service = SymbolTradeSettingsService(db)
    settings = await service.get_by_symbol(symbol, user_id)

    if settings and settings.is_active:
        # 설정된 수량 사용
        return int(settings.buy_quantity_per_order)
    elif fallback_amount is not None:
        # 폴백: 금액 기반 수량 계산
        return int(fallback_amount / price) if price > 0 else 0
    else:
        # 설정이 없고 폴백도 없으면 매수 안함
        return None


async def get_buy_amount_for_crypto(
    db: AsyncSession,
    symbol: str,
    user_id: Optional[int] = None,
    default_amount: float = 10000,
) -> float:
    """
    코인의 매수 금액 조회

    설정이 있으면 설정된 금액 반환
    설정이 없으면 기본 금액 반환

    Args:
        db: DB 세션
        symbol: 코인 코드 (KRW-BTC 등)
        user_id: 사용자 ID (optional)
        default_amount: 설정이 없을 때 사용할 기본 금액 (기본 10000 KRW)

    Returns:
        매수 금액 (KRW)
    """
    service = SymbolTradeSettingsService(db)
    settings = await service.get_by_symbol(symbol, user_id)

    if settings and settings.is_active:
        # 설정된 금액 사용
        return float(settings.buy_quantity_per_order)
    else:
        # 기본 금액 사용
        return default_amount


async def get_buy_quantity_for_crypto(
    db: AsyncSession, symbol: str, price: float, fallback_amount: float
) -> float:
    """
    코인의 매수 수량 조회 (소수점 지원) - 레거시 호환

    Args:
        db: DB 세션
        symbol: 코인 코드 (BTC, ETH 등)
        price: 현재 가격
        fallback_amount: 설정이 없을 때 사용할 금액 (KRW)

    Returns:
        매수 수량 (소수점 가능)
    """
    service = SymbolTradeSettingsService(db)
    settings = await service.get_by_symbol(symbol)

    if settings and settings.is_active:
        # 코인의 경우 buy_quantity_per_order가 금액이므로 수량으로 변환
        buy_amount = float(settings.buy_quantity_per_order)
        return buy_amount / price if price > 0 else 0.0
    else:
        # 폴백: 금액 기반 수량 계산
        return fallback_amount / price if price > 0 else 0.0
