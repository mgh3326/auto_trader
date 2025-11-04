"""
Upbit 자동 매매 웹 인터페이스 라우터
- 보유 코인 조회
- AI 분석 실행
- 자동 매수 주문
- 자동 매도 주문
"""

import asyncio
from decimal import Decimal, InvalidOperation
from typing import List, Optional
from fastapi import APIRouter, Depends, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.config import settings
from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from app.services.stock_info_service import (
    process_buy_orders_with_analysis,
    StockAnalysisService
)
from data.coins_info import upbit_pairs

router = APIRouter(prefix="/upbit-trading", tags=["Upbit Trading"])

# 템플릿 설정
templates = Jinja2Templates(directory="app/templates")


def _to_decimal(value) -> Decimal:
    """입력 값을 Decimal로 안전하게 변환"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _format_coin_amount(value: Decimal) -> str:
    """코인 수량 표시용 문자열 반환"""
    normalized = value.normalize()
    formatted = format(normalized, 'f')
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted or '0'


@router.get("/", response_class=HTMLResponse)
async def upbit_trading_dashboard(request: Request):
    """Upbit 자동 매매 대시보드 페이지"""
    return templates.TemplateResponse("upbit_trading_dashboard.html", {"request": request})


@router.get("/api/my-coins")
async def get_my_coins(
    db: AsyncSession = Depends(get_db),
):
    """보유 코인 조회 API"""
    analyzer: UpbitAnalyzer | None = None
    tradable_coins: list[dict] = []
    try:
        await upbit_pairs.prime_upbit_constants()
        my_coins = await upbit.fetch_my_coins()

        analyzer = UpbitAnalyzer()
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer.is_tradable(coin)
            and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]

        if tradable_coins:
            market_codes = [f"KRW-{coin['currency']}" for coin in tradable_coins]
            current_prices = await upbit.fetch_multiple_current_prices(market_codes)
            analysis_service = StockAnalysisService(db)
            latest_analysis_map = await analysis_service.get_latest_analysis_results_for_coins(
                list(dict.fromkeys(market_codes))
            )

            for coin in tradable_coins:
                currency = coin['currency']
                market = f"KRW-{currency}"
                balance_raw = coin.get('balance', '0')
                locked_raw = coin.get('locked', '0')
                balance_decimal = _to_decimal(balance_raw)
                locked_decimal = _to_decimal(locked_raw)
                balance = float(balance_decimal)
                locked = float(locked_decimal)
                avg_buy_price = float(coin.get('avg_buy_price', 0))

                korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency, currency)
                coin['korean_name'] = korean_name
                coin['balance_raw'] = str(balance_raw)
                coin['locked_raw'] = str(locked_raw)
                coin['balance'] = balance
                coin['locked'] = locked
                coin['balance_display'] = _format_coin_amount(balance_decimal)
                coin['locked_display'] = _format_coin_amount(locked_decimal)

                if market in current_prices:
                    current_price = current_prices[market]
                    coin['current_price'] = current_price

                    if avg_buy_price > 0:
                        profit_rate = (current_price - avg_buy_price) / avg_buy_price
                        coin['profit_rate'] = profit_rate

                        evaluation = (balance + locked) * current_price
                        coin['evaluation'] = evaluation

                        profit_loss = evaluation - ((balance + locked) * avg_buy_price)
                        coin['profit_loss'] = profit_loss
                    else:
                        coin['profit_rate'] = 0
                        coin['evaluation'] = 0
                        coin['profit_loss'] = 0
                else:
                    coin['current_price'] = 0
                    coin['profit_rate'] = 0
                    coin['evaluation'] = 0
                    coin['profit_loss'] = 0

                analysis = latest_analysis_map.get(market)
                coin['market'] = market
                if analysis:
                    coin['analysis_id'] = analysis.id
                    coin['stock_info_id'] = analysis.stock_info_id
                    coin['last_analysis_at'] = (
                        analysis.created_at.isoformat() if analysis.created_at else None
                    )
                    coin['last_analysis_decision'] = analysis.decision
                    coin['analysis_confidence'] = (
                        float(analysis.confidence) if analysis.confidence is not None else None
                    )
                else:
                    coin['analysis_id'] = None
                    coin['stock_info_id'] = None
                    coin['last_analysis_at'] = None
                    coin['last_analysis_decision'] = None
                    coin['analysis_confidence'] = None

        krw_balance = 0
        krw_locked = 0
        for coin in my_coins:
            if coin.get("currency") == "KRW":
                balance_decimal = _to_decimal(coin.get("balance", "0"))
                locked_decimal = _to_decimal(coin.get("locked", "0"))
                krw_balance = int(balance_decimal)
                krw_locked = int(locked_decimal)
                break

        return {
            "success": True,
            "krw_balance": krw_balance,
            "krw_locked": krw_locked,
            "krw_total": krw_balance + krw_locked,
            "total_coins": len(my_coins),
            "tradable_coins_count": len(tradable_coins),
            "coins": tradable_coins
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if analyzer is not None:
            await analyzer.close()


@router.post("/api/analyze-coins")
async def analyze_my_coins():
    """보유 코인 AI 분석 실행 (Celery)"""
    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        # Celery 작업 큐에 등록
        from app.core.celery_app import celery_app
        async_result = celery_app.send_task("analyze.run_for_my_coins")

        return {
            "success": True,
            "message": "코인 분석이 시작되었습니다.",
            "task_id": async_result.id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/analyze-task/{task_id}")
async def get_analyze_task_status(task_id: str):
    """Celery 분석 작업 상태 조회 API"""
    from app.core.celery_app import celery_app

    result = celery_app.AsyncResult(task_id)

    response = {
        "task_id": task_id,
        "state": result.state,
        "ready": result.ready(),
    }

    if result.state == 'PROGRESS':
        # 진행 중 - meta 정보 반환
        response["progress"] = result.info
    elif result.successful():
        # 완료 - 결과 반환
        try:
            response["result"] = result.get(timeout=0)
        except Exception:
            response["result"] = None
    elif result.failed():
        # 실패 - 에러 반환
        response["error"] = str(result.result)

    return response


@router.post("/api/buy-orders")
async def execute_buy_orders():
    """보유 코인 자동 매수 주문 실행 (Celery)"""
    from app.core.celery_app import celery_app

    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        async_result = celery_app.send_task("upbit.execute_buy_orders")
        return {
            "success": True,
            "message": "매수 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 코인 자동 매도 주문 실행 (Celery)"""
    from app.core.celery_app import celery_app

    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        async_result = celery_app.send_task("upbit.execute_sell_orders")
        return {
            "success": True,
            "message": "매도 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/automation/per-coin")
