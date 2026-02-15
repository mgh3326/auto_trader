import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.analysis import StockAnalysisResult, StockInfo
from app.tasks.analyze import run_analysis_for_stock

router = APIRouter(prefix="/stock-latest", tags=["Stock Latest Analysis"])


def _normalize_reasons(raw_reasons) -> list[str]:
    """Convert stored reasons to a string list, handling JSON/text/null."""
    if raw_reasons is None:
        return []
    if isinstance(raw_reasons, list):
        return [str(r) for r in raw_reasons]
    if isinstance(raw_reasons, str):
        try:
            parsed = json.loads(raw_reasons)
            if isinstance(parsed, list):
                return [str(r) for r in parsed]
            return [str(parsed)]
        except Exception:
            return [raw_reasons]
    return []


@router.get("/", response_class=HTMLResponse)
async def stock_latest_dashboard(request: Request):
    """종목별 최신 분석 결과 대시보드 페이지"""
    return templates.TemplateResponse(
        "stock_latest_dashboard.html",
        {"request": request, "user": getattr(request.state, "user", None)},
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def stock_latest_dashboard_page(request: Request):
    """종목별 최신 분석 결과 대시보드 HTML 페이지"""
    return templates.TemplateResponse(
        "stock_latest_dashboard.html",
        {"request": request, "user": getattr(request.state, "user", None)},
    )


@router.get("/api/latest-results")
async def get_latest_analysis_results(
    db: AsyncSession = Depends(get_db),
    instrument_type: str | None = Query(None, description="상품 타입 필터"),
    symbol: str | None = Query(None, description="종목 코드 필터"),
    decision: str | None = Query(None, description="투자 결정 필터"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """종목별 최신 분석 결과를 조회하는 API"""

    # 최신 분석 결과 서브쿼리 (STOCK_INFO_GUIDE.md 참고)
    latest_analysis_subquery = (
        select(
            StockAnalysisResult.stock_info_id,
            func.max(StockAnalysisResult.created_at).label("latest_created_at"),
        )
        .group_by(StockAnalysisResult.stock_info_id)
        .subquery()
    )

    # 메인 쿼리: StockInfo와 최신 분석 결과 JOIN
    base_query = (
        select(StockInfo, StockAnalysisResult)
        .join(
            latest_analysis_subquery,
            StockInfo.id == latest_analysis_subquery.c.stock_info_id,
        )
        .join(
            StockAnalysisResult,
            (StockAnalysisResult.stock_info_id == StockInfo.id)
            & (
                StockAnalysisResult.created_at
                == latest_analysis_subquery.c.latest_created_at
            ),
        )
        .where(StockInfo.is_active == True)
    )

    # 필터 적용
    if instrument_type and instrument_type != "전체":
        base_query = base_query.where(StockInfo.instrument_type == instrument_type)

    if symbol and symbol != "전체":
        base_query = base_query.where(StockInfo.symbol.ilike(f"%{symbol}%"))

    if decision and decision != "전체":
        base_query = base_query.where(StockAnalysisResult.decision == decision)

    # 전체 개수 조회
    count_subquery = base_query.subquery()
    count_query = select(func.count()).select_from(count_subquery)
    total_count = await db.scalar(count_query)

    # 페이지네이션 적용 (최신 분석 순으로 정렬)
    query = base_query.order_by(latest_analysis_subquery.c.latest_created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    # 결과 조회
    result = await db.execute(query)
    results = result.fetchall()

    # 응답 데이터 구성
    latest_results = []
    for stock_info, analysis_result in results:
        latest_results.append(
            {
                "stock_info_id": stock_info.id,
                "symbol": stock_info.symbol,
                "name": stock_info.name,
                "instrument_type": stock_info.instrument_type,
                "exchange": stock_info.exchange,
                "sector": stock_info.sector,
                "latest_analysis": {
                    "id": analysis_result.id,
                    "model_name": analysis_result.model_name,
                    "decision": analysis_result.decision,
                    "confidence": analysis_result.confidence,
                    "appropriate_buy_min": analysis_result.appropriate_buy_min,
                    "appropriate_buy_max": analysis_result.appropriate_buy_max,
                    "appropriate_sell_min": analysis_result.appropriate_sell_min,
                    "appropriate_sell_max": analysis_result.appropriate_sell_max,
                    "buy_hope_min": analysis_result.buy_hope_min,
                    "buy_hope_max": analysis_result.buy_hope_max,
                    "sell_target_min": analysis_result.sell_target_min,
                    "sell_target_max": analysis_result.sell_target_max,
                    "created_at": analysis_result.created_at.isoformat()
                    if analysis_result.created_at
                    else None,
                },
            }
        )

    return {
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": (total_count + page_size - 1) // page_size,
        "results": latest_results,
    }


@router.get("/api/stock/{stock_info_id}/history")
async def get_stock_analysis_history(
    stock_info_id: int,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="페이지 번호"),
    page_size: int = Query(10, ge=1, le=50, description="페이지 크기"),
):
    """특정 종목의 분석 이력을 조회하는 API"""

    # 종목 정보 확인
    stock_info_query = select(StockInfo).where(StockInfo.id == stock_info_id)
    stock_info_result = await db.execute(stock_info_query)
    stock_info = stock_info_result.scalar_one_or_none()

    if not stock_info:
        return {"error": "종목을 찾을 수 없습니다."}

    # 분석 이력 조회
    history_query = (
        select(StockAnalysisResult)
        .where(StockAnalysisResult.stock_info_id == stock_info_id)
        .order_by(StockAnalysisResult.created_at.desc())
    )

    # 전체 개수 조회
    count_query = select(func.count(StockAnalysisResult.id)).where(
        StockAnalysisResult.stock_info_id == stock_info_id
    )
    total_count = await db.scalar(count_query)

    # 페이지네이션 적용
    history_query = history_query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(history_query)
    analysis_history = result.scalars().all()

    # 응답 데이터 구성
    history_results = []
    for analysis in analysis_history:
        reasons = _normalize_reasons(analysis.reasons)

        history_results.append(
            {
                "id": analysis.id,
                "model_name": analysis.model_name,
                "decision": analysis.decision,
                "confidence": analysis.confidence,
                "appropriate_buy_range": {
                    "min": analysis.appropriate_buy_min,
                    "max": analysis.appropriate_buy_max,
                },
                "appropriate_sell_range": {
                    "min": analysis.appropriate_sell_min,
                    "max": analysis.appropriate_sell_max,
                },
                "buy_hope_range": {
                    "min": analysis.buy_hope_min,
                    "max": analysis.buy_hope_max,
                },
                "sell_target_range": {
                    "min": analysis.sell_target_min,
                    "max": analysis.sell_target_max,
                },
                "reasons": reasons,
                "detailed_text": analysis.detailed_text,
                "created_at": analysis.created_at.isoformat()
                if analysis.created_at
                else None,
            }
        )

    return {
        "stock_info": {
            "id": stock_info.id,
            "symbol": stock_info.symbol,
            "name": stock_info.name,
            "instrument_type": stock_info.instrument_type,
            "exchange": stock_info.exchange,
            "sector": stock_info.sector,
        },
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": (total_count + page_size - 1) // page_size,
        "history": history_results,
    }


@router.get("/api/filters")
async def get_filter_options(db: AsyncSession = Depends(get_db)):
    """필터 옵션을 조회하는 API"""

    # 상품 타입 옵션 (StockInfo에서 조회)
    instrument_types = await db.execute(
        select(StockInfo.instrument_type)
        .distinct()
        .where(StockInfo.is_active == True)
        .where(StockInfo.instrument_type.isnot(None))
    )
    instrument_type_options = [row[0] for row in instrument_types.fetchall()]

    # 종목 코드 옵션 (StockInfo에서 조회)
    symbols = await db.execute(
        select(StockInfo.symbol)
        .distinct()
        .where(StockInfo.is_active == True)
        .where(StockInfo.symbol.isnot(None))
    )
    symbol_options = [row[0] for row in symbols.fetchall()]

    # 투자 결정 옵션 (최신 분석 결과에서 조회)
    latest_analysis_subquery = (
        select(
            StockAnalysisResult.stock_info_id,
            func.max(StockAnalysisResult.created_at).label("latest_created_at"),
        )
        .group_by(StockAnalysisResult.stock_info_id)
        .subquery()
    )

    decisions = await db.execute(
        select(StockAnalysisResult.decision)
        .distinct()
        .join(
            latest_analysis_subquery,
            (
                StockAnalysisResult.stock_info_id
                == latest_analysis_subquery.c.stock_info_id
            )
            & (
                StockAnalysisResult.created_at
                == latest_analysis_subquery.c.latest_created_at
            ),
        )
        .where(StockAnalysisResult.decision.isnot(None))
    )
    decision_options = [row[0] for row in decisions.fetchall()]

    return {
        "instrument_types": instrument_type_options,
        "symbols": symbol_options,
        "decisions": decision_options,
    }


@router.get("/api/analyze-status/{stock_info_id}")
async def get_analysis_status(stock_info_id: int, db: AsyncSession = Depends(get_db)):
    """특정 종목의 분석 상태를 확인하는 API"""

    # 종목 정보 확인
    stock_info_query = select(StockInfo).where(StockInfo.id == stock_info_id)
    stock_info_result = await db.execute(stock_info_query)
    stock_info = stock_info_result.scalar_one_or_none()

    if not stock_info:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")

    # 최신 분석 결과 조회
    latest_analysis_query = (
        select(StockAnalysisResult)
        .where(StockAnalysisResult.stock_info_id == stock_info_id)
        .order_by(StockAnalysisResult.created_at.desc())
        .limit(1)
    )

    latest_result = await db.execute(latest_analysis_query)
    latest_analysis = latest_result.scalar_one_or_none()

    return {
        "stock_info": {
            "id": stock_info.id,
            "symbol": stock_info.symbol,
            "name": stock_info.name,
            "instrument_type": stock_info.instrument_type,
        },
        "latest_analysis": {
            "id": latest_analysis.id,
            "created_at": latest_analysis.created_at.isoformat()
            if latest_analysis.created_at
            else None,
            "model_name": latest_analysis.model_name,
            "decision": latest_analysis.decision,
            "confidence": latest_analysis.confidence,
        }
        if latest_analysis
        else None,
    }


@router.get("/api/statistics")
async def get_latest_analysis_statistics(db: AsyncSession = Depends(get_db)):
    """최신 분석 통계를 조회하는 API"""

    # 최신 분석 결과 서브쿼리
    latest_analysis_subquery = (
        select(
            StockAnalysisResult.stock_info_id,
            func.max(StockAnalysisResult.created_at).label("latest_created_at"),
        )
        .group_by(StockAnalysisResult.stock_info_id)
        .subquery()
    )

    # 활성 종목 수
    active_stocks_count = await db.scalar(
        select(func.count(StockInfo.id)).where(StockInfo.is_active == True)
    )

    # 최신 분석이 있는 종목 수
    analyzed_stocks_subquery = (
        select(StockInfo.id)
        .join(
            latest_analysis_subquery,
            StockInfo.id == latest_analysis_subquery.c.stock_info_id,
        )
        .where(StockInfo.is_active == True)
        .subquery()
    )
    analyzed_stocks_count = await db.scalar(
        select(func.count()).select_from(analyzed_stocks_subquery)
    )

    # 투자 결정별 통계 (최신 분석 기준)
    decision_stats = await db.execute(
        select(StockAnalysisResult.decision, func.count().label("count"))
        .join(
            latest_analysis_subquery,
            (
                StockAnalysisResult.stock_info_id
                == latest_analysis_subquery.c.stock_info_id
            )
            & (
                StockAnalysisResult.created_at
                == latest_analysis_subquery.c.latest_created_at
            ),
        )
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
        .group_by(StockAnalysisResult.decision)
    )
    decision_counts = {row[0]: row[1] for row in decision_stats.fetchall()}

    # 상품 타입별 통계
    instrument_stats = await db.execute(
        select(StockInfo.instrument_type, func.count().label("count"))
        .join(
            latest_analysis_subquery,
            StockInfo.id == latest_analysis_subquery.c.stock_info_id,
        )
        .where(StockInfo.is_active == True)
        .group_by(StockInfo.instrument_type)
    )
    instrument_counts = {row[0]: row[1] for row in instrument_stats.fetchall()}

    # 평균 신뢰도 (최신 분석 기준)
    avg_confidence = await db.scalar(
        select(func.avg(StockAnalysisResult.confidence))
        .join(
            latest_analysis_subquery,
            (
                StockAnalysisResult.stock_info_id
                == latest_analysis_subquery.c.stock_info_id
            )
            & (
                StockAnalysisResult.created_at
                == latest_analysis_subquery.c.latest_created_at
            ),
        )
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
        .where(StockAnalysisResult.confidence.isnot(None))
    )

    return {
        "active_stocks_count": active_stocks_count,
        "analyzed_stocks_count": analyzed_stocks_count,
        "decision_counts": decision_counts,
        "instrument_counts": instrument_counts,
        "average_confidence": round(avg_confidence, 2) if avg_confidence else 0,
    }


@router.post("/api/analyze/{stock_info_id}")
async def trigger_new_analysis(stock_info_id: int, db: AsyncSession = Depends(get_db)):
    # 종목 정보 확인
    stock_info_query = select(StockInfo).where(StockInfo.id == stock_info_id)
    stock_info_result = await db.execute(stock_info_query)
    stock_info = stock_info_result.scalar_one_or_none()

    if not stock_info:
        raise HTTPException(status_code=404, detail="종목을 찾을 수 없습니다.")

    if not stock_info.is_active:
        raise HTTPException(status_code=400, detail="비활성화된 종목입니다.")

    result = await run_analysis_for_stock(
        stock_info.symbol, stock_info.name, stock_info.instrument_type
    )

    return {
        "success": True,
        **result,
        "stock_info": {
            "id": stock_info.id,
            "symbol": stock_info.symbol,
            "name": stock_info.name,
            "instrument_type": stock_info.instrument_type,
        },
    }
