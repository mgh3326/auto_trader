import json
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.analysis import StockAnalysisResult, StockInfo
from app.monitoring.telemetry import get_meter, get_tracer

router = APIRouter(prefix="/analysis-json", tags=["JSON Analysis Results"])

# Initialize telemetry
_meter = get_meter(__name__)
_tracer = get_tracer(__name__)

# Create endpoint-specific metrics
endpoint_counter = _meter.create_counter(
    name="analysis_api.requests",
    description="Number of analysis API requests",
    unit="1",
)

endpoint_duration = _meter.create_histogram(
    name="analysis_api.duration",
    description="Analysis API request duration",
    unit="ms",
)

db_query_duration = _meter.create_histogram(
    name="analysis_api.db_query.duration",
    description="Database query duration for analysis API",
    unit="ms",
)


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
async def analysis_json_dashboard(request: Request):
    """JSON 분석 결과 대시보드 페이지"""
    return templates.TemplateResponse(
        "analysis_json_dashboard.html", {"request": request}
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def analysis_json_dashboard_page(request: Request):
    """JSON 분석 결과 대시보드 HTML 페이지"""
    return templates.TemplateResponse(
        "analysis_json_dashboard.html", {"request": request}
    )


@router.get("/api/results")
async def get_analysis_results(
    db: AsyncSession = Depends(get_db),
    instrument_type: str | None = Query(None, description="상품 타입 필터"),
    symbol: str | None = Query(None, description="종목 코드 필터"),
    model_name: str | None = Query(None, description="모델명 필터"),
    decision: str | None = Query(None, description="투자 결정 필터"),
    page: int = Query(1, ge=1, description="페이지 번호"),
    page_size: int = Query(20, ge=1, le=100, description="페이지 크기"),
):
    """JSON 분석 결과를 조회하는 API"""
    start_time = time.time()

    with _tracer.start_as_current_span("get_analysis_results") as span:
        span.set_attribute("page", page)
        span.set_attribute("page_size", page_size)
        if instrument_type:
            span.set_attribute("filter.instrument_type", instrument_type)
        if symbol:
            span.set_attribute("filter.symbol", symbol)
        if model_name:
            span.set_attribute("filter.model_name", model_name)
        if decision:
            span.set_attribute("filter.decision", decision)

        try:
            # 기본 쿼리 생성 (StockInfo와 JOIN)
            query = (
                select(StockAnalysisResult, StockInfo)
                .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
                .where(StockInfo.is_active == True)
            )

            # 필터 적용
            if instrument_type and instrument_type != "전체":
                query = query.where(StockInfo.instrument_type == instrument_type)

            if symbol and symbol != "전체":
                query = query.where(StockInfo.symbol.ilike(f"%{symbol}%"))

            if model_name and model_name != "전체":
                query = query.where(StockAnalysisResult.model_name == model_name)

            if decision and decision != "전체":
                query = query.where(StockAnalysisResult.decision == decision)

            # 전체 개수 조회 with timing
            count_start = time.time()
            count_query = (
                select(func.count(StockAnalysisResult.id))
                .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
                .where(StockInfo.is_active == True)
            )

            if instrument_type and instrument_type != "전체":
                count_query = count_query.where(
                    StockInfo.instrument_type == instrument_type
                )
            if symbol and symbol != "전체":
                count_query = count_query.where(StockInfo.symbol.ilike(f"%{symbol}%"))
            if model_name and model_name != "전체":
                count_query = count_query.where(
                    StockAnalysisResult.model_name == model_name
                )
            if decision and decision != "전체":
                count_query = count_query.where(
                    StockAnalysisResult.decision == decision
                )

            total_count = await db.scalar(count_query)
            count_duration = (time.time() - count_start) * 1000
            db_query_duration.record(count_duration, {"operation": "count"})
            span.set_attribute("total_count", total_count or 0)

            # 페이지네이션 적용
            query = query.order_by(StockAnalysisResult.created_at.desc())
            query = query.offset((page - 1) * page_size).limit(page_size)

            # 결과 조회 with timing
            query_start = time.time()
            result = await db.execute(query)
            results = result.fetchall()
            query_duration = (time.time() - query_start) * 1000
            db_query_duration.record(query_duration, {"operation": "select"})
            span.set_attribute("results_count", len(results))

            # 응답 데이터 구성
            analysis_results = []
            for analysis_result, stock_info in results:
                analysis_results.append(
                    {
                        "id": analysis_result.id,
                        "symbol": stock_info.symbol,
                        "name": stock_info.name,
                        "instrument_type": stock_info.instrument_type,
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
                        "reasons": _normalize_reasons(analysis_result.reasons),
                        "detailed_text": analysis_result.detailed_text,
                        "created_at": analysis_result.created_at.isoformat()
                        if analysis_result.created_at
                        else None,
                    }
                )

            # Record success metrics
            total_duration = (time.time() - start_time) * 1000

            endpoint_counter.add(
                1,
                {
                    "endpoint": "/api/results",
                    "status": "success",
                    "has_filters": bool(
                        instrument_type or symbol or model_name or decision
                    ),
                },
            )
            endpoint_duration.record(
                total_duration,
                {
                    "endpoint": "/api/results",
                },
            )

            return {
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": (total_count + page_size - 1) // page_size,
                "results": analysis_results,
            }

        except Exception as e:
            # Record error metrics
            total_duration = (time.time() - start_time) * 1000
            span.record_exception(e)

            endpoint_counter.add(
                1,
                {
                    "endpoint": "/api/results",
                    "status": "error",
                    "error_type": type(e).__name__,
                },
            )
            endpoint_duration.record(
                total_duration,
                {
                    "endpoint": "/api/results",
                    "status": "error",
                },
            )
            raise


def _build_analysis_response(
    analysis_result: StockAnalysisResult, stock_info: StockInfo
) -> dict:
    """분석 결과를 응답 형식으로 변환하는 헬퍼 함수"""
    reasons = _normalize_reasons(analysis_result.reasons)

    return {
        "id": analysis_result.id,
        "symbol": stock_info.symbol,
        "name": stock_info.name,
        "instrument_type": stock_info.instrument_type,
        "model_name": analysis_result.model_name,
        "decision": analysis_result.decision,
        "confidence": analysis_result.confidence,
        "appropriate_buy_range": {
            "min": analysis_result.appropriate_buy_min,
            "max": analysis_result.appropriate_buy_max,
        },
        "appropriate_sell_range": {
            "min": analysis_result.appropriate_sell_min,
            "max": analysis_result.appropriate_sell_max,
        },
        "buy_hope_range": {
            "min": analysis_result.buy_hope_min,
            "max": analysis_result.buy_hope_max,
        },
        "sell_target_range": {
            "min": analysis_result.sell_target_min,
            "max": analysis_result.sell_target_max,
        },
        "reasons": reasons,
        "detailed_text": analysis_result.detailed_text,
        "created_at": analysis_result.created_at.isoformat()
        if analysis_result.created_at
        else None,
        "prompt": analysis_result.prompt,
    }


@router.get("/api/detail/{result_id}")
async def get_analysis_detail(result_id: int, db: AsyncSession = Depends(get_db)):
    """특정 분석 결과의 상세 정보를 조회하는 API"""

    query = (
        select(StockAnalysisResult, StockInfo)
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockAnalysisResult.id == result_id)
    )

    result = await db.execute(query)
    row = result.first()

    if not row:
        return {"error": "분석 결과를 찾을 수 없습니다."}

    analysis_result, stock_info = row
    return _build_analysis_response(analysis_result, stock_info)


