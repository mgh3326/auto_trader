"""
KIS 국내주식 자동 매매 웹 인터페이스 라우터
- 보유 주식 조회 (KIS + 수동 잔고 통합)
- AI 분석 실행
- 자동 매수/매도 주문 (Placeholder)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.services.kis import KISClient
from app.services.merged_portfolio_service import MergedPortfolioService
from app.tasks.kis import (
    analyze_domestic_stock_task,
    execute_domestic_buy_order_task,
    execute_domestic_buy_orders,
    execute_domestic_sell_order_task,
    execute_domestic_sell_orders,
    run_analysis_for_my_domestic_stocks,
    run_per_domestic_stock_automation,
)

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
        },
    )


@router.get("/api/my-stocks")
async def get_my_domestic_stocks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
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


@router.post("/api/analyze-stocks")
async def analyze_my_domestic_stocks():
    """보유 국내 주식 AI 분석 실행"""
    try:
        result = await run_analysis_for_my_domestic_stocks()
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/buy-orders")
async def execute_buy_orders():
    """보유 국내 주식 자동 매수 주문 실행"""
    try:
        result = await execute_domestic_buy_orders()
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 국내 주식 자동 매도 주문 실행"""
    try:
        result = await execute_domestic_sell_orders()
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/automation/per-stock")
async def run_per_stock_automation():
    """보유 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    result = await run_per_domestic_stock_automation()
    return {"success": True, **result}


@router.post("/api/analyze-stock/{symbol}")
async def analyze_stock(symbol: str):
    """단일 종목 분석 요청"""
    result = await analyze_domestic_stock_task(symbol)
    return {"success": True, **result}


@router.post("/api/buy-order/{symbol}")
async def buy_order(symbol: str):
    """단일 종목 매수 요청"""
    result = await execute_domestic_buy_order_task(symbol)
    return {"success": True, **result}


@router.post("/api/sell-order/{symbol}")
async def sell_order(symbol: str):
    """단일 종목 매도 요청"""
    result = await execute_domestic_sell_order_task(symbol)
    return {"success": True, **result}
