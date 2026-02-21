#!/usr/bin/env python3
"""
업비트 코인 자동 매도 주문 시스템
"""

import asyncio

from app.analysis.service_analyzers import UpbitAnalyzer
from app.services import upbit
from app.services import upbit_symbol_universe_service as upbit_pairs

# =========================


async def process_cancel_orders():
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
        all_market_codes = await upbit.fetch_all_market_codes()

        # tradable_coins에서 currency를 추출하여 KRW- 마켓 코드로 변환
        tradable_market_codes = {f"KRW-{coin['currency']}" for coin in tradable_coins}
        # all_market_codes에서 tradable_coins에 없는 market_code만 필터링
        non_tradable_market_codes = [
            market_code for market_code in all_market_codes
            if market_code not in tradable_market_codes
        ]

        print(f"거래 불가능한 마켓: {len(non_tradable_market_codes)}개")
        for market_code in non_tradable_market_codes:
            print(f"  - {market_code}")

        # 4. 거래 불가능한 마켓에 대한 매수 주문 확인 및 취소
        if non_tradable_market_codes:
            print("\n=== 거래 불가능한 마켓 매수 주문 확인 및 취소 ===")
            await check_and_cancel_buy_orders_for_non_tradable_markets(non_tradable_market_codes)



    except Exception as e:
        print(f"❌ 에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


async def check_and_cancel_buy_orders_for_non_tradable_markets(market_codes: list[str]):
    """거래 불가능한 마켓들에 대한 매수 주문을 확인하고 취소합니다."""
    try:
        total_buy_orders = 0
        total_cancelled = 0

        for market_code in market_codes:
            # 특정 마켓의 체결 대기 중인 매수 주문 조회
            open_orders = await upbit.fetch_open_orders(market_code)

            # 매수 주문만 필터링 (side: 'bid')
            buy_orders = [order for order in open_orders if order.get('side') == 'bid']

            if not buy_orders:
                continue

            total_buy_orders += len(buy_orders)
            print(f"  📋 {market_code}: {len(buy_orders)}개 매수 주문 발견")

            # 각 매수 주문 정보 출력
            for order in buy_orders:
                volume = float(order.get('volume', 0))
                price = float(order.get('price', 0))
                remaining_volume = float(order.get('remaining_volume', 0))
                reserved_fee = float(order.get('reserved_fee', 0))
                remaining_fee = float(order.get('remaining_fee', 0))
                paid_fee = float(order.get('paid_fee', 0))
                locked = float(order.get('locked', 0))
                executed_volume = float(order.get('executed_volume', 0))
                trade_count = int(order.get('trades_count', 0))

                print(f"     - ID: {order.get('uuid')[:8]}...")
                print(f"       주문량: {volume:.8f} | 가격: {price:,.0f}원")
                print(f"       미체결량: {remaining_volume:.8f} | 잠김금액: {locked:,.0f}원")
                if executed_volume > 0:
                    print(f"       체결량: {executed_volume:.8f} ({trade_count}회 체결)")

            # 매수 주문 취소
            order_uuids = [order['uuid'] for order in buy_orders]

            print(f"  🔄 {len(order_uuids)}개 매수 주문 취소 중...")
            cancel_results = await upbit.cancel_orders(order_uuids)

            success_count = len([r for r in cancel_results if 'error' not in r])
            total_cancelled += success_count

            if success_count == len(order_uuids):
                print(f"  ✅ {market_code}: 모든 매수 주문 취소 성공 ({success_count}개)")
            else:
                failed_count = len(order_uuids) - success_count
                print(f"  ⚠️  {market_code}: {success_count}/{len(order_uuids)}개 취소 성공, {failed_count}개 실패")

                # 실패한 주문들 상세 정보 출력
                for i, result in enumerate(cancel_results):
                    if 'error' in result:
                        print(
                            f"     ❌ 실패: {order_uuids[i][:8]}... - {result.get('error', {}).get('message', '알 수 없는 오류')}")

        print("\n📊 매수 주문 취소 결과:")
        print(f"   발견된 매수 주문: {total_buy_orders}개")
        print(f"   취소 성공: {total_cancelled}개")
        if total_buy_orders > total_cancelled:
            print(f"   취소 실패: {total_buy_orders - total_cancelled}개")

    except Exception as e:
        print(f"❌ 매수 주문 확인/취소 실패: {e}")
        import traceback
        traceback.print_exc()




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

    await process_cancel_orders()

    print("\n" + "=" * 50)
    print("🏁 매도 주문 프로세스 완료")


if __name__ == "__main__":
    asyncio.run(main())
