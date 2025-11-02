import asyncio
from typing import Optional, List

from celery import shared_task

from app.analysis.service_analyzers import KISAnalyzer, YahooAnalyzer, UpbitAnalyzer
from app.services import upbit
from data.coins_info import upbit_pairs


@shared_task(name="analyze.run_for_stock")
def run_analysis_for_stock(symbol: str, name: str, instrument_type: str) -> dict:
    """Bridge Celery task to run the existing async analyzers.

    This runs the appropriate analyzer based on instrument_type and persists results
    using existing analyzer code paths. Returns a minimal status payload.
    """

    async def _run() -> dict:
        analyzer = None
        try:
            if instrument_type == "equity_kr":
                analyzer = KISAnalyzer()
                await analyzer.analyze_stock_json(name)
            elif instrument_type == "equity_us":
                analyzer = YahooAnalyzer()
                await analyzer.analyze_stock_json(symbol)
            elif instrument_type == "crypto":
                analyzer = UpbitAnalyzer()
                await analyzer.analyze_coin_json(name)
            else:
                return {"status": "ignored", "reason": f"unsupported type: {instrument_type}"}

            return {"status": "ok", "symbol": symbol, "name": name, "instrument_type": instrument_type}
        finally:
            if analyzer and hasattr(analyzer, "close"):
                await analyzer.close()

    # Run the async analyzer in a new event loop isolated from worker's default
    return asyncio.run(_run())


@shared_task(name="analyze.run_for_my_coins", bind=True)
def run_analysis_for_my_coins(self) -> dict:
    """보유 코인에 대한 AI 분석 실행 with progress tracking

    Returns:
        dict: {
            'status': 'completed',
            'analyzed_count': int,
            'total_count': int,
            'results': List[dict]
        }
    """

    async def _run() -> dict:
        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 분석기 초기화
        analyzer = UpbitAnalyzer()

        try:
            # 1. 보유 코인 조회
            self.update_state(
                state='PROGRESS',
                meta={
                    'current': 0,
                    'total': 0,
                    'status': '보유 코인 조회 중...',
                    'current_coin': None
                }
            )

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
                    'status': 'completed',
                    'analyzed_count': 0,
                    'total_count': 0,
                    'message': '거래 가능한 코인이 없습니다.',
                    'results': []
                }

            # 한글 이름 목록 생성
            coin_names = []
            for coin in tradable_coins:
                currency = coin.get("currency")
                korean_name = upbit_pairs.COIN_TO_NAME_KR.get(currency)
                if korean_name:
                    coin_names.append(korean_name)

            total_count = len(coin_names)
            results = []

            # 2. 각 코인 분석 (진행 상황 업데이트)
            for index, coin_name in enumerate(coin_names, 1):
                # 진행 상황 업데이트
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{coin_name} 분석 중... ({index}/{total_count})',
                        'current_coin': coin_name,
                        'percentage': int((index / total_count) * 100)
                    }
                )

                # 개별 코인 분석
                try:
                    result, model = await analyzer.analyze_coins_json([coin_name])
                    results.append({
                        'coin_name': coin_name,
                        'success': True,
                        'model': model
                    })
                except Exception as e:
                    results.append({
                        'coin_name': coin_name,
                        'success': False,
                        'error': str(e)
                    })

            # 3. 완료
            success_count = sum(1 for r in results if r['success'])

            return {
                'status': 'completed',
                'analyzed_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 코인 분석 완료',
                'results': results
            }

        except Exception as e:
            return {
                'status': 'failed',
                'error': str(e),
                'analyzed_count': 0,
                'total_count': 0,
                'results': []
            }
        finally:
            await analyzer.close()

    # Run the async analyzer in a new event loop
    return asyncio.run(_run())


