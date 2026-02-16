"""KOSPI200 구성종목 API Router"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.jobs.kospi200 import (
    sync_kospi200_to_stock_info_task,
    update_kospi200_constituents_task,
)
from app.models.kospi200 import Kospi200Constituent

router = APIRouter(prefix="/kospi200", tags=["KOSPI200"])


@router.get("/constituents")
async def get_kospi200_constituents(
    active_only: bool = Query(True, description="활성화된 종목만 조회"),
    sector: str | None = Query(None, description="섹터 필터"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    page_size: int = Query(50, ge=1, le=200, description="페이지 크기"),
    db: AsyncSession = Depends(get_db),
):
    """KOSPI200 구성종목 목록 조회"""

    query = select(Kospi200Constituent)

    if active_only:
        query = query.where(Kospi200Constituent.is_active == True)

    if sector:
        query = query.where(Kospi200Constituent.sector == sector)

    total_count = await db.scalar(select(func.count()).select_from(query.subquery()))

    query = query.order_by(Kospi200Constituent.weight.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    constituents = result.scalars().all()

    return {
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": (total_count + page_size - 1) // page_size,
        "constituents": [
            {
                "id": c.id,
                "stock_code": c.stock_code,
                "stock_name": c.stock_name,
                "market_cap": c.market_cap,
                "weight": c.weight,
                "sector": c.sector,
                "is_active": c.is_active,
                "added_at": c.added_at.isoformat() if c.added_at else None,
                "removed_at": c.removed_at.isoformat() if c.removed_at else None,
            }
            for c in constituents
        ],
    }


@router.get("/constituents/{stock_code}")
async def get_constituent_by_code(stock_code: str, db: AsyncSession = Depends(get_db)):
    """종목코드로 KOSPI200 구성종목 조회"""

    query = select(Kospi200Constituent).where(
        Kospi200Constituent.stock_code == stock_code
    )
    result = await db.execute(query)
    constituent = result.scalar_one_or_none()

    if not constituent:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")

    return {
        "id": constituent.id,
        "stock_code": constituent.stock_code,
        "stock_name": constituent.stock_name,
        "market_cap": constituent.market_cap,
        "weight": constituent.weight,
        "sector": constituent.sector,
        "is_active": constituent.is_active,
        "added_at": constituent.added_at.isoformat() if constituent.added_at else None,
        "removed_at": constituent.removed_at.isoformat()
        if constituent.removed_at
        else None,
        "created_at": constituent.created_at.isoformat()
        if constituent.created_at
        else None,
        "updated_at": constituent.updated_at.isoformat()
        if constituent.updated_at
        else None,
    }


@router.get("/statistics")
async def get_kospi200_statistics(db: AsyncSession = Depends(get_db)):
    """KOSPI200 통계 정보 조회"""

    query = select(Kospi200Constituent).where(Kospi200Constituent.is_active == True)

    total_constituents = await db.scalar(
        select(func.count()).select_from(query.subquery())
    )

    sectors = await db.execute(
        select(Kospi200Constituent.sector, func.count(Kospi200Constituent.id))
        .where(Kospi200Constituent.is_active == True)
        .group_by(Kospi200Constituent.sector)
    )
    sector_counts = {row[0]: row[1] for row in sectors.fetchall()}

    total_market_cap = await db.scalar(
        select(func.sum(Kospi200Constituent.market_cap)).where(
            Kospi200Constituent.is_active == True
        )
    )

    return {
        "total_constituents": total_constituents,
        "total_market_cap": total_market_cap,
        "sector_counts": sector_counts,
    }


@router.get("/sectors")
async def get_kospi200_sectors(db: AsyncSession = Depends(get_db)):
    """KOSPI200 섹터 목록 조회"""

    sectors = await db.execute(
        select(Kospi200Constituent.sector)
        .where(Kospi200Constituent.is_active == True)
        .where(Kospi200Constituent.sector.isnot(None))
        .distinct()
        .order_by(Kospi200Constituent.sector)
    )

    return {"sectors": [row[0] for row in sectors.fetchall()]}


@router.post("/update")
async def trigger_kospi200_update():
    """KOSPI200 구성종목 업데이트 트리거"""

    result = await update_kospi200_constituents_task()
    return {"success": True, **result}


@router.post("/sync")
async def trigger_kospi200_sync():
    """KOSPI200 구성종목을 StockInfo에 동기화 트리거"""

    result = await sync_kospi200_to_stock_info_task()
    return {"success": True, **result}
