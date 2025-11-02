"""
Upbit 자동 매매 웹 인터페이스 라우터
- 보유 코인 조회
- AI 분석 실행
- 자동 매수 주문
- 자동 매도 주문
"""

import asyncio
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


@router.get("/", response_class=HTMLResponse)
async def upbit_trading_dashboard(request: Request):
    """Upbit 자동 매매 대시보드 페이지"""
    return templates.TemplateResponse("upbit_trading_dashboard.html", {"request": request})


@router.get("/api/my-coins")
async def get_my_coins():
    """보유 코인 조회 API"""
    try:
        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 보유 코인 조회
        my_coins = await upbit.fetch_my_coins()

        # 거래 가능한 코인만 필터링
        analyzer = UpbitAnalyzer()
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"  # 원화 제외
               and analyzer._is_tradable(coin)  # 최소 평가액 이상
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]
        await analyzer.close()

        # 현재가 일괄 조회
        if tradable_coins:
            market_codes = [f"KRW-{coin['currency']}" for coin in tradable_coins]
            current_prices = await upbit.fetch_multiple_current_prices(market_codes)

            # 수익률 계산
            for coin in tradable_coins:
                currency = coin['currency']
                market = f"KRW-{currency}"
                balance = float(coin.get('balance', 0))
                locked = float(coin.get('locked', 0))
                avg_buy_price = float(coin.get('avg_buy_price', 0))

                # 한글 이름 찾기
                korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency, currency)
                coin['korean_name'] = korean_name

                if market in current_prices:
                    current_price = current_prices[market]
                    coin['current_price'] = current_price

                    if avg_buy_price > 0:
                        # 수익률 계산
                        profit_rate = (current_price - avg_buy_price) / avg_buy_price
                        coin['profit_rate'] = profit_rate

                        # 평가금액
                        evaluation = (balance + locked) * current_price
                        coin['evaluation'] = evaluation

                        # 손익금액
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

        # KRW 잔고
        krw_balance = 0
        krw_locked = 0
        for coin in my_coins:
            if coin.get("currency") == "KRW":
                krw_balance = float(coin.get("balance", 0))
                krw_locked = float(coin.get("locked", 0))
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

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@router.post("/api/analyze-coins")
async def analyze_my_coins(background_tasks: BackgroundTasks):
    """보유 코인 AI 분석 실행 (백그라운드)"""
    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        # 백그라운드에서 분석 실행
        background_tasks.add_task(run_coin_analysis)

        return {
            "success": True,
            "message": "코인 분석이 백그라운드에서 시작되었습니다."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def run_coin_analysis():
    """백그라운드 코인 분석 실행"""
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()

    # 분석기 초기화
    analyzer = UpbitAnalyzer()

    try:
        # 보유 코인 조회
        my_coins = await upbit.fetch_my_coins()

        # 거래 가능한 코인 필터링
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"
               and analyzer._is_tradable(coin)
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
        ]

        if not tradable_coins:
            return

        # 한글 이름 목록 생성
        coin_names = []
        for coin in tradable_coins:
            currency = coin.get("currency")
            korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency)
            if korean_name:
                coin_names.append(korean_name)

        # 분석 실행
        if coin_names:
            await analyzer.analyze_coins_json(coin_names)

    finally:
        await analyzer.close()


