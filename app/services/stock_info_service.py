from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analysis import StockAnalysisResult, StockInfo


class StockInfoService:
    """주식 정보 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_stock_info(self, stock_data: dict[str, Any]) -> StockInfo:
        """새로운 주식 정보 생성"""
        stock_info = StockInfo(**stock_data)
        self.db.add(stock_info)
        await self.db.commit()
        await self.db.refresh(stock_info)
        return stock_info

    async def get_stock_info_by_symbol(self, symbol: str) -> StockInfo | None:
        """심볼로 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(StockInfo.symbol == symbol)
        )
        return result.scalar_one_or_none()

    async def get_stock_info_by_id(self, stock_info_id: int) -> StockInfo | None:
        """ID로 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(StockInfo.id == stock_info_id)
        )
        return result.scalar_one_or_none()

    async def update_stock_info(
        self, stock_info_id: int, update_data: dict[str, Any]
    ) -> StockInfo | None:
        """주식 정보 업데이트"""
        await self.db.execute(
            update(StockInfo).where(StockInfo.id == stock_info_id).values(**update_data)
        )
        await self.db.commit()
        return await self.get_stock_info_by_id(stock_info_id)


# 편의 함수들
async def create_stock_if_not_exists(
    symbol: str,
    name: str,
    instrument_type: str,
    db: AsyncSession | None = None,
    **kwargs,
) -> StockInfo:
    """주식이 존재하지 않으면 생성하고, 존재하면 반환

    Parameters
    ----------
    symbol : str
        종목 심볼
    name : str
        종목명
    instrument_type : str
        종목 타입
    db : AsyncSession | None
        외부에서 주입된 세션. 제공되면 해당 세션을 사용하고 커밋하지 않음.
        제공되지 않으면 자체 세션을 생성하고 커밋함.
    **kwargs
        추가 필드

    Returns
    -------
    StockInfo
        생성되거나 조회된 StockInfo
    """
    if db is not None:
        # 외부 세션 사용 - 커밋하지 않음 (호출자가 트랜잭션 관리)
        existing_stock = await db.execute(
            select(StockInfo).where(StockInfo.symbol == symbol)
        )
        stock = existing_stock.scalar_one_or_none()
        if stock:
            return stock

        stock_data = {
            "symbol": symbol,
            "name": name,
            "instrument_type": instrument_type,
            **kwargs,
        }
        new_stock = StockInfo(**stock_data)
        db.add(new_stock)
        await db.flush()  # ID 생성을 위해 flush, 커밋은 호출자가 함
        return new_stock

    # 자체 세션 사용 - 독립적으로 커밋
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as own_db:
        service = StockInfoService(own_db)

        existing_stock = await service.get_stock_info_by_symbol(symbol)
        if existing_stock:
            return existing_stock

        stock_data = {
            "symbol": symbol,
            "name": name,
            "instrument_type": instrument_type,
            **kwargs,
        }

        return await service.create_stock_info(stock_data)


class StockAnalysisService:
    """주식 분석 결과 관리 서비스"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_latest_analysis_by_symbol(
        self, symbol: str
    ) -> StockAnalysisResult | None:
        """심볼로 최신 분석 결과 조회"""
        result = await self.db.execute(
            select(StockAnalysisResult)
            .join(StockInfo)
            .where(StockInfo.symbol == symbol)
            .order_by(desc(StockAnalysisResult.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_analysis_results_for_coins(
        self, coin_symbols: list[str]
    ) -> dict[str, StockAnalysisResult | None]:
        """여러 코인의 최신 분석 결과를 한 번에 조회"""
        if not coin_symbols:
            return {}

        # PostgreSQL DISTINCT ON 사용
        stmt = (
            select(StockAnalysisResult)
            .join(StockInfo)
            .where(StockInfo.symbol.in_(coin_symbols))
            .order_by(StockInfo.symbol, desc(StockAnalysisResult.created_at))
            .distinct(StockInfo.symbol)
        )

        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        results = dict.fromkeys(coin_symbols)
        for _row in rows:
            # row.stock_info might not be loaded if not requested, but we joined it.
            # However, we need the symbol to map back.
            # Since we joined, we can access it if we eager load or if we select it.
            # Let's select it explicitly or rely on lazy loading (which might be N+1 if not careful).
            # Better to select both.
            pass

        # Re-write query to select symbol too or use options
        from sqlalchemy.orm import selectinload

        stmt = (
            select(StockAnalysisResult)
            .join(StockInfo)
            .where(StockInfo.symbol.in_(coin_symbols))
            .order_by(StockInfo.symbol, desc(StockAnalysisResult.created_at))
            .distinct(StockInfo.symbol)
            .options(selectinload(StockAnalysisResult.stock_info))
        )

        result = await self.db.execute(stmt)
        rows = result.scalars().all()

        results = dict.fromkeys(coin_symbols)
        for row in rows:
            if row.stock_info:
                results[row.stock_info.symbol] = row

        return results
