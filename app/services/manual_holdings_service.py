"""
Manual Holdings Service

수동 보유 종목 관리 서비스
"""
import logging
from typing import Dict, List, Optional, Any

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.manual_holdings import (
    BrokerAccount,
    BrokerType,
    ManualHolding,
    MarketType,
)

logger = logging.getLogger(__name__)


class ManualHoldingsService:
    """수동 보유 종목 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_holding(
        self,
        broker_account_id: int,
        ticker: str,
        market_type: MarketType,
        quantity: float,
        avg_price: float,
        display_name: Optional[str] = None,
    ) -> ManualHolding:
        """새 보유 종목 등록"""
        holding = ManualHolding(
            broker_account_id=broker_account_id,
            ticker=ticker.upper(),
            market_type=market_type,
            quantity=quantity,
            avg_price=avg_price,
            display_name=display_name,
        )
        self.db.add(holding)
        await self.db.commit()
        await self.db.refresh(holding)
        logger.info(
            f"Created manual holding: account_id={broker_account_id}, "
            f"ticker={ticker}, qty={quantity}"
        )
        return holding

    async def get_holding_by_id(
        self, holding_id: int
    ) -> Optional[ManualHolding]:
        """ID로 보유 종목 조회"""
        result = await self.db.execute(
            select(ManualHolding)
            .where(ManualHolding.id == holding_id)
            .options(selectinload(ManualHolding.broker_account))
        )
        return result.scalar_one_or_none()

    async def get_holdings_by_account(
        self, broker_account_id: int
    ) -> List[ManualHolding]:
        """브로커 계좌별 보유 종목 조회"""
        result = await self.db.execute(
            select(ManualHolding)
            .where(ManualHolding.broker_account_id == broker_account_id)
            .order_by(ManualHolding.ticker)
        )
        return list(result.scalars().all())

    async def get_holdings_by_user(
        self,
        user_id: int,
        market_type: Optional[MarketType] = None,
        broker_type: Optional[BrokerType] = None,
    ) -> List[ManualHolding]:
        """사용자별 모든 보유 종목 조회"""
        query = (
            select(ManualHolding)
            .join(BrokerAccount)
            .where(BrokerAccount.user_id == user_id)
            .where(BrokerAccount.is_active == True)  # noqa: E712
            .options(selectinload(ManualHolding.broker_account))
        )

        if market_type:
            query = query.where(ManualHolding.market_type == market_type)

        if broker_type:
            query = query.where(BrokerAccount.broker_type == broker_type)

        result = await self.db.execute(
            query.order_by(ManualHolding.ticker)
        )
        return list(result.scalars().all())

    async def get_holding_by_ticker(
        self,
        broker_account_id: int,
        ticker: str,
        market_type: MarketType,
    ) -> Optional[ManualHolding]:
        """티커로 보유 종목 조회"""
        result = await self.db.execute(
            select(ManualHolding)
            .where(ManualHolding.broker_account_id == broker_account_id)
            .where(ManualHolding.ticker == ticker.upper())
            .where(ManualHolding.market_type == market_type)
        )
        return result.scalar_one_or_none()

    async def get_holdings_by_ticker_all_accounts(
        self,
        user_id: int,
        ticker: str,
        market_type: MarketType,
    ) -> List[ManualHolding]:
        """특정 티커의 모든 계좌 보유 종목 조회"""
        result = await self.db.execute(
            select(ManualHolding)
            .join(BrokerAccount)
            .where(BrokerAccount.user_id == user_id)
            .where(BrokerAccount.is_active == True)  # noqa: E712
            .where(ManualHolding.ticker == ticker.upper())
            .where(ManualHolding.market_type == market_type)
            .options(selectinload(ManualHolding.broker_account))
        )
        return list(result.scalars().all())

    async def update_holding(
        self, holding_id: int, **kwargs
    ) -> Optional[ManualHolding]:
        """보유 종목 업데이트"""
        holding = await self.get_holding_by_id(holding_id)
        if not holding:
            return None

        for key, value in kwargs.items():
            if hasattr(holding, key) and key not in (
                "id", "broker_account_id", "created_at"
            ):
                if key == "ticker" and value:
                    value = value.upper()
                setattr(holding, key, value)

        await self.db.commit()
        await self.db.refresh(holding)
        return holding

    async def delete_holding(self, holding_id: int) -> bool:
        """보유 종목 삭제"""
        holding = await self.get_holding_by_id(holding_id)
        if not holding:
            return False

        await self.db.delete(holding)
        await self.db.commit()
        logger.info(f"Deleted manual holding: id={holding_id}")
        return True

    async def upsert_holding(
        self,
        broker_account_id: int,
        ticker: str,
        market_type: MarketType,
        quantity: float,
        avg_price: float,
        display_name: Optional[str] = None,
    ) -> ManualHolding:
        """보유 종목 등록 또는 업데이트 (upsert)"""
        existing = await self.get_holding_by_ticker(
            broker_account_id, ticker, market_type
        )

        if existing:
            existing.quantity = quantity
            existing.avg_price = avg_price
            if display_name is not None:
                existing.display_name = display_name
            await self.db.commit()
            await self.db.refresh(existing)
            return existing

        return await self.create_holding(
            broker_account_id, ticker, market_type,
            quantity, avg_price, display_name
        )

    async def bulk_create_holdings(
        self,
        broker_account_id: int,
        holdings_data: List[Dict[str, Any]],
    ) -> List[ManualHolding]:
        """여러 보유 종목 일괄 등록"""
        created = []
        for data in holdings_data:
            holding = await self.upsert_holding(
                broker_account_id=broker_account_id,
                ticker=data["ticker"],
                market_type=data["market_type"],
                quantity=data["quantity"],
                avg_price=data["avg_price"],
                display_name=data.get("display_name"),
            )
            created.append(holding)
        return created

    async def get_holdings_summary_by_user(
        self, user_id: int
    ) -> Dict[str, Any]:
        """사용자별 보유 종목 요약"""
        holdings = await self.get_holdings_by_user(user_id)

        summary = {
            "total_count": len(holdings),
            "by_market": {},
            "by_broker": {},
        }

        for holding in holdings:
            # 시장별 집계
            market = holding.market_type.value
            if market not in summary["by_market"]:
                summary["by_market"][market] = 0
            summary["by_market"][market] += 1

            # 브로커별 집계
            broker = holding.broker_account.broker_type.value
            if broker not in summary["by_broker"]:
                summary["by_broker"][broker] = 0
            summary["by_broker"][broker] += 1

        return summary
