#!/usr/bin/env python3
"""
KIS 국내주식 자동 매도 주문 시스템
"""

import asyncio

from app.analysis.service_analyzers import KISAnalyzer
from app.services.kis import kis

# ===== 매도 전략 설정 =====
SELL_STRATEGY = "split"  # "split": 분할 지정가 매도

# =========================


async def cancel_existing_sell_orders(
    stock_code: str,
    all_open_orders: list[dict],
    is_mock: bool = False
):
    """
    특정 종목의 기존 매도 주문들을 취소합니다.

    Args:
        stock_code: 종목코드
        all_open_orders: 미리 조회한 전체 미체결 주문 목록
        is_mock: 모의투자 여부
    """
    try:
        # 해당 종목의 매도 주문만 필터링
        # sll_buy_dvsn_cd: 01=매도, 02=매수
        sell_orders = [
            order for order in all_open_orders
            if order.get('pdno') == stock_code and order.get('sll_buy_dvsn_cd') == '01'
        ]

        if not sell_orders:
            print(f"  ✅ {stock_code}에 기존 매도 주문이 없습니다.")
            return

        print(f"  📋 {stock_code}에 {len(sell_orders)}개의 매도 주문이 있습니다.")

        # 주문 취소
        success_count = 0
        for order in sell_orders:
            try:
                order_number = order.get('ord_no')  # 주문번호
                order_qty = int(order.get('ord_qty', 0))  # 주문수량
                order_price = int(float(order.get('ord_unpr', 0)))  # 주문단가

                print(f"     🔄 주문 취소 중: {order_number} ({order_qty}주 @ {order_price:,}원)")

                result = await kis.cancel_korea_order(
                    order_number=order_number,
                    stock_code=stock_code,
                    quantity=order_qty,
                    price=order_price,
                    order_type="sell",
                    is_mock=is_mock
                )

                print(f"     ✅ 취소 완료: {result.get('odno')}")
                success_count += 1

                # API 호출 제한 방지를 위한 대기
                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"     ❌ 주문 취소 실패: {e}")

        print(f"  ✅ {success_count}/{len(sell_orders)}개 주문 취소 완료")

    except Exception as e:
        print(f"  ❌ 기존 주문 취소 실패: {e}")


