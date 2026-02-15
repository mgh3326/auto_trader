"""
KIS 국내주식 자동 매매 웹 인터페이스 라우터
- 보유 주식 조회 (KIS + 수동 잔고 통합)
- AI 분석 실행
- 자동 매수/매도 주문 (Placeholder)

접근 정책: trader, admin만 접근 가능 (viewer 차단)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.trading import User
from app.routers.dependencies import require_trader_user
from app.services.kis import KISClient
from app.services.merged_portfolio_service import MergedPortfolioService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kis-domestic-trading", tags=["KIS Domestic Trading"])


@router.get("/", response_class=HTMLResponse)
async def kis_domestic_trading_dashboard(
    request: Request, current_user: User = Depends(require_trader_user)
):
    """KIS 국내주식 자동 매매 대시보드 페이지 (trader/admin 전용)"""
    return templates.TemplateResponse(
        "kis_domestic_trading_dashboard.html",
        {
            "request": request,
            "user": current_user,
        },
    )


@router.get("/api/my-stocks")
async def get_my_domestic_stocks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_trader_user),
):
    try:
        kis = KISClient()

        balance_data = await kis.inquire_domestic_cash_balance()
        krw_balance = float(
            balance_data.get("dnca_tot_amt")
            or balance_data.get("stck_cash_ord_psbl_amt")
            or 0
        )

        # MergedPortfolioService를 사용하여 KIS + 수동 잔고 통합
        service = MergedPortfolioService(db)
        merged_holdings = await service.get_merged_portfolio_domestic(
            current_user.id, kis
        )

        processed_stocks = []
        for holding in merged_holdings:
            processed_stocks.append(
                {
                    "code": holding.ticker,
                    "name": holding.name,
                    "quantity": holding.total_quantity,
                    "current_price": holding.current_price,
                    "avg_price": holding.combined_avg_price,
                    "profit_rate": holding.profit_rate,
                    "evaluation": holding.evaluation,
                    "profit_loss": holding.profit_loss,
                    "analysis_id": holding.analysis_id,
                    "last_analysis_at": holding.last_analysis_at,
                    "last_analysis_decision": holding.last_analysis_decision,
                    "analysis_confidence": holding.analysis_confidence,
                    # Symbol trade settings
                    "settings_quantity": holding.settings_quantity,
                    "settings_price_levels": holding.settings_price_levels,
                    "settings_note": None,
                    "settings_active": holding.settings_active,
                    # 브로커별 보유 정보 (UI에서 표시 가능)
                    "kis_quantity": holding.kis_quantity,
                    "kis_avg_price": holding.kis_avg_price,
                    "toss_quantity": holding.toss_quantity,
                    "toss_avg_price": holding.toss_avg_price,
                    "holdings": [
                        {
                            "broker": h.broker,
                            "quantity": h.quantity,
                            "avg_price": h.avg_price,
                        }
                        for h in holding.holdings
                    ],
                }
            )

        return {
            "success": True,
            "krw_balance": krw_balance,
            "total_stocks": len(processed_stocks),
            "stocks": processed_stocks,
        }

    except Exception as e:
        logger.error(f"Error in get_my_domestic_stocks: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


from app.core.celery_app import celery_app


@router.post("/api/analyze-stocks")
async def analyze_my_domestic_stocks(current_user: User = Depends(require_trader_user)):
    """보유 국내 주식 AI 분석 실행 (Celery)"""
    try:
        async_result = celery_app.send_task(
            "kis.run_analysis_for_my_domestic_stocks", args=[current_user.id]
        )

        return {
            "success": True,
            "message": "국내 주식 분석이 시작되었습니다.",
            "task_id": async_result.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/analyze-task/{task_id}")
async def get_analyze_task_status(
    task_id: str, current_user: User = Depends(require_trader_user)
):
    """Celery 작업 상태 조회 API"""

    result = celery_app.AsyncResult(task_id)

    response = {
        "task_id": task_id,
        "state": result.state,
        "ready": result.ready(),
    }

    if result.state == "PROGRESS":
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
async def execute_buy_orders(current_user: User = Depends(require_trader_user)):
    """보유 국내 주식 자동 매수 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task(
            "kis.execute_domestic_buy_orders", args=[current_user.id]
        )
        return {
            "success": True,
            "message": "매수 주문이 시작되었습니다.",
            "task_id": async_result.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/sell-orders")
async def execute_sell_orders(current_user: User = Depends(require_trader_user)):
    """보유 국내 주식 자동 매도 주문 실행 (Celery)"""
    try:
        async_result = celery_app.send_task(
            "kis.execute_domestic_sell_orders", args=[current_user.id]
        )
        return {
            "success": True,
            "message": "매도 주문이 시작되었습니다.",
            "task_id": async_result.id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/automation/per-stock")
async def run_per_stock_automation(current_user: User = Depends(require_trader_user)):
    """보유 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    task = celery_app.send_task(
        "kis.run_per_domestic_stock_automation", args=[current_user.id]
    )
    return {
        "success": True,
        "message": "종목별 자동 실행이 시작되었습니다.",
        "task_id": task.id,
    }


@router.post("/api/analyze-stock/{symbol}")
async def analyze_stock(symbol: str, current_user: User = Depends(require_trader_user)):
    """단일 종목 분석 요청"""
    task = celery_app.send_task(
        "kis.analyze_domestic_stock_task", args=[symbol, current_user.id]
    )
    return {"success": True, "message": f"{symbol} 분석 요청 완료", "task_id": task.id}


@router.post("/api/buy-order/{symbol}")
async def buy_order(symbol: str, current_user: User = Depends(require_trader_user)):
    """단일 종목 매수 요청"""
    task = celery_app.send_task(
        "kis.execute_domestic_buy_order_task", args=[symbol, current_user.id]
    )
    return {"success": True, "message": f"{symbol} 매수 요청 완료", "task_id": task.id}


@router.post("/api/sell-order/{symbol}")
async def sell_order(symbol: str, current_user: User = Depends(require_trader_user)):
    """단일 종목 매도 요청"""
    task = celery_app.send_task(
        "kis.execute_domestic_sell_order_task", args=[symbol, current_user.id]
    )
    return {"success": True, "message": f"{symbol} 매도 요청 완료", "task_id": task.id}