@router.post("/api/buy-orders")
async def execute_buy_orders():
    """보유 코인 자동 매수 주문 실행"""
    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 분석기 초기화
        analyzer = UpbitAnalyzer()

        try:
            # 보유 코인 조회
            my_coins = await upbit.fetch_my_coins()

            # 거래 가능한 코인 필터링
            tradable_coins = [
                coin for coin in my_coins
                if coin.get("currency") != "KRW"
                   and analyzer._is_tradable(coin)
                   and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
            ]

            if not tradable_coins:
                return {
                    "success": True,
                    "message": "거래 가능한 코인이 없습니다.",
                    "orders": []
                }

            # 현재가 일괄 조회하여 수익률 계산
            market_codes = [f"KRW-{coin['currency']}" for coin in tradable_coins]
            current_prices = await upbit.fetch_multiple_current_prices(market_codes)

            for coin in tradable_coins:
                currency = coin['currency']
                market = f"KRW-{currency}"
                avg_buy_price = float(coin.get('avg_buy_price', 0))

                if avg_buy_price > 0 and market in current_prices:
                    current_price = current_prices[market]
                    profit_rate = (current_price - avg_buy_price) / avg_buy_price
                    coin['profit_rate'] = profit_rate
                else:
                    coin['profit_rate'] = float('inf')

            # 수익률이 낮은 순으로 정렬
            tradable_coins.sort(key=lambda c: c.get('profit_rate', float('inf')))

            # 각 코인 처리
            order_results = []
            for coin in tradable_coins:
                currency = coin['currency']
                market = f"KRW-{currency}"
                avg_buy_price = float(coin['avg_buy_price'])

                try:
                    # 현재가 조회
                    current_price_df = await upbit.fetch_price(market)
                    current_price = float(current_price_df.iloc[0]['close'])

                    # 기존 매수 주문 취소
                    await cancel_existing_buy_orders(market)
                    await asyncio.sleep(1)  # API 동기화 대기

                    # 분석 결과 기반 매수 주문
                    from app.services.stock_info_service import process_buy_orders_with_analysis
                    result = await process_buy_orders_with_analysis(market, current_price, avg_buy_price)

                    # 결과 확인 후 적절한 응답 추가
                    if result['success']:
                        order_results.append({
                            "currency": currency,
                            "market": market,
                            "success": True,
                            "message": result['message'],
                            "orders_placed": result.get('orders_placed', 0)
                        })
                    else:
                        order_results.append({
                            "currency": currency,
                            "market": market,
                            "success": False,
                            "message": result['message']
                        })

                except Exception as e:
                    order_results.append({
                        "currency": currency,
                        "market": market,
                        "success": False,
                        "error": str(e)
                    })

            return {
                "success": True,
                "message": f"{len(order_results)}개 코인 매수 주문 완료",
                "orders": order_results
            }

        finally:
            await analyzer.close()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sell-orders")
async def execute_sell_orders():
    """보유 코인 자동 매도 주문 실행"""
    try:
        # API 키 확인
        if not settings.upbit_access_key or not settings.upbit_secret_key:
            raise HTTPException(status_code=400, detail="Upbit API 키가 설정되지 않았습니다.")

        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 분석기 초기화
        analyzer = UpbitAnalyzer()

        try:
            # 보유 코인 조회
            my_coins = await upbit.fetch_my_coins()

            # 거래 가능한 코인 필터링
            tradable_coins = [
                coin for coin in my_coins
                if coin.get("currency") != "KRW"
                   and analyzer._is_tradable(coin)
                   and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS
            ]

            if not tradable_coins:
                return {
                    "success": True,
                    "message": "거래 가능한 코인이 없습니다.",
                    "orders": []
                }

            # 각 코인 처리
            order_results = []
            for coin in tradable_coins:
                currency = coin['currency']
                market = f"KRW-{currency}"
                balance = float(coin['balance'])
                avg_buy_price = float(coin['avg_buy_price'])

                try:
                    # 기존 매도 주문 취소
                    await cancel_existing_sell_orders(market)
                    await asyncio.sleep(1)  # API 동기화 대기

                    # 보유 수량 재조회
                    updated_coins = await upbit.fetch_my_coins()
                    balance = 0.0
                    for updated_coin in updated_coins:
                        if updated_coin.get('currency') == currency:
                            balance = float(updated_coin['balance'])
                            break

                    if balance < 0.00000001:
                        order_results.append({
                            "currency": currency,
                            "market": market,
                            "success": False,
                            "error": "보유 수량이 너무 적음"
                        })
                        continue

                    # 현재가 조회
                    current_price_df = await upbit.fetch_price(market)
                    current_price = float(current_price_df.iloc[0]['close'])

                    # 분석 결과에서 매도 가격 조회
                    sell_prices = await get_sell_prices_for_coin(currency, avg_buy_price, current_price)

                    if sell_prices:
                        # 분할 매도 주문 실행
                        result = await place_multiple_sell_orders(market, balance, sell_prices, currency)
                        if result['success']:
                            order_results.append({
                                "currency": currency,
                                "market": market,
                                "success": True,
                                "message": result['message'],
                                "orders_placed": result.get('orders_placed', 0)
                            })
                        else:
                            order_results.append({
                                "currency": currency,
                                "market": market,
                                "success": False,
                                "message": result['message']
                            })
                    else:
                        order_results.append({
                            "currency": currency,
                            "market": market,
                            "success": False,
                            "message": "매도 조건에 맞는 가격 없음"
                        })

                except Exception as e:
                    order_results.append({
                        "currency": currency,
                        "market": market,
                        "success": False,
                        "error": str(e)
                    })

            return {
                "success": True,
                "message": f"{len(order_results)}개 코인 매도 주문 완료",
                "orders": order_results
            }

        finally:
            await analyzer.close()

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
            except:
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
    except:
        pass


