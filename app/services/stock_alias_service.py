"""
Stock Alias Service

종목 별칭 관리 서비스
"""
import logging
from typing import Dict, List, Optional, Any

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manual_holdings import StockAlias, MarketType

logger = logging.getLogger(__name__)


class StockAliasService:
    """종목 별칭 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_alias(
        self,
        ticker: str,
        market_type: MarketType,
        alias: str,
        source: str = "user",
    ) -> StockAlias:
        """새 종목 별칭 등록"""
        stock_alias = StockAlias(
            ticker=ticker.upper(),
            market_type=market_type,
            alias=alias,
            source=source,
        )
        self.db.add(stock_alias)
        await self.db.commit()
        await self.db.refresh(stock_alias)
        logger.info(
            f"Created stock alias: ticker={ticker}, alias={alias}"
        )
        return stock_alias

    async def get_alias_by_id(self, alias_id: int) -> Optional[StockAlias]:
        """ID로 별칭 조회"""
        result = await self.db.execute(
            select(StockAlias).where(StockAlias.id == alias_id)
        )
        return result.scalar_one_or_none()

    async def get_ticker_by_alias(
        self, alias: str, market_type: MarketType
    ) -> Optional[str]:
        """별칭으로 티커 조회"""
        result = await self.db.execute(
            select(StockAlias)
            .where(StockAlias.alias == alias)
            .where(StockAlias.market_type == market_type)
        )
        stock_alias = result.scalar_one_or_none()
        return stock_alias.ticker if stock_alias else None

    async def search_by_alias(
        self,
        query: str,
        market_type: Optional[MarketType] = None,
        limit: int = 20,
    ) -> List[StockAlias]:
        """별칭 검색 (부분 일치)"""
        search_query = select(StockAlias).where(
            or_(
                StockAlias.alias.ilike(f"%{query}%"),
                StockAlias.ticker.ilike(f"%{query}%"),
            )
        )

        if market_type:
            search_query = search_query.where(
                StockAlias.market_type == market_type
            )

        result = await self.db.execute(
            search_query.order_by(StockAlias.alias).limit(limit)
        )
        return list(result.scalars().all())

    async def get_aliases_by_ticker(
        self, ticker: str, market_type: Optional[MarketType] = None
    ) -> List[StockAlias]:
        """티커의 모든 별칭 조회"""
        query = select(StockAlias).where(
            StockAlias.ticker == ticker.upper()
        )

        if market_type:
            query = query.where(StockAlias.market_type == market_type)

        result = await self.db.execute(query.order_by(StockAlias.alias))
        return list(result.scalars().all())

    async def bulk_create_aliases(
        self, aliases_data: List[Dict[str, Any]]
    ) -> List[StockAlias]:
        """여러 별칭 일괄 등록 (중복 무시)"""
        created = []
        for data in aliases_data:
            ticker = data["ticker"].upper()
            alias = data["alias"]
            market_type = data["market_type"]
            source = data.get("source", "user")

            # 이미 존재하는지 확인
            existing = await self.db.execute(
                select(StockAlias)
                .where(StockAlias.alias == alias)
                .where(StockAlias.market_type == market_type)
            )
            if existing.scalar_one_or_none():
                continue

            stock_alias = StockAlias(
                ticker=ticker,
                market_type=market_type,
                alias=alias,
                source=source,
            )
            self.db.add(stock_alias)
            created.append(stock_alias)

        if created:
            await self.db.commit()
            for alias in created:
                await self.db.refresh(alias)

        return created

    async def delete_alias(self, alias_id: int) -> bool:
        """별칭 삭제"""
        alias = await self.get_alias_by_id(alias_id)
        if not alias:
            return False

        await self.db.delete(alias)
        await self.db.commit()
        logger.info(f"Deleted stock alias: id={alias_id}")
        return True

    async def resolve_ticker(
        self,
        name_or_ticker: str,
        market_type: MarketType,
    ) -> str:
        """이름이나 티커를 정규 티커로 변환"""
        # 1. 먼저 별칭에서 검색
        ticker = await self.get_ticker_by_alias(name_or_ticker, market_type)
        if ticker:
            return ticker

        # 2. 없으면 입력값을 대문자로 반환 (티커로 가정)
        return name_or_ticker.upper()


# 토스 종목명 -> 실제 티커 매핑 기본 데이터
TOSS_STOCK_ALIASES = [
    # 해외주식
    {"ticker": "BRK.B", "alias": "버크셔 해서웨이 B", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "TSMC", "alias": "TSMC(ADR)", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "QQQM", "alias": "QQQM", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "QQQ", "alias": "QQQ", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "SPYM", "alias": "SPYM", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "VOO", "alias": "VOO", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "PLTR", "alias": "팔란티어", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "IVV", "alias": "IVV", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "AAPL", "alias": "애플", "market_type": MarketType.US, "source": "toss"},
    {"ticker": "ETHU", "alias": "ETHU", "market_type": MarketType.US, "source": "toss"},
    # 국내주식
    {"ticker": "068270", "alias": "셀트리온", "market_type": MarketType.KR, "source": "toss"},
    {"ticker": "005930", "alias": "삼성전자", "market_type": MarketType.KR, "source": "toss"},
    {"ticker": "000660", "alias": "SK하이닉스", "market_type": MarketType.KR, "source": "toss"},
    {"ticker": "207940", "alias": "삼성바이오로직스", "market_type": MarketType.KR, "source": "toss"},
    {"ticker": "373220", "alias": "LG에너지솔루션", "market_type": MarketType.KR, "source": "toss"},
]


async def seed_toss_aliases(db: AsyncSession) -> int:
    """토스 종목명 별칭 초기 데이터 시딩"""
    service = StockAliasService(db)
    created = await service.bulk_create_aliases(TOSS_STOCK_ALIASES)
    return len(created)
