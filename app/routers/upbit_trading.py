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
    try:
        await upbit_pairs.prime_upbit_constants()
        my_coins = await upbit.fetch_my_coins()

        analyzer = UpbitAnalyzer()
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"
            and analyzer._is_tradable(coin)
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
    """체결 대기 중인 주문 조회"""
    try:
        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 보유 코인 조회
        my_coins = await upbit.fetch_my_coins()

        # 거래 가능한 코인 필터링
        analyzer = UpbitAnalyzer()
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"
               and analyzer._is_tradable(coin)
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]
        await analyzer.close()

        # 각 코인의 미체결 주문 조회
        all_orders = []
        for coin in tradable_coins:
            currency = coin['currency']
            market = f"KRW-{currency}"

            try:
                open_orders = await upbit.fetch_open_orders(market)

                for order in open_orders:
                    order['currency'] = currency
                    order['korean_name'] = upbit_pairs.COIN_TO_NAME_KR.get(currency, currency)
                    all_orders.append(order)
            except Exception as e:
                print(f"⚠️ {market} 미체결 주문 조회 실패: {e}")
                continue

        return {
            "success": True,
            "orders": all_orders,
            "total_count": len(all_orders)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@router.delete("/api/cancel-orders")
async def cancel_all_orders():
    """모든 미체결 주문 취소"""
    try:
        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 보유 코인 조회
        my_coins = await upbit.fetch_my_coins()

        # 거래 가능한 코인 필터링
        analyzer = UpbitAnalyzer()
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"
               and analyzer._is_tradable(coin)
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]
        await analyzer.close()

        # 각 코인의 미체결 주문 취소
        cancel_results = []
        for coin in tradable_coins:
            currency = coin['currency']
            market = f"KRW-{currency}"

            try:
                open_orders = await upbit.fetch_open_orders(market)

                if open_orders:
                    order_uuids = [order['uuid'] for order in open_orders]
                    results = await upbit.cancel_orders(order_uuids)

                    success_count = sum(1 for r in results if 'error' not in r)
                    cancel_results.append({
                        "currency": currency,
                        "market": market,
                        "success": True,
                        "cancelled_count": success_count,
                        "total_count": len(order_uuids)
                    })
            except Exception as e:
                cancel_results.append({
                    "currency": currency,
                    "market": market,
                    "success": False,
                    "error": str(e)
                })

        return {
            "success": True,
            "results": cancel_results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===== Helper Functions =====

async def cancel_existing_buy_orders(market: str):
    """해당 마켓의 기존 매수 주문들을 취소"""
    try:
        open_orders = await upbit.fetch_open_orders(market)
        buy_orders = [order for order in open_orders if order.get('side') == 'bid']

        if buy_orders:
            order_uuids = [order['uuid'] for order in buy_orders]
            await upbit.cancel_orders(order_uuids)
    except Exception as e:
        print(f"⚠️ {market} 매수 주문 취소 실패: {e}")


async def cancel_existing_sell_orders(market: str):
    """해당 마켓의 기존 매도 주문들을 취소"""
    try:
        open_orders = await upbit.fetch_open_orders(market)
        sell_orders = [order for order in open_orders if order.get('side') == 'ask']

        if sell_orders:
            order_uuids = [order['uuid'] for order in sell_orders]
            await upbit.cancel_orders(order_uuids)
    except Exception as e:
        print(f"⚠️ {market} 매도 주문 취소 실패: {e}")


async def get_sell_prices_for_coin(currency: str, avg_buy_price: float, current_price: float) -> List[float]:
    """코인의 매도 가격들을 분석 결과에서 조회"""
    try:
        from app.core.db import AsyncSessionLocal
        from app.services.stock_info_service import StockAnalysisService

        symbol = f"KRW-{currency}"

        async with AsyncSessionLocal() as db:
            service = StockAnalysisService(db)
            analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return []

        # 4개 매도 가격 수집
        sell_prices = []

        if analysis.appropriate_sell_min is not None:
            sell_prices.append(analysis.appropriate_sell_min)
        if analysis.appropriate_sell_max is not None:
            sell_prices.append(analysis.appropriate_sell_max)
        if analysis.sell_target_min is not None:
            sell_prices.append(analysis.sell_target_min)
        if analysis.sell_target_max is not None:
            sell_prices.append(analysis.sell_target_max)

        # 평균 매수가 대비 1% 이상이고 현재가 이상인 가격들만 필터링
        min_sell_price = avg_buy_price * 1.01
        valid_prices = [p for p in sell_prices if p >= min_sell_price and p >= current_price]

        # 가격 오름차순 정렬
        valid_prices.sort()

        return valid_prices

    except Exception as e:
        return []


async def place_multiple_sell_orders(market: str, balance: float, sell_prices: List[float], currency: str) -> dict:
    """여러 가격으로 분할 매도 주문을 실행하고 진행 상황을 로그로 출력합니다."""

    def format_price(value: float) -> str:
        return f"{value:,.0f}"

    if not sell_prices:
        return {'success': False, 'message': '매도 가격이 없습니다', 'orders_placed': 0}

    orders_placed = 0

    if len(sell_prices) == 1:
        target_price = sell_prices[0]
        print(f"💰 전량 매도 주문 시도: {format_price(target_price)}원, 수량 {balance:.8f}")
        result = await place_sell_order_single(market, balance, target_price)
        if result:
            orders_placed = 1
            print("✅ 전량 매도 주문 성공")
            return {'success': True, 'message': '전량 매도 주문 완료', 'orders_placed': orders_placed}
        print("❌ 전량 매도 주문 실패")
        return {'success': False, 'message': '매도 주문 실패', 'orders_placed': 0}

    sell_prices_sorted = sorted(sell_prices)

    split_ratio = 1.0 / len(sell_prices_sorted)
    min_split_volume = balance * split_ratio
    first_sell_price = sell_prices_sorted[0]
    split_amount = (balance * split_ratio) * first_sell_price

    if min_split_volume < 0.00000001 or split_amount < 10000:
        lowest_price = min(sell_prices_sorted)
        print("⚠️ 분할 매도 불가: 최소 분할 수량/금액 미충족, 전량 매도로 전환")
        print(f"💰 전량 매도 주문 시도: {format_price(lowest_price)}원, 수량 {balance:.8f}")
        result = await place_sell_order_single(market, balance, lowest_price)
        if result:
            orders_placed = 1
            print("✅ 전량 매도 주문 성공")
            return {'success': True, 'message': '분할 불가능하여 전량 매도', 'orders_placed': orders_placed}
        print("❌ 전량 매도 주문 실패 (분할 불가)")
        return {'success': False, 'message': '매도 주문 실패 (분할 불가)', 'orders_placed': 0}

    split_prices = sell_prices_sorted[:-1]
    highest_price = sell_prices_sorted[-1]

    print(f"🎯 총 {len(sell_prices_sorted)}개 가격에서 매도 주문 실행:")
    for index, sell_price in enumerate(split_prices, 1):
        try:
            split_volume = balance * split_ratio
            if split_volume < 0.00000001:
                continue

            adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
            volume_str = f"{split_volume:.8f}"
            price_str = f"{adjusted_sell_price}"

            print(f"[{index}/{len(sell_prices_sorted)}] {format_price(adjusted_sell_price)}원 매도 주문, 수량 {split_volume:.8f}")
            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
                print("    ✅ 매도 주문 성공")
            else:
                print("    ❌ 매도 주문 실패")
        except Exception as e:
            print(f"    ❌ 분할 매도 주문 실패: {e}")

    try:
        current_coins = await upbit.fetch_my_coins()
        current_balance = 0.0
        for coin in current_coins:
            if coin.get('currency') == currency:
                current_balance = float(coin['balance'])
                break

        if current_balance >= 0.00000001:
            adjusted_highest_price = upbit.adjust_price_to_upbit_unit(highest_price)
            volume_str = f"{current_balance:.8f}"
            price_str = f"{adjusted_highest_price}"

            print(f"[마지막] 잔량 전량 매도: {format_price(adjusted_highest_price)}원, 수량 {current_balance:.8f}")
            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
                print("    ✅ 잔량 매도 주문 성공")
            else:
                print("    ❌ 잔량 매도 주문 실패")
    except Exception as e:
        print(f"❌ 잔량 매도 주문 실패: {e}")

    if orders_placed > 0:
        return {'success': True, 'message': f'{orders_placed}단계 분할 매도 완료', 'orders_placed': orders_placed}
    return {'success': False, 'message': '모든 매도 주문 실패', 'orders_placed': 0}


async def place_sell_order_single(market: str, balance: float, sell_price: float):
    """단일 매도 주문

    Returns:
        dict: Order result or None if failed
    """
    try:
        volume_str = f"{balance:.8f}"
        adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
        price_str = f"{adjusted_sell_price}"

        result = await upbit.place_sell_order(market, volume_str, price_str)
        return result
    except Exception as e:
        print(f"매도 주문 실패: {e}")
        return None