@router.get("/api/detail/by-symbol/{symbol}")
async def get_latest_analysis_by_symbol(
    symbol: str, db: AsyncSession = Depends(get_db)
):
    """특정 종목의 최신 분석 결과를 조회하는 API"""

    query = (
        select(StockAnalysisResult, StockInfo)
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.symbol == symbol)
        .order_by(StockAnalysisResult.created_at.desc())
        .limit(1)
    )

    result = await db.execute(query)
    row = result.first()

    if not row:
        return {"error": "분석 결과를 찾을 수 없습니다."}

    analysis_result, stock_info = row
    return _build_analysis_response(analysis_result, stock_info)


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

    # 모델명 옵션 (StockAnalysisResult에서 조회)
    model_names = await db.execute(
        select(StockAnalysisResult.model_name)
        .distinct()
        .where(StockAnalysisResult.model_name.isnot(None))
    )
    model_name_options = [row[0] for row in model_names.fetchall()]

    return {
        "instrument_types": instrument_type_options,
        "symbols": symbol_options,
        "model_names": model_name_options,
    }


@router.get("/api/statistics")
async def get_analysis_statistics(db: AsyncSession = Depends(get_db)):
    """분석 통계를 조회하는 API"""

    # 전체 분석 개수 (활성화된 주식만)
    total_count = await db.scalar(
        select(func.count(StockAnalysisResult.id))
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
    )

    # 투자 결정별 통계
    decision_stats = await db.execute(
        select(StockAnalysisResult.decision, func.count(StockAnalysisResult.id))
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
        .group_by(StockAnalysisResult.decision)
    )
    decision_counts = {row[0]: row[1] for row in decision_stats.fetchall()}

    # 상품 타입별 통계 (StockInfo에서 조회)
    instrument_stats = await db.execute(
        select(StockInfo.instrument_type, func.count(StockAnalysisResult.id))
        .join(StockAnalysisResult, StockInfo.id == StockAnalysisResult.stock_info_id)
        .where(StockInfo.is_active == True)
        .group_by(StockInfo.instrument_type)
    )
    instrument_counts = {row[0]: row[1] for row in instrument_stats.fetchall()}

    # 모델별 통계
    model_stats = await db.execute(
        select(StockAnalysisResult.model_name, func.count(StockAnalysisResult.id))
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
        .group_by(StockAnalysisResult.model_name)
    )
    model_counts = {row[0]: row[1] for row in model_stats.fetchall()}

    # 평균 신뢰도
    avg_confidence = await db.scalar(
        select(func.avg(StockAnalysisResult.confidence))
        .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
        .where(StockInfo.is_active == True)
        .where(StockAnalysisResult.confidence.isnot(None))
    )

    return {
        "total_count": total_count,
        "decision_counts": decision_counts,
        "instrument_counts": instrument_counts,
        "model_counts": model_counts,
        "average_confidence": round(avg_confidence, 2) if avg_confidence else 0,
    }
