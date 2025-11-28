"""
KIS 국내주식 자동 매매 웹 인터페이스 라우터
- 보유 주식 조회
- AI 분석 실행
- 자동 매수/매도 주문 (Placeholder)
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.config import settings
from app.core.templates import templates
from app.services.kis import KISClient
from app.analysis.service_analyzers import KISAnalyzer
from app.services.stock_info_service import StockAnalysisService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kis-domestic-trading", tags=["KIS Domestic Trading"])


@router.get("/", response_class=HTMLResponse)
async def kis_domestic_trading_dashboard(request: Request):
    """KIS 국내주식 자동 매매 대시보드 페이지"""
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        "kis_domestic_trading_dashboard.html",
        {
            "request": request,
            "user": user,
        }
    )


@router.get("/api/my-stocks")
async def get_my_domestic_stocks(
    db: AsyncSession = Depends(get_db),
):
    """보유 국내 주식 조회 API"""
    try:
        kis = KISClient()
        # TODO: Add caching or database storage for analysis results if needed
        # For now, we just fetch current balance and basic info
        
        my_stocks = await kis.fetch_my_stocks()
        
        # 통합 증거금 조회 (예수금 확인용)
        margin = await kis.inquire_integrated_margin()
        krw_balance = margin.get("dnca_tot_amt", 0)
        
        # Enrich with analysis data if available (mock for now or fetch from DB if implemented)
        # 2. DB에서 최신 분석 결과 조회
        stock_service = StockAnalysisService(db)
        
        # 종목 코드 리스트 추출
        codes = [stock.get("pdno") for stock in my_stocks]
        analysis_map = await stock_service.get_latest_analysis_results_for_coins(codes)
        
        processed_stocks = []
        for stock in my_stocks:
            code = stock.get("pdno")
            analysis = analysis_map.get(code)
            
            processed_stocks.append({
                "code": code,
                "name": stock.get("prdt_name"),
                "quantity": int(stock.get("hldg_qty", 0)),
                "current_price": float(stock.get("prpr", 0)),
                "avg_price": float(stock.get("pchs_avg_pric", 0)),
                "profit_rate": float(stock.get("evlu_pfls_rt", 0)) / 100.0,
                "evaluation": float(stock.get("evlu_amt", 0)),
                "profit_loss": float(stock.get("evlu_pfls_amt", 0)),
                "analysis_id": analysis.id if analysis else None,
                "last_analysis_at": analysis.created_at.isoformat() if analysis and analysis.created_at else None,
                "last_analysis_decision": analysis.decision if analysis else None,
                "analysis_confidence": analysis.confidence if analysis else None,
            })

        return {
            "success": True,
            "krw_balance": krw_balance,
            "total_stocks": len(processed_stocks),
            "stocks": processed_stocks
        }

    except Exception as e:
        logger.error(f"Error in get_my_domestic_stocks: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


from app.core.celery_app import celery_app

@router.post("/api/analyze-stocks")
async def analyze_my_domestic_stocks():
    """보유 국내 주식 AI 분석 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.run_analysis_for_my_domestic_stocks")

        return {
            "success": True,
            "message": "국내 주식 분석이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/analyze-task/{task_id}")
async def get_analyze_task_status(task_id: str):
    """Celery 작업 상태 조회 API"""

    result = celery_app.AsyncResult(task_id)

    response = {
        "task_id": task_id,
        "state": result.state,
        "ready": result.ready(),
    }

    if result.state == 'PROGRESS':
        response["progress"] = result.info
    elif result.successful():
        try:
            response["result"] = result.get(timeout=0)
        except Exception:
            response["result"] = None
    elif result.failed():
        response["error"] = str(result.result)

    return response


@router.post("/api/buy-orders")
async def execute_buy_orders():
    """보유 국내 주식 자동 매수 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.execute_domestic_buy_orders")
        return {
            "success": True,
            "message": "매수 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 국내 주식 자동 매도 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.execute_domestic_sell_orders")
        return {
            "success": True,
            "message": "매도 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/automation/per-stock")
async def run_per_stock_automation():
    """보유 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    task = celery_app.send_task("kis.run_per_domestic_stock_automation")
    return {"success": True, "message": "종목별 자동 실행이 시작되었습니다.", "task_id": task.id}


@router.post("/api/analyze-stock/{symbol}")
async def analyze_stock(symbol: str):
    """단일 종목 분석 요청"""
    task = celery_app.send_task("kis.analyze_domestic_stock_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 분석 요청 완료", "task_id": task.id}


@router.post("/api/buy-order/{symbol}")
async def buy_order(symbol: str):
    """단일 종목 매수 요청"""
    task = celery_app.send_task("kis.execute_domestic_buy_order_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 매수 요청 완료", "task_id": task.id}


@router.post("/api/sell-order/{symbol}")
async def sell_order(symbol: str):
    """단일 종목 매도 요청"""
    task = celery_app.send_task("kis.execute_domestic_sell_order_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 매도 요청 완료", "task_id": task.id}
