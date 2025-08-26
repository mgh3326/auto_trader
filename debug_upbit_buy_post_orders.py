#!/usr/bin/env python3
"""
업비트 코인 자동 매수 주문 시스템
"""

import asyncio
import decimal
from typing import List, Dict, Optional
from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from app.services.stock_info_service import get_coin_sell_price, get_coin_sell_price_range
from data.coins_info import upbit_pairs



async def process_buy_orders_for_my_coins():
    """보유 코인에 대해 매수 주문 프로세스를 실행합니다."""
    
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()
    
    # JSON 분석기 초기화
    analyzer = UpbitAnalyzer()
    
    try:
        # 1. 보유 코인 정보 가져오기
        print("=== 보유 코인 조회 ===")
        my_coins = await upbit.fetch_my_coins()
        print(f"총 {len(my_coins)}개 자산 보유 중")
        
        # 2. 거래 가능한 코인만 필터링 (원화 제외, 최소 평가액 이상, KRW 마켓 거래 가능)
        tradable_coins = [
            coin for coin in my_coins
            if coin.get("currency") != "KRW"  # 원화 제외
               and analyzer._is_tradable(coin)  # 최소 평가액 이상
               and coin.get("currency") in upbit_pairs.KRW_TRADABLE_COINS  # KRW 마켓에서 거래 가능
        ]

        # 3. 코인을 보유 금액 기준으로 정렬 (큰 순서대로)
        # def calculate_total_value(coin):
        #     balance = float(coin.get('balance', 0))
        #     locked = float(coin.get('locked', 0))
        #     avg_buy_price = float(coin.get('avg_buy_price', 0))
        #     return (balance + locked) * avg_buy_price
        #
        # tradable_coins.sort(key=calculate_total_value, reverse=True)

        # 모든 코인의 현재가를 한 번에 조회하여 수익률 계산
        if tradable_coins:
            # 마켓 코드 리스트 생성
            market_codes = [f"KRW-{coin['currency']}" for coin in tradable_coins]
            
            try:
                print(f"📊 {len(market_codes)}개 코인의 현재가 일괄 조회 중...")
                
                # 업비트 공통 함수 사용하여 현재가 일괄 조회
                current_prices = await upbit.fetch_multiple_current_prices(market_codes)
                
                print(f"✅ {len(current_prices)}개 코인의 현재가 조회 완료")
                
                # 각 코인의 수익률 계산
                for coin in tradable_coins:
                    avg_buy_price = float(coin.get('avg_buy_price', 0))
                    currency = coin['currency']
                    market = f"KRW-{currency}"
                    
                    if avg_buy_price > 0 and market in current_prices:
                        current_price = current_prices[market]
                        # 수익률 계산: (현재가 - 평균 단가) / 평균 단가
                        profit_rate = (current_price - avg_buy_price) / avg_buy_price
                        coin['profit_rate'] = profit_rate
                    else:
                        # 매수 내역이 없거나 현재가 조회 실패한 경우
                        coin['profit_rate'] = float('inf')
                
            except Exception as e:
                print(f"❌ 현재가 일괄 조회 실패: {e}")
                # 실패 시 모든 코인에 기본값 설정
                for coin in tradable_coins:
                    coin['profit_rate'] = float('inf')

        # 수익률이 좋지 않은 순(오름차순)으로 정렬
        tradable_coins.sort(key=lambda c: c.get('profit_rate', float('inf')))

        print(f"거래 가능한 코인: {len(tradable_coins)}개 (수익률 낮은 순)")
        for coin in tradable_coins:
            balance = float(coin['balance'])
            locked = float(coin['locked'])
            avg_buy_price = float(coin['avg_buy_price'])
            total_value = (balance + locked) * avg_buy_price
            profit_rate = coin.get('profit_rate', float('inf'))
            
            if profit_rate == float('inf'):
                profit_str = "수익률 계산 불가"
            else:
                profit_str = f"수익률: {profit_rate:+.2%}"
            
            print(f"  - {coin['currency']}: {balance + locked:.8f} (보유 금액: {total_value:,.0f}원, 평균 단가: {avg_buy_price:,.0f}원, {profit_str})")
        
        if not tradable_coins:
            print("거래 가능한 코인이 없습니다.")
            return
        
        # 4. 각 코인에 대해 분할 매수 처리
        for coin in tradable_coins:
            await process_single_coin_buy_orders(coin, analyzer)
        
    except Exception as e:
        print(f"❌ 에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


async def process_single_coin_buy_orders(coin: dict, analyzer: UpbitAnalyzer):
    """단일 코인에 대한 분할 매수 주문을 처리합니다."""
    
    currency = coin['currency']
    market = f"KRW-{currency}"
    avg_buy_price = float(coin['avg_buy_price'])
    
    print(f"\n=== {currency} 분할 매수 처리 시작 ===")
    print(f"현재 평균 단가: {avg_buy_price:,.0f}원")
    
    try:
        # 1. 현재가 조회
        current_price_df = await upbit.fetch_price(market)
        current_price = float(current_price_df.iloc[0]['close'])
        
        print(f"현재가: {current_price:,.0f}원")
        
        # 2. 기존 매수 주문 먼저 취소 (조건과 상관없이)
        await cancel_existing_buy_orders(market)
        
        # 3. 분석 결과 기반 조건 확인 및 매수 처리
        from app.services.stock_info_service import process_buy_orders_with_analysis
        
        await process_buy_orders_with_analysis(market, current_price, avg_buy_price)
        
    except Exception as e:
        print(f"❌ {currency} 처리 중 오류: {e}")
        import traceback
        traceback.print_exc()


async def cancel_existing_buy_orders(market: str):
    """해당 마켓의 기존 매수 주문들을 취소합니다."""
    
    try:
        print(f"기존 {market} 매수 주문 조회 중...")
        
        # 해당 마켓의 체결 대기 중인 주문 조회
        open_orders = await upbit.fetch_open_orders(market)
        
        # 매수 주문만 필터링
        buy_orders = [
            order for order in open_orders 
            if order.get('side') == 'bid'  # 매수 주문
        ]
        
        if not buy_orders:
            print(f"  취소할 매수 주문이 없습니다.")
            return
        
        print(f"  {len(buy_orders)}개 매수 주문 발견")
        for order in buy_orders:
            price = float(order.get('price', 0))
            volume = float(order.get('volume', 0))
            remaining = float(order.get('remaining_volume', 0))
            print(f"    - 가격: {price:,.0f}원, 수량: {volume:.8f}, 미체결: {remaining:.8f}")
        
        # 주문 취소
        order_uuids = [order['uuid'] for order in buy_orders]
        cancel_results = await upbit.cancel_orders(order_uuids)
        
        success_count = sum(1 for result in cancel_results if 'error' not in result)
        print(f"  ✅ {success_count}/{len(buy_orders)}개 주문 취소 완료")
        
    except Exception as e:
        print(f"❌ 주문 취소 중 오류: {e}")


# 사용하지 않는 함수들 제거됨 - stock_info_service.py로 이동


async def place_split_buy_order_with_analysis(market: str, amount: int, current_price: float, buy_ranges: dict):
    """분석 결과의 매수 가격 범위를 활용한 분할 매수 주문."""
    
    try:
        print(f"💰 {market} {amount:,}원 분석 기반 지정가 매수 주문")
        
        # 1. 최적 매수 가격 결정
        order_price = determine_optimal_buy_price(current_price, buy_ranges)
        
        if order_price is None:
            print("  ⚠️ 분석 결과에 매수 가격 범위가 없습니다. 현재가 기준으로 주문합니다.")
            order_price = current_price * 1.001  # 현재가보다 0.1% 높게
        
        # 2. 매수 수량 계산 (수수료 고려)
        fee_rate = 0.0005  # 업비트 수수료 0.05%
        effective_amount = amount * (1 - fee_rate)
        volume = effective_amount / order_price
        
        print(f"  - 주문 가격: {order_price:,.0f}원")
        print(f"  - 주문 수량: {volume:.8f}")
        
        # 3. 지정가 매수 주문
        order_result = await upbit.place_buy_order(
            market=market,
            price=str(int(order_price)),
            volume=str(volume),
            ord_type="limit"
        )
        
        print(f"  ✅ 주문 성공:")
        print(f"    - 주문 ID: {order_result.get('uuid')}")
        print(f"    - 매수 가격: {order_price:,.0f}원")
        print(f"    - 매수 수량: {volume:.8f}")
        print(f"    - 예상 금액: {int(order_price) * volume:,.0f}원")
        print(f"    - 주문 시간: {order_result.get('created_at')}")
        
        return order_result
        
    except Exception as e:
        print(f"❌ 지정가 주문 실패: {e}")
        print(f"   시장가 매수로 대체 시도...")
        
        # 지정가 주문 실패 시 시장가로 대체
        try:
            order_result = await upbit.place_market_buy_order(market, str(amount))
            print(f"  ✅ 시장가 주문 성공:")
            print(f"    - 주문 ID: {order_result.get('uuid')}")
            print(f"    - 매수 금액: {amount:,}원")
            print(f"    - 주문 시간: {order_result.get('created_at')}")
            return order_result
        except Exception as e2:
            print(f"❌ 시장가 주문도 실패: {e2}")
            return None


def determine_optimal_buy_price(current_price: float, buy_ranges: dict) -> float:
    """분석 결과를 바탕으로 최적 매수 가격을 결정합니다."""
    
    appropriate_buy = buy_ranges.get('appropriate_buy')
    buy_hope = buy_ranges.get('buy_hope')
    
    print(f"  📊 분석 결과:")
    if appropriate_buy:
        print(f"    - 적절한 매수 범위: {appropriate_buy[0]:,.0f}원 ~ {appropriate_buy[1]:,.0f}원")
    if buy_hope:
        print(f"    - 희망 매수 범위: {buy_hope[0]:,.0f}원 ~ {buy_hope[1]:,.0f}원")
    
    # 전략 1: appropriate_buy 범위가 있으면 우선 사용
    if appropriate_buy:
        min_price, max_price = appropriate_buy
        
        # 현재가가 적절한 매수 범위 내에 있으면 현재가 사용
        if min_price <= current_price <= max_price:
            order_price = current_price
            print(f"  🎯 전략: 현재가가 적절한 매수 범위 내 → 현재가 사용 ({order_price:,.0f}원)")
            return order_price
        
        # 현재가가 범위보다 낮으면 최대값 사용 (더 많이 매수)
        elif current_price < min_price:
            order_price = max_price
            print(f"  🎯 전략: 현재가가 범위보다 낮음 → 최대값 사용 ({order_price:,.0f}원)")
            return order_price
        
        # 현재가가 범위보다 높으면 최소값 사용 (보수적 매수)
        else:  # current_price > max_price
            order_price = min_price
            print(f"  🎯 전략: 현재가가 범위보다 높음 → 최소값 사용 ({order_price:,.0f}원)")
            return order_price
    
    # 전략 2: appropriate_buy가 없으면 buy_hope 사용
    elif buy_hope:
        min_price, max_price = buy_hope
        
        # 희망 범위의 중간값 사용
        order_price = (min_price + max_price) / 2
        print(f"  🎯 전략: 희망 매수 범위의 중간값 사용 ({order_price:,.0f}원)")
        return order_price
    
    # 전략 3: 분석 결과가 없으면 None 반환 (현재가 기준으로 처리)
    else:
        print(f"  🎯 전략: 분석 결과 없음 → 현재가 기준으로 처리")
        return None


# 기존 함수는 백업용으로 유지
async def place_split_buy_order(market: str, amount: int, current_price: float):
    """기본 분할 매수 주문 (백업용)."""
    
    try:
        order_price = current_price * 1.001  # 현재가보다 0.1% 높은 가격
        
        fee_rate = 0.0005
        effective_amount = amount * (1 - fee_rate)
        volume = effective_amount / order_price
        
        print(f"💰 {market} {amount:,}원 기본 지정가 매수 주문")
        print(f"  - 주문 가격: {order_price:,.0f}원 (현재가의 100.1%)")
        print(f"  - 주문 수량: {volume:.8f}")
        
        order_result = await upbit.place_buy_order(
            market=market,
            price=str(int(order_price)),
            volume=str(volume),
            ord_type="limit"
        )
        
        print(f"  ✅ 주문 성공:")
        print(f"    - 주문 ID: {order_result.get('uuid')}")
        print(f"    - 매수 가격: {order_price:,.0f}원")
        print(f"    - 매수 수량: {volume:.8f}")
        print(f"    - 예상 금액: {int(order_price) * volume:,.0f}원")
        print(f"    - 주문 시간: {order_result.get('created_at')}")
        
        return order_result
        
    except Exception as e:
        print(f"❌ 매수 주문 실패: {e}")
        return None


async def main():
    """메인 실행 함수"""
    print("🚀 업비트 자동 매수 주문 시스템 시작")
    print("=" * 50)
    
    # 환경 변수 확인
    from app.core.config import settings
    if not settings.upbit_access_key or not settings.upbit_secret_key:
        print("❌ 업비트 API 키가 설정되지 않았습니다.")
        print("   UPBIT_ACCESS_KEY와 UPBIT_SECRET_KEY 환경 변수를 확인해주세요.")
        return
    
    print(f"✅ API 키 확인: Access Key {settings.upbit_access_key[:8]}...")

    await process_buy_orders_for_my_coins()
    
    print("\n" + "=" * 50)
    print("🏁 매수 주문 프로세스 완료")


if __name__ == "__main__":
    asyncio.run(main())
