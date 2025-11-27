"""
KIS 해외주식 자동 매매 웹 인터페이스 라우터
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
from app.services.stock_info_service import StockAnalysisService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kis-overseas-trading", tags=["KIS Overseas Trading"])


@router.get("/", response_class=HTMLResponse)
async def kis_overseas_trading_dashboard(request: Request):
    """KIS 해외주식 자동 매매 대시보드 페이지"""
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        "kis_overseas_trading_dashboard.html",
        {
            "request": request,
            "user": user,
        }
    )


@router.get("/api/my-stocks")
async def get_my_overseas_stocks(
    db: AsyncSession = Depends(get_db),
):
    """보유 해외 주식 조회 API"""
    try:
        kis = KISClient()
        
        my_stocks = await kis.fetch_my_overseas_stocks()
        
        # 통합 증거금 조회 (달러 예수금 확인용)
        margin = await kis.inquire_integrated_margin()
        usd_balance = margin.get("usd_balance", 0)
        
        # 2. DB에서 최신 분석 결과 조회
        stock_service = StockAnalysisService(db)
        
        # 종목 코드 리스트 추출
        codes = [stock.get("ovrs_pdno") for stock in my_stocks]
        analysis_map = await stock_service.get_latest_analysis_results_for_coins(codes)
        
        processed_stocks = []
        for stock in my_stocks:
            code = stock.get("ovrs_pdno")
            analysis = analysis_map.get(code)
            
            processed_stocks.append({
                "code": code,
                "name": stock.get("ovrs_item_name"),
                "quantity": float(stock.get("ovrs_cblc_qty", 0)),
                "current_price": float(stock.get("now_pric2", 0)),
                "avg_price": float(stock.get("pchs_avg_pric", 0)),
                "profit_rate": float(stock.get("evlu_pfls_rt", 0)) / 100.0,
                "evaluation": float(stock.get("ovrs_stck_evlu_amt", 0)),
                "profit_loss": float(stock.get("frcr_evlu_pfls_amt", 0)),
                "analysis_id": analysis.id if analysis else None,
                "last_analysis_at": analysis.created_at.isoformat() if analysis and analysis.created_at else None,
                "last_analysis_decision": analysis.decision if analysis else None,
                "analysis_confidence": analysis.confidence if analysis else None,
            })

        return {
            "success": True,
            "usd_balance": usd_balance,
            "total_stocks": len(processed_stocks),
            "stocks": processed_stocks
        }

    except Exception as e:
        logger.error(f"Error in get_my_overseas_stocks: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


from app.core.celery_app import celery_app

@router.post("/api/analyze-stocks")
async def analyze_my_overseas_stocks():
    """보유 해외 주식 AI 분석 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.run_analysis_for_my_overseas_stocks")

        return {
            "success": True,
            "message": "해외 주식 분석이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    """보유 해외 주식 자동 매수 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.execute_overseas_buy_orders")
        return {
            "success": True,
            "message": "매수 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 해외 주식 자동 매도 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task("kis.execute_overseas_sell_orders")
        return {
            "success": True,
            "message": "매도 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/automation/per-stock")
async def run_per_stock_automation(request: Request):
    """보유 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    task = celery_app.send_task("kis.run_per_overseas_stock_automation")
    return {"success": True, "message": "종목별 자동 실행이 시작되었습니다.", "task_id": task.id}


@router.post("/api/analyze-stock/{symbol}")
async def analyze_stock(symbol: str, request: Request):
    """단일 종목 분석 요청"""
    task = celery_app.send_task("kis.analyze_overseas_stock_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 분석 요청 완료", "task_id": task.id}


@router.post("/api/buy-order/{symbol}")
async def buy_order(symbol: str, request: Request):
    """단일 종목 매수 요청"""
    task = celery_app.send_task("kis.execute_overseas_buy_order_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 매수 요청 완료", "task_id": task.id}


@router.post("/api/sell-order/{symbol}")
async def sell_order(symbol: str, request: Request):
    """단일 종목 매도 요청"""
    task = celery_app.send_task("kis.execute_overseas_sell_order_task", args=[symbol])
    return {"success": True, "message": f"{symbol} 매도 요청 완료", "task_id": task.id}