async def cancel_existing_sell_orders(market: str):
    """해당 마켓의 기존 매도 주문들을 취소"""
    try:
        open_orders = await upbit.fetch_open_orders(market)
        sell_orders = [order for order in open_orders if order.get('side') == 'ask']

        if sell_orders:
            order_uuids = [order['uuid'] for order in sell_orders]
            await upbit.cancel_orders(order_uuids)
    except:
        pass


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
    """여러 가격으로 분할 매도 주문

    Returns:
        dict: {'success': bool, 'message': str, 'orders_placed': int}
    """
    if not sell_prices:
        return {'success': False, 'message': '매도 가격이 없습니다', 'orders_placed': 0}

    orders_placed = 0

    if len(sell_prices) == 1:
        # 가격이 1개만 있으면 전량 매도
        result = await place_sell_order_single(market, balance, sell_prices[0])
        if result:
            orders_placed = 1
            return {'success': True, 'message': '전량 매도 주문 완료', 'orders_placed': orders_placed}
        else:
            return {'success': False, 'message': '매도 주문 실패', 'orders_placed': 0}

    # 가격 정렬
    sell_prices_sorted = sorted(sell_prices)

    # 분할 수량 체크
    split_ratio = 1.0 / len(sell_prices)
    min_split_volume = balance * split_ratio
    first_sell_price = sell_prices_sorted[0]
    split_amount = (balance * split_ratio) * first_sell_price

    if min_split_volume < 0.00000001 or split_amount < 10000:
        # 분할 불가능 - 최저가에서 전량 매도
        lowest_price = min(sell_prices_sorted)
        result = await place_sell_order_single(market, balance, lowest_price)
        if result:
            orders_placed = 1
            return {'success': True, 'message': '분할 불가능하여 전량 매도', 'orders_placed': orders_placed}
        else:
            return {'success': False, 'message': '매도 주문 실패 (분할 불가)', 'orders_placed': 0}

    # 마지막 가격 제외한 나머지로 분할 매도
    split_prices = sell_prices_sorted[:-1]
    highest_price = sell_prices_sorted[-1]

    # 분할 매도
    for sell_price in split_prices:
        try:
            split_volume = balance * split_ratio
            volume_str = f"{split_volume:.8f}"

            if split_volume < 0.00000001:
                continue

            adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
            price_str = f"{adjusted_sell_price}"

            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
        except Exception as e:
            print(f"분할 매도 주문 실패: {e}")
            continue

    # 잔량 전량 매도
    try:
        current_coins = await upbit.fetch_my_coins()
        current_balance = 0.0
        for coin in current_coins:
            if coin.get('currency') == currency:
                current_balance = float(coin['balance'])
                break

        if current_balance >= 0.00000001:
            volume_str = f"{current_balance:.8f}"
            adjusted_highest_price = upbit.adjust_price_to_upbit_unit(highest_price)
            price_str = f"{adjusted_highest_price}"

            result = await upbit.place_sell_order(market, volume_str, price_str)
            if result:
                orders_placed += 1
    except Exception as e:
        print(f"잔량 매도 주문 실패: {e}")

    if orders_placed > 0:
        return {'success': True, 'message': f'{orders_placed}단계 분할 매도 완료', 'orders_placed': orders_placed}
    else:
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
