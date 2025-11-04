"""보유 자산 관리 라우터"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.services.holdings_service import HoldingsService
from app.services.kis import KISClient
from app.core.config import settings

router = APIRouter(prefix="/holdings", tags=["holdings"])

# 템플릿 설정
templates = Jinja2Templates(directory="app/templates")


def get_holdings_service() -> HoldingsService:
    """HoldingsService 인스턴스 생성"""
    kis_client = KISClient()
    return HoldingsService(kis_client=kis_client)


@router.get("/", response_class=HTMLResponse)
async def holdings_dashboard(request: Request):
    """보유 자산 대시보드 페이지"""
    return templates.TemplateResponse(
        "holdings_dashboard.html",
        {"request": request}
    )


@router.post("/api/refresh")
async def refresh_holdings(
    is_mock: bool = Query(False, description="KIS 모의투자 여부"),
    db: AsyncSession = Depends(get_db),
    holdings_service: HoldingsService = Depends(get_holdings_service)
):
    """보유 자산 갱신

    KIS와 Upbit에서 현재 보유 자산을 가져와 데이터베이스를 업데이트합니다.
    """
    try:
        results = await holdings_service.fetch_and_update_all_holdings(
            db=db,
            user_id=1,  # 기본 사용자 ID
            is_mock=is_mock
        )
        return {
            "success": True,
            "message": "보유 자산 갱신 완료",
            "data": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"보유 자산 갱신 실패: {str(e)}")


@router.get("/api/list")
async def list_holdings(
    instrument_type: Optional[str] = Query(None, description="상품 타입 필터"),
    db: AsyncSession = Depends(get_db),
    holdings_service: HoldingsService = Depends(get_holdings_service)
):
    """보유 자산 목록 조회"""
    try:
        # instrument_type 필터 처리
        filter_type = None
        if instrument_type and instrument_type != "전체":
            try:
                filter_type = InstrumentType(instrument_type)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"잘못된 상품 타입: {instrument_type}")

        holdings = await holdings_service.get_all_holdings(
            db=db,
            user_id=1,
            instrument_type=filter_type
        )

        return {
            "success": True,
            "count": len(holdings),
            "data": holdings
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"보유 자산 조회 실패: {str(e)}")


@router.get("/api/statistics")
async def get_holdings_statistics(
    db: AsyncSession = Depends(get_db),
    holdings_service: HoldingsService = Depends(get_holdings_service)
):
    """보유 자산 통계"""
    try:
        # 전체 보유 자산 조회
        all_holdings = await holdings_service.get_all_holdings(db=db, user_id=1)

        # 타입별 통계
        kr_stocks = [h for h in all_holdings if h["instrument_type"] == "equity_kr"]
        us_stocks = [h for h in all_holdings if h["instrument_type"] == "equity_us"]
        crypto = [h for h in all_holdings if h["instrument_type"] == "crypto"]

        return {
            "success": True,
            "data": {
                "total_count": len(all_holdings),
                "kr_stocks_count": len(kr_stocks),
                "us_stocks_count": len(us_stocks),
                "crypto_count": len(crypto),
                "last_updated": all_holdings[0]["updated_at"] if all_holdings else None
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"통계 조회 실패: {str(e)}")


@router.get("/api/filters")
async def get_filters(
    db: AsyncSession = Depends(get_db),
    holdings_service: HoldingsService = Depends(get_holdings_service)
):
    """필터 옵션 조회"""
    try:
        all_holdings = await holdings_service.get_all_holdings(db=db, user_id=1)

        # 고유한 상품 타입 추출
        instrument_types = sorted(list(set(h["instrument_type"] for h in all_holdings)))

        # 고유한 거래소 추출
        exchanges = sorted(list(set(h["exchange_code"] for h in all_holdings)))

        return {
            "success": True,
            "data": {
                "instrument_types": instrument_types,
                "exchanges": exchanges
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"필터 옵션 조회 실패: {str(e)}")


@router.post("/api/sync-analysis")
async def sync_analysis_prices(
    db: AsyncSession = Depends(get_db),
    holdings_service: HoldingsService = Depends(get_holdings_service)
):
    """보유 자산의 목표가를 최신 분석 결과로 동기화"""
    try:
        from sqlalchemy import select, update
        from app.models.trading import UserWatchItem, Instrument
        from app.models.analysis import StockInfo, StockAnalysisResult

        # 모든 활성 보유 자산 조회
        stmt = (
            select(UserWatchItem, Instrument)
            .join(Instrument, UserWatchItem.instrument_id == Instrument.id)
            .where(UserWatchItem.user_id == 1)
            .where(UserWatchItem.is_active == True)
        )
        result = await db.execute(stmt)
        holdings = result.all()

        updated_count = 0
        skipped_count = 0
        errors = []

        for watch_item, instrument in holdings:
            try:
                # StockInfo 조회 (symbol로)
                stock_info_stmt = select(StockInfo).where(StockInfo.symbol == instrument.symbol)
                stock_info_result = await db.execute(stock_info_stmt)
                stock_info = stock_info_result.scalar_one_or_none()

                if not stock_info:
                    skipped_count += 1
                    continue

                # 최신 분석 결과 조회
                analysis_stmt = (
                    select(StockAnalysisResult)
                    .where(StockAnalysisResult.stock_info_id == stock_info.id)
                    .order_by(StockAnalysisResult.created_at.desc())
                    .limit(1)
                )
                analysis_result = await db.execute(analysis_stmt)
                analysis = analysis_result.scalar_one_or_none()

                if not analysis:
                    skipped_count += 1
                    continue

                # 목표가 업데이트
                update_stmt = (
                    update(UserWatchItem)
                    .where(UserWatchItem.id == watch_item.id)
                    .values(
                        desired_buy_px=analysis.buy_hope_min,
                        target_sell_px=analysis.sell_target_max,
                    )
                )
                await db.execute(update_stmt)
                updated_count += 1

            except Exception as e:
                errors.append(f"{instrument.symbol}: {str(e)}")

        await db.commit()

        return {
            "success": True,
            "message": "분석 데이터 동기화 완료",
            "data": {
                "updated": updated_count,
                "skipped": skipped_count,
                "errors": errors
            }
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"분석 동기화 실패: {str(e)}")
