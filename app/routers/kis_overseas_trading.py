"""
KIS 해외주식 자동 매매 웹 인터페이스 라우터
- 보유 주식 조회 (KIS + 수동 잔고 통합)
- AI 분석 실행
- 자동 매수/매도 주문 (Placeholder)
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.taskiq_result import build_task_status_response
from app.core.templates import templates
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.services.kis import KISClient
from app.services.merged_portfolio_service import MergedPortfolioService
from app.tasks.kis import (
    analyze_overseas_stock_task,
    execute_overseas_buy_order_task,
    execute_overseas_buy_orders,
    execute_overseas_sell_order_task,
    execute_overseas_sell_orders,
    run_analysis_for_my_overseas_stocks,
    run_per_overseas_stock_automation,
)

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
        },
    )


def _is_us_nation_name(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().casefold()
    return normalized in {
        "미국",
        "us",
        "usa",
        "united states",
        "united states of america",
    }


def _select_usd_row_for_balance(rows: list[dict]) -> dict | None:
    if not rows:
        return None

    usd_rows = [
        row for row in rows if str(row.get("crcy_cd", "")).strip().upper() == "USD"
    ]
    if not usd_rows:
        return None

    us_row = next(
        (row for row in usd_rows if _is_us_nation_name(row.get("natn_name"))), None
    )
    if us_row:
        return us_row

    def _orderable_amount(row: dict) -> float:
        try:
            return float(row.get("frcr_gnrl_ord_psbl_amt") or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(usd_rows, key=_orderable_amount)


@router.get("/api/my-stocks")
async def get_my_overseas_stocks(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
):
    try:
        kis = KISClient()

        margin_data = await kis.inquire_overseas_margin()
        usd_row = _select_usd_row_for_balance(margin_data)
        if usd_row is None:
            raise RuntimeError("USD margin data not found in overseas margin response")
        usd_balance = float(
            usd_row.get("frcr_dncl_amt1") or usd_row.get("frcr_dncl_amt_2") or 0
        )

        # MergedPortfolioService를 사용하여 KIS + 수동 잔고 통합
        service = MergedPortfolioService(db)
        merged_holdings = await service.get_merged_portfolio_overseas(
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
            "usd_balance": usd_balance,
            "total_stocks": len(processed_stocks),
            "stocks": processed_stocks,
        }

    except Exception as e:
        logger.error(f"Error in get_my_overseas_stocks: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/analyze-stocks")
async def analyze_my_overseas_stocks():
    """보유 해외 주식 AI 분석 실행 (TaskIQ)"""
    try:
        task = await run_analysis_for_my_overseas_stocks.kiq()

        return {
            "success": True,
            "message": "해외 주식 분석이 시작되었습니다.",
            "task_id": task.task_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/analyze-task/{task_id}")
async def get_analyze_task_status(task_id: str):
    """TaskIQ 작업 상태 조회 API"""

    return await build_task_status_response(task_id)


@router.post("/api/buy-orders")
async def execute_buy_orders():
    """보유 해외 주식 자동 매수 주문 실행 (TaskIQ)"""
    try:
        task = await execute_overseas_buy_orders.kiq()
        return {
            "success": True,
            "message": "매수 주문이 시작되었습니다.",
            "task_id": task.task_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 해외 주식 자동 매도 주문 실행 (TaskIQ)"""
    try:
        task = await execute_overseas_sell_orders.kiq()
        return {
            "success": True,
            "message": "매도 주문이 시작되었습니다.",
            "task_id": task.task_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/automation/per-stock")
async def run_per_stock_automation():
    """보유 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    task = await run_per_overseas_stock_automation.kiq()
    return {
        "success": True,
        "message": "종목별 자동 실행이 시작되었습니다.",
        "task_id": task.task_id,
    }


@router.post("/api/analyze-stock/{symbol}")
async def analyze_stock(symbol: str):
    """단일 종목 분석 요청"""
    task = await analyze_overseas_stock_task.kiq(symbol)
    return {
        "success": True,
        "message": f"{symbol} 분석 요청 완료",
        "task_id": task.task_id,
    }


@router.post("/api/buy-order/{symbol}")
async def buy_order(symbol: str):
    """단일 종목 매수 요청"""
    task = await execute_overseas_buy_order_task.kiq(symbol)
    return {
        "success": True,
        "message": f"{symbol} 매수 요청 완료",
        "task_id": task.task_id,
    }


@router.post("/api/sell-order/{symbol}")
async def sell_order(symbol: str):
    """단일 종목 매도 요청"""
    task = await execute_overseas_sell_order_task.kiq(symbol)
    return {
        "success": True,
        "message": f"{symbol} 매도 요청 완료",
        "task_id": task.task_id,
    }
