"""
Symbol Trade Settings Service

종목별 거래 설정 CRUD 및 예상 비용 계산 서비스
"""
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.symbol_trade_settings import SymbolTradeSettings
from app.models.trading import InstrumentType


class SymbolTradeSettingsService:
    """종목별 거래 설정 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        symbol: str,
        instrument_type: InstrumentType,
        buy_quantity_per_order: float,
        exchange_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> SymbolTradeSettings:
        """새로운 종목 설정 생성"""
        settings = SymbolTradeSettings(
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

    async def get_by_symbol(self, symbol: str) -> Optional[SymbolTradeSettings]:
        """심볼로 설정 조회"""
        result = await self.db.execute(
            select(SymbolTradeSettings).where(SymbolTradeSettings.symbol == symbol)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, settings_id: int) -> Optional[SymbolTradeSettings]:
        """ID로 설정 조회"""
        result = await self.db.execute(
            select(SymbolTradeSettings).where(SymbolTradeSettings.id == settings_id)
        )
        return result.scalar_one_or_none()

    async def get_all(self, active_only: bool = True) -> List[SymbolTradeSettings]:
        """모든 설정 조회"""
        query = select(SymbolTradeSettings)
        if active_only:
            query = query.where(SymbolTradeSettings.is_active == True)
        result = await self.db.execute(query.order_by(SymbolTradeSettings.symbol))
        return list(result.scalars().all())

    async def get_by_type(
        self, instrument_type: InstrumentType, active_only: bool = True
    ) -> List[SymbolTradeSettings]:
        """상품 타입별 설정 조회"""
        query = select(SymbolTradeSettings).where(
            SymbolTradeSettings.instrument_type == instrument_type
        )
        if active_only:
            query = query.where(SymbolTradeSettings.is_active == True)
        result = await self.db.execute(query.order_by(SymbolTradeSettings.symbol))
        return list(result.scalars().all())

    async def update_settings(
        self, symbol: str, update_data: Dict[str, Any]
    ) -> Optional[SymbolTradeSettings]:
        """설정 업데이트"""
        # buy_quantity_per_order가 있으면 Decimal로 변환
        if "buy_quantity_per_order" in update_data:
            update_data["buy_quantity_per_order"] = Decimal(
                str(update_data["buy_quantity_per_order"])
            )

        await self.db.execute(
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
            .values(**update_data)
        )
        await self.db.commit()
        return await self.get_by_symbol(symbol)

    async def delete_settings(self, symbol: str) -> bool:
        """설정 삭제"""
        result = await self.db.execute(
            delete(SymbolTradeSettings).where(SymbolTradeSettings.symbol == symbol)
        )
        await self.db.commit()
        return result.rowcount > 0

    async def deactivate(self, symbol: str) -> bool:
        """설정 비활성화"""
        result = await self.db.execute(
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
            .values(is_active=False)
        )
        await self.db.commit()
        return result.rowcount > 0

    async def activate(self, symbol: str) -> bool:
        """설정 활성화"""
        result = await self.db.execute(
            update(SymbolTradeSettings)
            .where(SymbolTradeSettings.symbol == symbol)
            .values(is_active=True)
        )
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
    db: AsyncSession, symbol: str, price: float, fallback_amount: float
) -> int:
    """
    종목의 매수 수량 조회

    설정이 있으면 설정된 수량 반환, 없으면 금액 기반 계산

    Args:
        db: DB 세션
        symbol: 종목 코드
        price: 현재 가격
        fallback_amount: 설정이 없을 때 사용할 금액 (KRW/USD)

    Returns:
        매수 수량 (정수)
    """
    service = SymbolTradeSettingsService(db)
    settings = await service.get_by_symbol(symbol)

    if settings and settings.is_active:
        # 설정된 수량 사용
        return int(settings.buy_quantity_per_order)
    else:
        # 폴백: 금액 기반 수량 계산
        return int(fallback_amount / price) if price > 0 else 0


async def get_buy_quantity_for_crypto(
    db: AsyncSession, symbol: str, price: float, fallback_amount: float
) -> float:
    """
    코인의 매수 수량 조회 (소수점 지원)

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
        # 설정된 수량 사용
        return float(settings.buy_quantity_per_order)
    else:
        # 폴백: 금액 기반 수량 계산
        return fallback_amount / price if price > 0 else 0.0