async def execute_per_coin_automation():
    """보유 코인별 자동 실행 (분석 → 분할 매수 → 분할 매도)"""
    from app.core.celery_app import celery_app

    try:
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        async_result = celery_app.send_task("upbit.run_per_coin_automation")
        return {
            "success": True,
            "message": "코인별 자동 실행이 시작되었습니다.",
            "task_id": async_result.id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/coin/{currency}/buy-orders")
async def execute_coin_buy_orders(currency: str):
    """특정 코인에 대한 분할 매수 주문 실행 (Celery)"""
    from app.core.celery_app import celery_app

    currency_code = currency.upper()

    try:
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        async_result = celery_app.send_task("upbit.execute_buy_order_for_coin", args=[currency_code])
        return {
            "success": True,
            "currency": currency_code,
            "message": f"{currency_code} 분할 매수 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/coin/{currency}/analysis")
async def analyze_coin(currency: str):
    """특정 코인에 대한 AI 분석 실행 (Celery)"""
    from app.core.celery_app import celery_app

    currency_code = currency.upper()

    try:
        await upbit_pairs.prime_upbit_constants()

        if currency_code not in upbit_pairs.KRW_TRADABLE_COINS:
            raise HTTPException(status_code=400, detail=f"{currency_code}는 KRW 마켓 거래 대상이 아닙니다.")

        async_result = celery_app.send_task("analyze.run_for_coin", args=[currency_code])
        return {
            "success": True,
            "currency": currency_code,
            "message": f"{currency_code} 분석이 시작되었습니다.",
            "task_id": async_result.id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/coin/{currency}/sell-orders")
async def execute_coin_sell_orders(currency: str):
    """특정 코인에 대한 분할 매도 주문 실행 (Celery)"""
    from app.core.celery_app import celery_app

    currency_code = currency.upper()

    try:
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        async_result = celery_app.send_task("upbit.execute_sell_order_for_coin", args=[currency_code])
        return {
            "success": True,
            "currency": currency_code,
            "message": f"{currency_code} 분할 매도 주문이 시작되었습니다.",
            "task_id": async_result.id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/open-orders")
async def get_open_orders():
    """체결 대기 중인 모든 주문 조회"""
    try:
        await upbit_pairs.prime_upbit_constants()

        # 현재 보유 자산 정보는 보조 메타데이터로 활용
        my_coins = await upbit.fetch_my_coins()
        holdings = {
            coin.get("currency"): coin
            for coin in my_coins
            if coin.get("currency")
        }

        open_orders = await upbit.fetch_open_orders()
        enriched_orders = []

        for order in open_orders:
            market = order.get("market", "")
            currency = ""
            if "-" in market:
                _, currency = market.split("-", 1)
            elif market:
                currency = market

            korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency, currency) if currency else ""
            holding = holdings.get(currency)

            enriched_order = dict(order)
            enriched_order["currency"] = currency
            enriched_order["korean_name"] = korean_name

            if holding:
                enriched_order["holding_balance"] = holding.get("balance")
                enriched_order["holding_locked"] = holding.get("locked")
                enriched_order["holding_avg_buy_price"] = holding.get("avg_buy_price")
            else:
                enriched_order["holding_balance"] = None
                enriched_order["holding_locked"] = None
                enriched_order["holding_avg_buy_price"] = None

            enriched_orders.append(enriched_order)

        return {
            "success": True,
            "orders": enriched_orders,
            "total_count": len(enriched_orders),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/cancel-orders")
async def cancel_all_orders():
    """모든 미체결 주문 취소"""
    try:
        await upbit_pairs.prime_upbit_constants()

        open_orders = await upbit.fetch_open_orders()

        if not open_orders:
            return {
                "success": True,
                "results": [],
                "total_count": 0,
                "message": "미체결 주문이 없습니다.",
            }

        grouped_orders: dict[str, list[dict]] = {}
        for order in open_orders:
            market = order.get("market") or ""
            grouped_orders.setdefault(market, []).append(order)

        cancel_results = []

        for market, orders in grouped_orders.items():
            currency = ""
            if "-" in market:
                _, currency = market.split("-", 1)
            elif market:
                currency = market

            try:
                order_uuids = [order["uuid"] for order in orders if order.get("uuid")]
                if not order_uuids:
                    cancel_results.append({
                        "currency": currency,
                        "market": market,
                        "success": False,
                        "cancelled_count": 0,
                        "total_count": len(orders),
                        "error": "취소할 주문 UUID가 없습니다.",
                    })
                    continue

                results = await upbit.cancel_orders(order_uuids)
                success_count = sum(1 for item in results if "error" not in item)
                cancel_results.append({
                    "currency": currency,
                    "market": market,
                    "success": success_count == len(order_uuids),
                    "cancelled_count": success_count,
                    "total_count": len(order_uuids),
                })
            except Exception as exc:
                cancel_results.append({
                    "currency": currency,
                    "market": market,
                    "success": False,
                    "cancelled_count": 0,
                    "total_count": len(orders),
                    "error": str(exc),
                })

        return {
            "success": True,
            "results": cancel_results,
            "total_count": sum(len(orders) for orders in grouped_orders.values()),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