async def process_sell_orders_for_my_stocks():
    """보유 국내주식에 대해 매도 주문 프로세스를 실행합니다."""

    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

    try:
        print("=== 보유 국내주식 조회 ===")

        # 국내주식 조회
        kr_stocks = await kis.fetch_my_stocks(is_mock=False, is_overseas=False)

        if not kr_stocks:
            print("거래 가능한 국내주식이 없습니다.")
            return

        print(f"\n총 {len(kr_stocks)}개 종목 보유 중")

        # 보유 주식 정보 출력
        for stock in kr_stocks:
            stock_code = stock.get('pdno')  # 종목코드
            stock_name = stock.get('prdt_name')  # 종목명
            quantity = int(stock.get('hldg_qty', 0))  # 보유수량
            avg_buy_price = int(float(stock.get('pchs_avg_pric', 0)))  # 매입평균가격
            evaluation = quantity * avg_buy_price
            print(f"  - {stock_name} ({stock_code}): {quantity}주 (평가액: {evaluation:,}원)")

        # 미체결 주문 조회 (한 번만)
        print("\n=== 미체결 주문 조회 ===")
        all_open_orders = await kis.inquire_korea_orders(is_mock=False)
        print(f"총 {len(all_open_orders)}개의 미체결 주문 발견")

        # 매도 주문만 카운트
        sell_orders_count = len([o for o in all_open_orders if o.get('sll_buy_dvsn_cd') == '01'])
        print(f"  - 매도 주문: {sell_orders_count}개")
        print(f"  - 매수 주문: {len(all_open_orders) - sell_orders_count}개")

        # 각 주식에 대해 매도 주문 처리
        for stock in kr_stocks:
            stock_code = stock.get('pdno')  # 종목코드
            stock_name = stock.get('prdt_name')  # 종목명
            quantity = int(stock.get('hldg_qty', 0))  # 보유수량
            avg_buy_price = int(float(stock.get('pchs_avg_pric', 0)))  # 매입평균가격

            print(f"\n{'=' * 70}")
            print(f"=== {stock_name} ({stock_code}) 매도 주문 처리 ===")
            print(f"  보유 수량: {quantity}주")
            print(f"  평균 매수가: {avg_buy_price:,}원")

            # 최소 주문 수량 체크
            if quantity < 1:
                print("  ⚠️  보유 수량이 1주 미만이어서 매도 불가능")
                continue

            # 현재가 조회
            try:
                current_price_df = await kis.inquire_price(stock_code)
                current_price = int(float(current_price_df.iloc[0]['close']))
                print(f"  💰 현재가: {current_price:,}원")
            except Exception as e:
                print(f"  ❌ 현재가 조회 실패: {e}")
                continue

            # 기존 매도 주문 확인 및 취소 (미리 조회한 데이터 사용)
            print("\n  🔍 기존 매도 주문 확인 및 취소...")
            await cancel_existing_sell_orders(stock_code, all_open_orders, is_mock=False)

            # API 서버 데이터 동기화를 위해 잠시 대기
            print("  ⏳ API 서버 동기화를 위해 1초 대기...")
            await asyncio.sleep(1)

            # 매도 전략에 따른 주문 실행
            if SELL_STRATEGY == "split":
                # 분석 결과에서 매도 가격들 조회 (1% 이상 수익 가능하고 현재가 이상인 가격들)
                sell_prices = await get_sell_prices_for_stock(
                    stock_code, stock_name, avg_buy_price, current_price
                )
                if sell_prices:
                    print(f"  📊 분할 지정가 매도 전략 ({len(sell_prices)}개 가격)")
                    await place_multiple_sell_orders(
                        stock_code, quantity, sell_prices, current_price
                    )
                else:
                    print("  ⚠️  조건에 맞는 매도 가격이 없어 주문 생략")

    except Exception as e:
        print(f"❌ 에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


async def get_sell_prices_for_stock(
    stock_code: str, stock_name: str, avg_buy_price: int, current_price: int
) -> list[int]:
    """주식의 매도 가격들을 분석 결과에서 조회합니다."""
    try:
        # 분석 결과에서 전체 정보 조회
        from app.core.db import AsyncSessionLocal
        from app.services.stock_info_service import StockAnalysisService

        async with AsyncSessionLocal() as db:
            service = StockAnalysisService(db)
            # 국내주식은 종목코드로 조회
            analysis = await service.get_latest_analysis_by_symbol(stock_code)

        if not analysis:
            print(f"  ⚠️  {stock_name}의 분석 결과가 없습니다.")
            return []

        # 4개 매도 가격 후보 수집
        sell_prices = []

        # appropriate_sell 범위
        if analysis.appropriate_sell_min is not None:
            sell_prices.append(("appropriate_sell_min", int(analysis.appropriate_sell_min)))
        if analysis.appropriate_sell_max is not None:
            sell_prices.append(("appropriate_sell_max", int(analysis.appropriate_sell_max)))

        # sell_target 범위
        if analysis.sell_target_min is not None:
            sell_prices.append(("sell_target_min", int(analysis.sell_target_min)))
        if analysis.sell_target_max is not None:
            sell_prices.append(("sell_target_max", int(analysis.sell_target_max)))

        # 평균 매수가 대비 1% 이상이고 현재가 이상인 가격들만 필터링
        min_sell_price = int(avg_buy_price * 1.01)  # 1% 이상
        valid_prices = [
            (name, price)
            for name, price in sell_prices
            if price >= min_sell_price and price >= current_price
        ]

        if not valid_prices:
            print(f"  ⚠️  {stock_name}의 매도 가격이 조건에 맞지 않습니다.")
            print(f"      - 평균 매수가: {avg_buy_price:,}원 (1% 이상: {min_sell_price:,}원)")
            print(f"      - 현재가: {current_price:,}원")
            print(f"      - 조건: 매도가 >= {max(min_sell_price, current_price):,}원")
            return []

        # 가격 오름차순 정렬
        valid_prices.sort(key=lambda x: x[1])

        print(f"  💰 {stock_name} 분할 매도 가격 ({len(valid_prices)}개):")
        for name, price in valid_prices:
            profit_rate = ((price / avg_buy_price) - 1) * 100
            current_premium = ((price / current_price) - 1) * 100
            print(
                f"     {name}: {price:,}원 (수익률: +{profit_rate:.1f}%, "
                f"현재가 대비: +{current_premium:.1f}%)"
            )

        return [price for name, price in valid_prices]

    except Exception as e:
        print(f"  ❌ {stock_name} 매도 가격 조회 실패: {e}")
        return []


async def place_multiple_sell_orders(
    stock_code: str,
    quantity: int,
    sell_prices: list[int],
    current_price: int,
):
    """여러 가격으로 분할 매도 주문을 넣습니다. 마지막은 최고가에서 전량 매도."""
    if not sell_prices:
        print("  ⚠️  매도 주문할 가격이 없습니다.")
        return

    if len(sell_prices) == 1:
        # 가격이 1개만 있으면 전량 매도
        print("  📤 단일 가격 전량 매도")
        await place_new_sell_order(
            stock_code, quantity, sell_prices[0]
        )
        return

    # 가격을 오름차순으로 정렬
    sell_prices_sorted = sorted(sell_prices)

    # 보유 수량과 가격 개수 비교
    num_prices = len(sell_prices_sorted)

    if quantity < num_prices:
        # 보유 수량이 가격 개수보다 적음 → 보유 수량만큼만 가격 사용
        # 예: 2주 보유, 4개 가격 → 첫 2개 가격에 1주씩
        split_prices = sell_prices_sorted[:quantity - 1]  # 마지막 1개 제외
        highest_price = sell_prices_sorted[quantity - 1]  # 보유 수량 번째 가격
        shares_per_price = 1  # 각 가격에 1주씩
        print(
            f"  📤 {quantity}단계 분할 매도 "
            f"(분할: {len(split_prices)}개 × 1주, 전량: 1개 × 1주)"
        )
    else:
        # 보유 수량이 가격 개수 이상 → 균등 분할
        # 예: 10주 보유, 4개 가격 → 3개 가격에 2주씩, 마지막에 4주
        split_prices = sell_prices_sorted[:-1]  # 마지막 가격 제외
        highest_price = sell_prices_sorted[-1]  # 최고가
        shares_per_price = quantity // num_prices  # 각 가격에 배분할 주수

        # 최소 1주씩은 배분
        if shares_per_price < 1:
            shares_per_price = 1

        print(
            f"  📤 {num_prices}단계 분할 매도 "
            f"(분할: {len(split_prices)}개 × {shares_per_price}주, 전량: 1개)"
        )

    success_count = 0
    total_expected_amount = 0

    # 1단계: 분할 매도
    for i, sell_price in enumerate(split_prices, 1):
        try:
            print(f"  📤 [{i}/{len(split_prices) + 1}] 분할: {shares_per_price}주")
            print(f"       가격: {sell_price:,}원")

            # 매도 주문 실행
            print("       🔄 API 호출 중...")
            order_result = await kis.sell_korea_stock(
                stock_code=stock_code,
                quantity=shares_per_price,
                price=sell_price,
                is_mock=False,
            )

            expected_amount = shares_per_price * sell_price
            total_expected_amount += expected_amount

            print(
                f"       ✅ 성공! 주문번호: {order_result.get('odno')} "
                f"(예상: {expected_amount:,}원)"
            )
            success_count += 1

            # API 호출 제한 방지를 위한 대기
            await asyncio.sleep(0.2)

        except Exception as e:
            print(f"       ❌ 실패: {e}")
            _print_error_hint(e)

    # 2단계: 최고가에서 잔량 전량 매도
    try:
        # 실제 남은 수량 계산 (분할 매도에서 성공한 만큼 제외)
        remaining_quantity = quantity - (success_count * shares_per_price)

        print(
            f"  📤 [{len(split_prices) + 1}/{len(split_prices) + 1}] "
            f"전량: {remaining_quantity}주"
        )
        print(f"       가격: {highest_price:,}원")
        print("       🎯 최고가에서 잔량 전부 매도!")

        # 최소 주문 수량 체크 (1주 이상만 허용)
        if remaining_quantity < 1:
            print("       ⚠️  잔량이 1주 미만이어서 매도 불가능")
            print(
                f"       📊 분할 매도 결과: {success_count}/{len(split_prices)}개 성공 "
                f"(잔량 매도 생략)"
            )
            return

        # 매도 주문 실행
        print("       🔄 API 호출 중...")
        order_result = await kis.sell_korea_stock(
            stock_code=stock_code,
            quantity=remaining_quantity,
            price=highest_price,
            is_mock=False,
        )

        expected_amount = remaining_quantity * highest_price
        total_expected_amount += expected_amount

        print(
            f"       ✅ 성공! 주문번호: {order_result.get('odno')} "
            f"(예상: {expected_amount:,}원)"
        )
        print("       ✨ 잔액 없이 깔끔하게 완료!")
        success_count += 1

    except Exception as e:
        print(f"       ❌ 전량 매도 실패: {e}")
        _print_error_hint(e)

    print(f"  📊 분할 매도 결과: {success_count}/{len(split_prices) + 1}개 성공")
    if total_expected_amount > 0:
        print(f"     총 예상 수령액: {total_expected_amount:,}원")


def _print_error_hint(e: Exception):
    """에러 메시지에 따른 힌트 출력"""
    error_str = str(e).lower()
    if "opsq0002" in error_str or "mca00124" in error_str:
        print("          💡 서비스 코드 문제일 수 있습니다. API 문서를 확인해주세요.")
    elif "egw00123" in error_str or "egw00121" in error_str:
        print("          💡 토큰 인증 문제일 수 있습니다. 토큰을 갱신합니다.")
    elif "40310000" in error_str:
        print("          💡 주문 수량/가격 오류입니다.")
        print("             - 최소 주문 수량 확인")
        print("             - 가격 단위 확인")


async def place_new_sell_order(
    stock_code: str, quantity: int, sell_price: int
):
    """단일 매도 주문을 넣습니다."""
    try:
        print(f"  📤 매도 주문 실행: {quantity}주")
        print(f"     가격: {sell_price:,}원")

        # 최소 주문 수량 체크
        if quantity < 1:
            print("  ⚠️  수량이 1주 미만이어서 주문 불가능")
            return

        # 매도 주문 실행
        order_result = await kis.sell_korea_stock(
            stock_code=stock_code,
            quantity=quantity,
            price=sell_price,
            is_mock=False,
        )

        expected_amount = quantity * sell_price
        print("  ✅ 매도 주문 성공!")
        print(f"     주문번호: {order_result.get('odno')}")
        print(f"     예상 수령액: {expected_amount:,}원")

    except Exception as e:
        print(f"  ❌ 매도 주문 실패: {e}")
        _print_error_hint(e)


async def main():
    """메인 실행 함수"""
    print("🚀 KIS 국내주식 자동 매도 주문 시스템 시작")
    print("=" * 70)

    # 환경 변수 확인
    from app.core.config import settings

    if not settings.kis_app_key or not settings.kis_app_secret:
        print("❌ KIS API 키가 설정되지 않았습니다.")
        print("   KIS_APP_KEY와 KIS_APP_SECRET 환경 변수를 확인해주세요.")
        return

    print(f"✅ API 키 확인: App Key {settings.kis_app_key[:8]}...")

    # 매도 전략 표시
    strategy_name = "🔀 분할 지정가 매도"
    print(f"📋 매도 전략: {strategy_name}")
    print("   → 분석 가격들로 분할 매도 후, 최고가에서 잔량 전부 매도")
    print("   → 1% 이상 수익 가능한 가격만 사용")
    print("   → 현재가 이상 가격만 사용")

    await process_sell_orders_for_my_stocks()

    print("\n" + "=" * 70)
    print("🏁 매도 주문 프로세스 완료")


if __name__ == "__main__":
    asyncio.run(main())