@shared_task(name="upbit.execute_buy_orders", bind=True)
def execute_buy_orders_task(self) -> dict:
    """보유 코인 자동 매수 주문 실행 with progress tracking"""

    async def _run() -> dict:
        from app.services.stock_info_service import process_buy_orders_with_analysis

        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 분석기 초기화
        analyzer = UpbitAnalyzer()

        try:
            # 1. 보유 코인 조회
            self.update_state(
                state='PROGRESS',
                meta={
                    'current': 0,
                    'total': 0,
                    'status': '보유 코인 조회 중...',
                    'percentage': 0
                }
            )

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
                    'status': 'completed',
                    'success_count': 0,
                    'total_count': 0,
                    'message': '거래 가능한 코인이 없습니다.',
                    'results': []
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

            total_count = len(tradable_coins)
            order_results = []

            # 2. 각 코인 매수 주문 처리
            for index, coin in enumerate(tradable_coins, 1):
                currency = coin['currency']
                market = f"KRW-{currency}"
                avg_buy_price = float(coin['avg_buy_price'])

                # 진행 상황 업데이트
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{currency} 매수 주문 중... ({index}/{total_count})',
                        'current_coin': currency,
                        'percentage': int((index / total_count) * 100)
                    }
                )

                try:
                    # 현재가 조회
                    current_price_df = await upbit.fetch_price(market)
                    current_price = float(current_price_df.iloc[0]['close'])

                    # 기존 매수 주문 취소
                    from app.routers.upbit_trading import cancel_existing_buy_orders
                    await cancel_existing_buy_orders(market)
                    await asyncio.sleep(1)

                    # 분석 결과 기반 매수 주문
                    result = await process_buy_orders_with_analysis(market, current_price, avg_buy_price)

                    if result['success']:
                        order_results.append({
                            'currency': currency,
                            'success': True,
                            'message': result['message'],
                            'orders_placed': result.get('orders_placed', 0)
                        })
                    else:
                        order_results.append({
                            'currency': currency,
                            'success': False,
                            'message': result['message']
                        })

                except Exception as e:
                    order_results.append({
                        'currency': currency,
                        'success': False,
                        'error': str(e)
                    })

            # 3. 완료
            success_count = sum(1 for r in order_results if r['success'])

            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 코인 매수 주문 완료',
                'results': order_results
            }

        except Exception as e:
            return {
                'status': 'failed',
                'error': str(e),
                'success_count': 0,
                'total_count': 0,
                'results': []
            }
        finally:
            await analyzer.close()

    return asyncio.run(_run())


@shared_task(name="upbit.execute_sell_orders", bind=True)
def execute_sell_orders_task(self) -> dict:
    """보유 코인 자동 매도 주문 실행 with progress tracking"""

    async def _run() -> dict:
        from app.routers.upbit_trading import (
            cancel_existing_sell_orders,
            get_sell_prices_for_coin,
            place_multiple_sell_orders
        )

        # Upbit 상수 초기화
        await upbit_pairs.prime_upbit_constants()

        # 분석기 초기화
        analyzer = UpbitAnalyzer()

        try:
            # 1. 보유 코인 조회
            self.update_state(
                state='PROGRESS',
                meta={
                    'current': 0,
                    'total': 0,
                    'status': '보유 코인 조회 중...',
                    'percentage': 0
                }
            )

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
                    'status': 'completed',
                    'success_count': 0,
                    'total_count': 0,
                    'message': '거래 가능한 코인이 없습니다.',
                    'results': []
                }

            total_count = len(tradable_coins)
            order_results = []

            # 2. 각 코인 매도 주문 처리
            for index, coin in enumerate(tradable_coins, 1):
                currency = coin['currency']
                market = f"KRW-{currency}"
                balance = float(coin['balance'])
                avg_buy_price = float(coin['avg_buy_price'])

                # 진행 상황 업데이트
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{currency} 매도 주문 중... ({index}/{total_count})',
                        'current_coin': currency,
                        'percentage': int((index / total_count) * 100)
                    }
                )

                try:
                    # 기존 매도 주문 취소
                    await cancel_existing_sell_orders(market)
                    await asyncio.sleep(1)

                    # 보유 수량 재조회
                    updated_coins = await upbit.fetch_my_coins()
                    balance = 0.0
                    for updated_coin in updated_coins:
                        if updated_coin.get('currency') == currency:
                            balance = float(updated_coin['balance'])
                            break

                    if balance < 0.00000001:
                        order_results.append({
                            'currency': currency,
                            'success': False,
                            'message': '보유 수량이 너무 적음'
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
                                'currency': currency,
                                'success': True,
                                'message': result['message'],
                                'orders_placed': result.get('orders_placed', 0)
                            })
                        else:
                            order_results.append({
                                'currency': currency,
                                'success': False,
                                'message': result['message']
                            })
                    else:
                        order_results.append({
                            'currency': currency,
                            'success': False,
                            'message': '매도 조건에 맞는 가격 없음'
                        })

                except Exception as e:
                    order_results.append({
                        'currency': currency,
                        'success': False,
                        'error': str(e)
                    })

            # 3. 완료
            success_count = sum(1 for r in order_results if r['success'])

            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 코인 매도 주문 완료',
                'results': order_results
            }

        except Exception as e:
            return {
                'status': 'failed',
                'error': str(e),
                'success_count': 0,
                'total_count': 0,
                'results': []
            }
        finally:
            await analyzer.close()

    return asyncio.run(_run())



