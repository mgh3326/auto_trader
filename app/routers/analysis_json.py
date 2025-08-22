from typing import List, Optional
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.analysis import StockAnalysisResult
from app.models.base import Base

router = APIRouter(prefix="/analysis-json", tags=["JSON Analysis Results"])

# 템플릿 설정
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def analysis_json_dashboard(request: Request):
    """JSON 분석 결과 대시보드 페이지"""
    return templates.TemplateResponse("analysis_json_dashboard.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def analysis_json_dashboard_page(request: Request):
    """JSON 분석 결과 대시보드 HTML 페이지"""
    return templates.TemplateResponse("analysis_json_dashboard.html", {"request": request})


@router.get("/api/results")
async def get_analysis_results(
    db: AsyncSession = Depends(get_db),
    instrument_type: Optional[str] = Query(None, description="상품 타입 필터"),
    symbol: Optional[str] = Query(None, description="종목 코드 필터"),
    model_name: Optional[str] = Query(None, description="모델명 필터"),
    decision: Optional[str] = Query(None, description="투자 결정 필터"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """JSON 분석 결과를 조회하는 API"""
    
    # 기본 쿼리 생성
    query = select(StockAnalysisResult)
    
    # 필터 적용
    if instrument_type and instrument_type != "전체":
        query = query.where(StockAnalysisResult.instrument_type == instrument_type)
    
    if symbol and symbol != "전체":
        query = query.where(StockAnalysisResult.symbol == symbol)
    
    if model_name and model_name != "전체":
        query = query.where(StockAnalysisResult.model_name == model_name)
    
    if decision and decision != "전체":
        query = query.where(StockAnalysisResult.decision == decision)
    
    # 전체 개수 조회
    count_query = select(func.count(StockAnalysisResult.id))
    if instrument_type and instrument_type != "전체":
        count_query = count_query.where(StockAnalysisResult.instrument_type == instrument_type)
    if symbol and symbol != "전체":
        count_query = count_query.where(StockAnalysisResult.symbol == symbol)
    if model_name and model_name != "전체":
        count_query = count_query.where(StockAnalysisResult.model_name == model_name)
    if decision and decision != "전체":
        count_query = count_query.where(StockAnalysisResult.decision == decision)
    
    total_count = await db.scalar(count_query)
    
    # 페이지네이션 적용
    query = query.order_by(StockAnalysisResult.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    # 결과 조회
    result = await db.execute(query)
    results = result.scalars().all()
    
    # 응답 데이터 구성
    analysis_results = []
    for row in results:
        analysis_results.append({
            "id": row.id,
            "symbol": row.symbol,
            "name": row.name,
            "instrument_type": row.instrument_type,
            "model_name": row.model_name,
            "decision": row.decision,
            "confidence": row.confidence,
            "appropriate_buy_min": row.appropriate_buy_min,
            "appropriate_buy_max": row.appropriate_buy_max,
            "appropriate_sell_min": row.appropriate_sell_min,
            "appropriate_sell_max": row.appropriate_sell_max,
            "buy_hope_min": row.buy_hope_min,
            "buy_hope_max": row.buy_hope_max,
            "sell_target_min": row.sell_target_min,
            "sell_target_max": row.sell_target_max,
            "reasons": row.reasons,
            "detailed_text": row.detailed_text,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        })
    
    return {
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": (total_count + page_size - 1) // page_size,
        "results": analysis_results
    }


@router.get("/api/detail/{result_id}")
async def get_analysis_detail(
    result_id: int,
    db: AsyncSession = Depends(get_db)
):
    """특정 분석 결과의 상세 정보를 조회하는 API"""
    
    query = select(StockAnalysisResult).where(StockAnalysisResult.id == result_id)
    result = await db.execute(query)
    row = result.scalar_one_or_none()
    
    if not row:
        return {"error": "분석 결과를 찾을 수 없습니다."}
    
    # 근거를 JSON에서 파싱
    import json
    reasons = []
    try:
        if row.reasons:
            reasons = json.loads(row.reasons)
    except:
        reasons = []
    
    return {
        "id": row.id,
        "symbol": row.symbol,
        "name": row.name,
        "instrument_type": row.instrument_type,
        "model_name": row.model_name,
        "decision": row.decision,
        "confidence": row.confidence,
        "appropriate_buy_range": {
            "min": row.appropriate_buy_min,
            "max": row.appropriate_buy_max
        },
        "appropriate_sell_range": {
            "min": row.appropriate_sell_min,
            "max": row.appropriate_sell_max
        },
        "buy_hope_range": {
            "min": row.buy_hope_min,
            "max": row.buy_hope_max
        },
        "sell_target_range": {
            "min": row.sell_target_min,
            "max": row.sell_target_max
        },
        "reasons": reasons,
        "detailed_text": row.detailed_text,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "prompt": row.prompt
    }


@router.get("/api/filters")
async def get_filter_options(db: AsyncSession = Depends(get_db)):
    """필터 옵션을 조회하는 API"""
    
    # 상품 타입 옵션
    instrument_types = await db.execute(
        select(StockAnalysisResult.instrument_type)
        .distinct()
        .where(StockAnalysisResult.instrument_type.isnot(None))
    )
    instrument_type_options = [row[0] for row in instrument_types.fetchall()]
    
    # 종목 코드 옵션
    symbols = await db.execute(
        select(StockAnalysisResult.symbol)
        .distinct()
        .where(StockAnalysisResult.symbol.isnot(None))
    )
    symbol_options = [row[0] for row in symbols.fetchall()]
    
    # 모델명 옵션
    model_names = await db.execute(
        select(StockAnalysisResult.model_name)
        .distinct()
        .where(StockAnalysisResult.model_name.isnot(None))
    )
    model_name_options = [row[0] for row in model_names.fetchall()]
    
    return {
        "instrument_types": instrument_type_options,
        "symbols": symbol_options,
        "model_names": model_name_options
    }


@router.get("/api/statistics")
async def get_analysis_statistics(db: AsyncSession = Depends(get_db)):
    """분석 통계를 조회하는 API"""
    
    # 전체 분석 개수
    total_count = await db.scalar(select(func.count(StockAnalysisResult.id)))
    
    # 투자 결정별 통계
    decision_stats = await db.execute(
        select(
            StockAnalysisResult.decision,
            func.count(StockAnalysisResult.id)
        )
        .group_by(StockAnalysisResult.decision)
    )
    decision_counts = {row[0]: row[1] for row in decision_stats.fetchall()}
    
    # 상품 타입별 통계
    instrument_stats = await db.execute(
        select(
            StockAnalysisResult.instrument_type,
            func.count(StockAnalysisResult.id)
        )
        .group_by(StockAnalysisResult.instrument_type)
    )
    instrument_counts = {row[0]: row[1] for row in instrument_stats.fetchall()}
    
    # 모델별 통계
    model_stats = await db.execute(
        select(
            StockAnalysisResult.model_name,
            func.count(StockAnalysisResult.id)
        )
        .group_by(StockAnalysisResult.model_name)
    )
    model_counts = {row[0]: row[1] for row in model_stats.fetchall()}
    
    # 평균 신뢰도
    avg_confidence = await db.scalar(
        select(func.avg(StockAnalysisResult.confidence))
        .where(StockAnalysisResult.confidence.isnot(None))
    )
    
    return {
        "total_count": total_count,
        "decision_counts": decision_counts,
        "instrument_counts": instrument_counts,
        "model_counts": model_counts,
        "average_confidence": round(avg_confidence, 2) if avg_confidence else 0
    }
