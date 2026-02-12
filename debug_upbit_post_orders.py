#!/usr/bin/env python3
"""
업비트 코인 자동 매도 주문 시스템
"""

import asyncio

from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from data.coins_info import upbit_pairs

# ===== 매도 전략 설정 =====
SELL_STRATEGY = "split"  # "split": 분할 지정가 매도 | "market": 전량 시장가 매도


# =========================


async def process_sell_orders_for_my_coins():
    """보유 코인에 대해 매도 주문 프로세스를 실행합니다."""

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

        print(f"거래 가능한 코인: {len(tradable_coins)}개")
        for coin in tradable_coins:
            balance = float(coin['balance'])
            avg_buy_price = float(coin['avg_buy_price'])
            evaluation = balance * avg_buy_price
            print(f"  - {coin['currency']}: {balance:.8f} (평가액: {evaluation:,.0f}원)")

        if not tradable_coins:
            print("거래 가능한 코인이 없습니다.")
            return

        # 3. 각 코인에 대해 매도 주문 처리
        for coin in tradable_coins:
            currency = coin['currency']
            market = f"KRW-{currency}"
            balance = float(coin['balance'])
            avg_buy_price = float(coin['avg_buy_price'])

            print(f"\n=== {currency} 매도 주문 처리 ===")
            print(f"  보유 수량: {balance:.8f} {currency}")
            print(f"  평균 매수가: {avg_buy_price:,.0f}원")

            # 3-1. 기존 매도 주문 확인 및 취소
            await cancel_existing_sell_orders(market)
            # --- 추가: API 서버 데이터 동기화를 위해 잠시 대기 ---
            print("  ⏳ API 서버 동기화를 위해 1초 대기...")
            await asyncio.sleep(1)

            # 3-2. 주문 취소 후 보유 수량 재조회
            print("  🔄 주문 취소 후 보유 수량 재조회...")
            updated_coins = await upbit.fetch_my_coins()

            # 현재 코인의 업데이트된 수량 찾기
            old_balance = balance
            balance = 0.0
            for updated_coin in updated_coins:
                if updated_coin.get('currency') == currency:
                    balance = float(updated_coin['balance'])
                    break

            print(f"  📊 업데이트된 보유 수량: {balance:.8f} {currency}")

            # 수량이 변경되었으면 표시
            if abs(balance - old_balance) > 0.00000001:
                diff = balance - old_balance
                print(f"     🔄 수량 변화: {diff:+.8f} {currency} (취소된 주문으로 인한 변화)")

            # 최소 주문 수량 체크 (주문 취소 후 최종 수량으로 체크)
            if balance < 0.00000001:
                print("  ⚠️  최종 보유 수량이 너무 적어 매도 불가능 (최소: 0.00000001)")
                continue

            # 3-3. 현재가 조회
            current_price_df = await upbit.fetch_price(market)
            current_price = float(current_price_df.iloc[0]['close'])
            print(f"  💰 현재가: {current_price:,.0f}원")

            # 3-4. 매도 전략에 따른 주문 실행
            if SELL_STRATEGY == "split":
                # 분석 결과에서 매도 가격들 조회 (1% 이상 수익 가능하고 현재가 이상인 가격들)
                sell_prices = await get_sell_prices_for_coin(currency, avg_buy_price, current_price)
                if sell_prices:
                    print(f"  📊 분할 지정가 매도 전략 ({len(sell_prices)}개 가격)")
                    await place_multiple_sell_orders(market, balance, sell_prices, currency)

    except Exception as e:
        print(f"❌ 에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


async def cancel_existing_sell_orders(market: str):
    """특정 마켓의 기존 매도 주문들을 취소합니다."""
    try:
        # 체결 대기 중인 주문 조회
        open_orders = await upbit.fetch_open_orders(market)

        # 매도 주문만 필터링
        sell_orders = [order for order in open_orders if order.get('side') == 'ask']

        if not sell_orders:
            print(f"  ✅ {market}에 기존 매도 주문이 없습니다.")
            return

        print(f"  📋 {market}에 {len(sell_orders)}개의 매도 주문이 있습니다.")

        # 주문 취소
        order_uuids = [order['uuid'] for order in sell_orders]
        cancel_results = await upbit.cancel_orders(order_uuids)

        success_count = len([r for r in cancel_results if 'error' not in r])
        print(f"  ✅ {success_count}/{len(order_uuids)}개 주문 취소 완료")

    except Exception as e:
        print(f"  ❌ 기존 주문 취소 실패: {e}")


async def get_sell_prices_for_coin(currency: str, avg_buy_price: float, current_price: float) -> list[float]:
    """코인의 매도 가격들을 분석 결과에서 조회합니다."""
    try:
        # KRW-{currency} 형태의 심볼로 분석 결과 조회
        symbol = f"KRW-{currency}"

        # 분석 결과에서 전체 정보 조회
        from app.core.db import AsyncSessionLocal
        from app.services.stock_info_service import StockAnalysisService

        async with AsyncSessionLocal() as db:
            service = StockAnalysisService(db)
            analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            print(f"  ⚠️  {symbol}의 분석 결과가 없습니다.")
            return []

        # 4개 매도 가격 후보 수집
        sell_prices = []

        # appropriate_sell 범위
        if analysis.appropriate_sell_min is not None:
            sell_prices.append(("appropriate_sell_min", analysis.appropriate_sell_min))
        if analysis.appropriate_sell_max is not None:
            sell_prices.append(("appropriate_sell_max", analysis.appropriate_sell_max))

        # sell_target 범위
        if analysis.sell_target_min is not None:
            sell_prices.append(("sell_target_min", analysis.sell_target_min))
        if analysis.sell_target_max is not None:
            sell_prices.append(("sell_target_max", analysis.sell_target_max))

        # 평균 매수가 대비 1% 이상이고 현재가 이상인 가격들만 필터링
        min_sell_price = avg_buy_price * 1.01  # 1% 이상
        valid_prices = [(name, price) for name, price in sell_prices if
                        price >= min_sell_price and price >= current_price]

        if not valid_prices:
            print(f"  ⚠️  {symbol}의 매도 가격이 조건에 맞지 않습니다.")
            print(f"      - 평균 매수가: {avg_buy_price:,.0f}원 (1% 이상: {min_sell_price:,.0f}원)")
            print(f"      - 현재가: {current_price:,.0f}원")
            print(f"      - 조건: 매도가 >= {max(min_sell_price, current_price):,.0f}원")
            return []

        # 가격 오름차순 정렬
        valid_prices.sort(key=lambda x: x[1])

        print(f"  💰 {symbol} 분단 매도 가격 ({len(valid_prices)}개):")
        for name, price in valid_prices:
            profit_rate = ((price / avg_buy_price) - 1) * 100
            current_premium = ((price / current_price) - 1) * 100
            print(f"     {name}: {price:,.0f}원 (수익률: +{profit_rate:.1f}%, 현재가 대비: +{current_premium:.1f}%)")

        return [price for name, price in valid_prices]

    except Exception as e:
        print(f"  ❌ {currency} 매도 가격 조회 실패: {e}")
        return []


async def place_multiple_sell_orders(market: str, balance: float, sell_prices: list[float], currency: str):
    """여러 가격으로 분할 매도 주문을 넣습니다. 마지막은 최고가에서 전량 매도."""
    if not sell_prices:
        print("  ⚠️  매도 주문할 가격이 없습니다.")
        return

    if len(sell_prices) == 1:
        # 가격이 1개만 있으면 전량 매도
        print("  📤 단일 가격 전량 매도")
        await place_new_sell_order(market, balance, sell_prices[0], currency)
        return

    # 가격을 오름차순으로 정렬
    sell_prices_sorted = sorted(sell_prices)

    # 분할 수량이 최소 주문 수량을 만족하는지 체크
    split_ratio = 1.0 / len(sell_prices)
    min_split_volume = balance * split_ratio

    # 분할한 개별 금액 계산 (첫 번째 매도 가격 기준)
    first_sell_price = sell_prices_sorted[0]
    split_amount = (balance * split_ratio) * first_sell_price

    if min_split_volume < 0.00000001 or split_amount < 10000:
        reason = ""
        if min_split_volume < 0.00000001:
            reason += "보유 수량이 적어 분할 불가능"
        if split_amount < 10000:
            if reason:
                reason += " 및 "
            reason += f"분할 금액이 1만원 미만 ({split_amount:,.0f}원)"

        print(f"  ⚠️  {reason}. 최저가에서 전량 매도로 전환")
        lowest_price = min(sell_prices_sorted)
        await place_new_sell_order(market, balance, lowest_price, currency)
        return

    # 마지막 가격을 제외한 나머지 가격들로 분할 매도
    split_prices = sell_prices_sorted[:-1]  # 마지막 가격 제외
    highest_price = sell_prices_sorted[-1]  # 최고가

    print(f"  📤 {len(sell_prices)}단계 분할 매도 (분할: {len(split_prices)}개 × {split_ratio * 100:.1f}%, 전량: 1개)")

    success_count = 0
    total_expected_amount = 0
    remaining_balance = balance

    # 1단계: 분할 매도
    executed_volumes = []  # 실제 체결된 수량들 기록

    for i, sell_price in enumerate(split_prices, 1):
        try:
            # 분할 수량 계산
            split_volume = balance * split_ratio
            volume_str = f"{split_volume:.8f}"

            # 업비트 가격 단위에 맞게 조정
            adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
            price_str = f"{adjusted_sell_price}"

            print(f"  📤 [{i}/{len(sell_prices)}] 분할: {volume_str} {currency}")
            print(f"       원본 가격: {sell_price:,.2f}원 → 조정 가격: {adjusted_sell_price}원")

            # 최소 주문 수량 체크
            if split_volume < 0.00000001:
                print("       ⚠️  분할 수량이 너무 적어 건너뜀 (최소: 0.00000001)")
                continue

            # 매도 주문 실행
            print(f"       🔄 API 호출 중... (market: {market})")
            order_result = await upbit.place_sell_order(market, volume_str, price_str)

            volume_executed = float(order_result.get('volume', 0))
            price_executed = float(order_result.get('price', 0))
            expected_amount = volume_executed * price_executed
            total_expected_amount += expected_amount

            # 실제 체결된 수량 기록
            executed_volumes.append(volume_executed)

            print(f"       ✅ 성공! ID: {order_result.get('uuid')[:8]}... (예상: {expected_amount:,.0f}원)")
            success_count += 1

        except Exception as e:
            print(f"       ❌ 실패: {e}")
            _print_error_hint(e)

    # 2단계: 현재 실제 보유 수량을 다시 조회해서 정확한 잔량 확인
    try:
        print("       🔄 마지막 매도 전 현재 보유 수량 확인...")
        current_coins = await upbit.fetch_my_coins()

        # 현재 실제 보유 수량 찾기
        current_balance = 0.0
        for coin in current_coins:
            if coin.get('currency') == currency:
                current_balance = float(coin['balance'])
                break

        print(f"       📊 현재 실제 보유 수량: {current_balance:.8f} {currency}")

        # 실제 보유 수량으로 전량 매도
        volume_str = f"{current_balance:.8f}"

        # 업비트 가격 단위에 맞게 조정
        adjusted_highest_price = upbit.adjust_price_to_upbit_unit(highest_price)
        price_str = f"{adjusted_highest_price}"

        print(f"  📤 [{len(sell_prices)}/{len(sell_prices)}] 전량: {volume_str} {currency}")
        print(f"       원본 가격: {highest_price:,.2f}원 → 조정 가격: {adjusted_highest_price}원")
        print("       🎯 최고가에서 실제 보유 수량 전부 매도!")

        # 최소 주문 수량 체크
        if current_balance < 0.00000001:
            print("       ⚠️  현재 보유 수량이 너무 적어 매도 불가능 (최소: 0.00000001)")
            print(f"       📊 분할 매도 결과: {success_count}/{len(sell_prices) - 1}개 성공 (잔량 매도 생략)")
            return

        # 매도 주문 실행
        print(f"       🔄 API 호출 중... (market: {market})")
        order_result = await upbit.place_sell_order(market, volume_str, price_str)

        volume_executed = float(order_result.get('volume', 0))
        price_executed = float(order_result.get('price', 0))
        expected_amount = volume_executed * price_executed
        total_expected_amount += expected_amount

        print(f"       ✅ 성공! ID: {order_result.get('uuid')[:8]}... (예상: {expected_amount:,.0f}원)")
        print("       ✨ 잔액 없이 깔끔하게 완료!")
        success_count += 1

    except Exception as e:
        print(f"       ❌ 전량 매도 실패: {e}")
        _print_error_hint(e)

    print(f"  📊 분할 매도 결과: {success_count}/{len(sell_prices)}개 성공")
    if total_expected_amount > 0:
        print(f"     총 예상 수령액: {total_expected_amount:,.0f}원")


def _print_error_hint(e: Exception):
    """에러 메시지에 따른 힌트 출력"""
    error_str = str(e).lower()
    if "401" in error_str:
        print("          💡 API 키 인증 문제일 수 있습니다. 키를 확인해주세요.")
    elif "400" in error_str:
        print("          💡 주문 파라미터 문제일 수 있습니다.")
        if "volume" in error_str or "수량" in error_str:
            print("             - 최소 주문 수량: 0.00000001 이상")
            print("             - 최대 소수점 자리: 8자리")
        if "price" in error_str or "가격" in error_str:
            print("             - 가격은 정수 단위로 입력")
    elif "429" in error_str:
        print("          💡 API 호출 제한에 걸렸습니다. 잠시 후 다시 시도해주세요.")


async def place_market_sell_all(market: str, balance: float, currency: str):
    raise RuntimeError("시장가 매도 금지")

    """전량 시장가 매도 주문을 넣습니다."""
    try:
        volume_str = f"{balance:.8f}"

        print(f"  💥 전량 시장가 매도 실행: {volume_str} {currency}")
        print("       🔄 시장가로 즉시 체결 시도...")

        # 시장가 매도 주문 실행
        order_result = await upbit.place_market_sell_order(market, volume_str)

        volume_executed = float(order_result.get('volume', 0))
        trades = order_result.get('trades', [])
        total_funds = sum(float(trade.get('funds', 0)) for trade in trades) if trades else 0

        print("  ✅ 전량 매도 성공!")
        print(f"     주문 ID: {order_result.get('uuid')}")
        print(f"     매도 수량: {volume_executed} {currency}")
        if total_funds > 0:
            print(f"     실제 수령액: {total_funds:,.0f}원")
            avg_price = total_funds / volume_executed if volume_executed > 0 else 0
            print(f"     평균 체결가: {avg_price:,.0f}원")
        print("     ✨ 잔액 없이 깔끔하게 전량 매도 완료!")

    except Exception as e:
        print(f"  ❌ 전량 매도 실패: {e}")
        _print_error_hint(e)


async def place_new_sell_order(market: str, balance: float, sell_price: float, currency: str):
    """단일 매도 주문을 넣습니다. (기존 호환성을 위해 유지)"""
    try:
        # 수량을 문자열로 변환 (소수점 8자리까지)
        volume_str = f"{balance:.8f}"

        # 업비트 가격 단위에 맞게 조정
        adjusted_sell_price = upbit.adjust_price_to_upbit_unit(sell_price)
        price_str = f"{adjusted_sell_price}"

        print(f"  📤 매도 주문 실행: {volume_str} {currency}")
        print(f"     원본 가격: {sell_price:,.2f}원 → 조정 가격: {adjusted_sell_price}원")

        # 매도 주문 실행
        order_result = await upbit.place_sell_order(market, volume_str, price_str)

        print("  ✅ 매도 주문 성공!")
        print(f"     주문 ID: {order_result.get('uuid')}")
        print(f"     수량: {order_result.get('volume')} {currency}")
        print(f"     가격: {order_result.get('price')}원")
        print(f"     예상 수령액: {float(order_result.get('volume', 0)) * float(order_result.get('price', 0)):,.0f}원")

    except Exception as e:
        print(f"  ❌ 매도 주문 실패: {e}")
        _print_error_hint(e)


async def main():
    """메인 실행 함수"""
    print("🚀 업비트 자동 매도 주문 시스템 시작")
    print("=" * 50)

    # 환경 변수 확인
    from app.core.config import settings
    if not settings.upbit_access_key or not settings.upbit_secret_key:
        print("❌ 업비트 API 키가 설정되지 않았습니다.")
        print("   UPBIT_ACCESS_KEY와 UPBIT_SECRET_KEY 환경 변수를 확인해주세요.")
        return

    print(f"✅ API 키 확인: Access Key {settings.upbit_access_key[:8]}...")

    # 매도 전략 표시
    strategy_name = "🔀 분할 지정가 매도" if SELL_STRATEGY == "split" else "💥 전량 시장가 매도"
    print(f"📋 매도 전략: {strategy_name}")
    if SELL_STRATEGY == "split":
        print("   → 분석 가격들로 분할 매도 후, 최고가에서 잔량 전부 매도 (잔액 없음)")
    else:
        print("   → 현재 시장가로 즉시 전량 매도 (잔액 없음)")

    await process_sell_orders_for_my_coins()

    print("\n" + "=" * 50)
    print("🏁 매도 주문 프로세스 완료")


if __name__ == "__main__":
    asyncio.run(main())
