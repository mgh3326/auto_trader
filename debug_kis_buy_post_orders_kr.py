#!/usr/bin/env python3
"""
KIS 국내주식 자동 매수 주문 시스템
"""

import asyncio

from app.analysis.service_analyzers import KISAnalyzer
from app.core.db import AsyncSessionLocal
from app.services.kis import kis
from app.services.kr_symbol_universe_service import (
    KRSymbolUniverseLookupError,
    get_kr_symbol_by_name,
)

# ===== 매수 설정 =====
TARGET_STOCKS = [
    "삼성전자",
    "SK하이닉스",
    "NAVER",
]

BUY_AMOUNT_PER_STOCK = 1_000_000  # 종목당 100만원

# =========================


async def _resolve_kr_code(stock_name: str) -> str | None:
    async with AsyncSessionLocal() as db:
        try:
            return await get_kr_symbol_by_name(stock_name, db)
        except KRSymbolUniverseLookupError:
            return None


async def process_buy_orders_for_stocks():
    """설정된 주식 목록에 대해 매수 주문 프로세스를 실행합니다."""

    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

    try:
        # 1. 보유 주식 정보 가져오기
        print("=== 보유 국내주식 조회 ===")
        kr_stocks = await kis.fetch_my_stocks(is_mock=False, is_overseas=False)
        print(f"총 {len(kr_stocks)}개 주식 보유 중")

        # 보유 주식을 종목코드 기준으로 딕셔너리 생성
        holdings_by_code = {}
        for stock in kr_stocks:
            stock_code = stock.get("pdno")
            holdings_by_code[stock_code] = stock

        # 보유 주식 정보 출력
        if holdings_by_code:
            print("\n보유 주식:")
            for stock_code, stock in holdings_by_code.items():
                stock_name = stock.get("prdt_name", "")
                quantity = int(stock.get("hldg_qty", 0))
                avg_price = int(float(stock.get("pchs_avg_pric", 0)))
                print(
                    f"  - {stock_name} ({stock_code}): {quantity}주, 평균 {avg_price:,}원"
                )

        # 2. 미체결 주문 조회 (한 번만)
        print("\n=== 미체결 주문 조회 ===")
        all_open_orders = await kis.inquire_korea_orders(is_mock=False)
        print(f"총 {len(all_open_orders)}개의 미체결 주문 발견")

        # 매수 주문만 카운트
        buy_orders_count = len(
            [o for o in all_open_orders if o.get("sll_buy_dvsn_cd") == "02"]
        )
        print(f"  - 매수 주문: {buy_orders_count}개")
        print(f"  - 매도 주문: {len(all_open_orders) - buy_orders_count}개")

        # 3. 각 타겟 주식에 대해 매수 처리
        print(f"\n=== 타겟 주식 {len(TARGET_STOCKS)}개 매수 처리 ===")
        for stock_name in TARGET_STOCKS:
            # 종목명 → 종목코드 변환
            stock_code = await _resolve_kr_code(stock_name)
            if not stock_code:
                print(f"\n❌ {stock_name}: 종목코드를 찾을 수 없습니다.")
                continue

            # 보유 정보 확인
            holding_info = holdings_by_code.get(stock_code)

            # 단일 주식 매수 처리
            await process_single_stock_buy_orders(
                stock_name, stock_code, holding_info, all_open_orders, analyzer
            )

    except Exception as e:
        print(f"❌ 에러 발생: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await analyzer.close()


async def process_single_stock_buy_orders(
    stock_name: str,
    stock_code: str,
    holding_info: dict | None,
    all_open_orders: list[dict],
    analyzer: KISAnalyzer,
):
    """단일 주식에 대한 매수 주문을 처리합니다."""

    print(f"\n{'=' * 70}")
    print(f"=== {stock_name} ({stock_code}) 매수 처리 시작 ===")

    try:
        # 1. 현재가 조회
        current_price_df = await kis.inquire_price(stock_code)
        current_price = int(float(current_price_df.iloc[0]["close"]))
        print(f"현재가: {current_price:,}원")

        # 2. 보유 여부 및 1% 하락 조건 확인
        should_buy = True
        avg_buy_price = None

        if holding_info:
            avg_buy_price = int(float(holding_info.get("pchs_avg_pric", 0)))
            quantity = int(holding_info.get("hldg_qty", 0))
            print(f"보유 중: {quantity}주, 평균 매수가: {avg_buy_price:,}원")

            # 1% 하락 조건 체크
            threshold_price = int(avg_buy_price * 0.99)
            print(f"매수 기준가 (99%): {threshold_price:,}원")

            if current_price >= threshold_price:
                print("⚠️  매수 조건 미충족: 현재가가 평균 매수가의 99%보다 높습니다.")
                print(f"   현재가 {current_price:,}원 >= 기준가 {threshold_price:,}원")
                should_buy = False
            else:
                drop_rate = ((avg_buy_price - current_price) / avg_buy_price) * 100
                print(f"✅ 매수 조건 충족: 평균 매수가 대비 {drop_rate:.1f}% 하락")
        else:
            print("보유하지 않음: 조건 없이 매수 가능")

        if not should_buy:
            return

        # 3. 기존 매수 주문 취소
        print("\n🔍 기존 매수 주문 확인 및 취소...")
        await cancel_existing_buy_orders(stock_code, all_open_orders, is_mock=False)

        # API 서버 데이터 동기화를 위해 잠시 대기
        print("⏳ API 서버 동기화를 위해 1초 대기...")
        await asyncio.sleep(1)

        # 4. 분석 결과 기반 분할 매수 처리
        await process_buy_with_analysis(
            stock_code,
            stock_name,
            current_price,
            avg_buy_price or current_price,  # 보유하지 않으면 현재가를 기준으로
        )

    except Exception as e:
        print(f"❌ {stock_name} 처리 중 오류: {e}")
        import traceback

        traceback.print_exc()


async def cancel_existing_buy_orders(
    stock_code: str, all_open_orders: list[dict], is_mock: bool = False
):
    """해당 종목의 기존 매수 주문들을 취소합니다."""

    try:
        # 해당 종목의 매수 주문만 필터링
        # sll_buy_dvsn_cd: 01=매도, 02=매수
        buy_orders = [
            order
            for order in all_open_orders
            if order.get("pdno") == stock_code and order.get("sll_buy_dvsn_cd") == "02"
        ]

        if not buy_orders:
            print("  취소할 매수 주문이 없습니다.")
            return

        print(f"  {len(buy_orders)}개 매수 주문 발견")
        for order in buy_orders:
            order_qty = int(order.get("ord_qty", 0))
            order_price = int(float(order.get("ord_unpr", 0)))
            print(f"    - 가격: {order_price:,}원, 수량: {order_qty}주")

        # 주문 취소
        success_count = 0
        for order in buy_orders:
            try:
                order_number = order.get("ord_no")
                order_qty = int(order.get("ord_qty", 0))
                order_price = int(float(order.get("ord_unpr", 0)))

                result = await kis.cancel_korea_order(
                    order_number=order_number,
                    stock_code=stock_code,
                    quantity=order_qty,
                    price=order_price,
                    order_type="buy",
                    is_mock=is_mock,
                )

                print(f"    ✅ 취소 완료: {result.get('odno')}")
                success_count += 1

                # API 호출 제한 방지를 위한 대기
                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"    ❌ 주문 취소 실패: {e}")

        print(f"  ✅ {success_count}/{len(buy_orders)}개 주문 취소 완료")

    except Exception as e:
        print(f"❌ 주문 취소 중 오류: {e}")


async def process_buy_with_analysis(
    stock_code: str, stock_name: str, current_price: int, avg_buy_price: int
):
    """분석 결과를 기반으로 분할 매수 주문을 실행합니다."""

    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    print("\n📊 분석 결과 기반 매수 주문 처리")

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        # 국내주식은 종목코드로 조회
        analysis = await service.get_latest_analysis_by_symbol(stock_code)

        if not analysis:
            print(f"  ⚠️  {stock_name}의 분석 결과가 없습니다.")
            print("  분석 결과 없이는 매수하지 않습니다.")
            return

        # 4개 매수 가격 값 추출
        buy_prices = []

        if analysis.appropriate_buy_min is not None:
            buy_prices.append(
                ("appropriate_buy_min", int(analysis.appropriate_buy_min))
            )
        if analysis.appropriate_buy_max is not None:
            buy_prices.append(
                ("appropriate_buy_max", int(analysis.appropriate_buy_max))
            )
        if analysis.buy_hope_min is not None:
            buy_prices.append(("buy_hope_min", int(analysis.buy_hope_min)))
        if analysis.buy_hope_max is not None:
            buy_prices.append(("buy_hope_max", int(analysis.buy_hope_max)))

        # 범위 정보 출력
        if (
            analysis.appropriate_buy_min is not None
            and analysis.appropriate_buy_max is not None
        ):
            print(
                f"  적절한 매수 범위: {int(analysis.appropriate_buy_min):,}원 ~ {int(analysis.appropriate_buy_max):,}원"
            )
        if analysis.buy_hope_min is not None and analysis.buy_hope_max is not None:
            print(
                f"  희망 매수 범위: {int(analysis.buy_hope_min):,}원 ~ {int(analysis.buy_hope_max):,}원"
            )

        if not buy_prices:
            print("  ❌ 분석 결과에 매수 가격 정보가 없습니다.")
            return

        # 조건에 맞는 가격들 필터링 (현재가보다 낮아야 함)
        # 보유 주식의 경우 이미 1% 하락 조건을 통과했으므로 추가 필터링 불필요
        valid_prices = []
        for price_name, price_value in buy_prices:
            is_below_current = price_value <= current_price

            if is_below_current:
                valid_prices.append((price_name, price_value))
                current_diff = (current_price - price_value) / current_price * 100
                print(
                    f"  ✅ {price_name}: {price_value:,}원 (현재가보다 {current_diff:.1f}% 낮음)"
                )
            else:
                print(f"  ❌ {price_name}: {price_value:,}원 (현재가보다 높음)")

        if not valid_prices:
            print("  ⚠️  조건에 맞는 매수 가격이 없습니다. (현재가보다 낮아야 함)")
            return

        # 가격 오름차순 정렬 (낮은 가격부터)
        valid_prices.sort(key=lambda x: x[1])

        print(
            f"\n🎯 총 {len(valid_prices)}개 가격에서 {BUY_AMOUNT_PER_STOCK:,}원 분할 매수:"
        )

        # 각 가격별 매수 금액 계산
        amount_per_price = BUY_AMOUNT_PER_STOCK // len(valid_prices)

        print(f"  가격당 매수 금액: {amount_per_price:,}원")

        # 각 가격별로 매수 주문
        success_count = 0
        total_orders = len(valid_prices)
        total_amount = 0

        for i, (price_name, buy_price) in enumerate(valid_prices, 1):
            print(f"\n  [{i}/{total_orders}] {price_name} - {buy_price:,}원")

            result = await place_single_buy_order(
                stock_code, amount_per_price, buy_price, price_name
            )

            if result:
                success_count += 1
                total_amount += amount_per_price

            # 주문 간 약간의 지연 (API 제한 고려)
            if i < total_orders:
                await asyncio.sleep(0.2)

        print(f"\n📈 매수 주문 완료: {success_count}/{total_orders}개 성공")
        if total_amount > 0:
            print(f"   총 주문 금액: {total_amount:,}원")


async def place_single_buy_order(
    stock_code: str, amount: int, buy_price: int, price_name: str
):
    """단일 가격으로 매수 주문을 실행합니다."""

    try:
        # 매수 수량 계산 (수수료는 고려하지 않음, KIS는 매수 시 수수료 별도)
        quantity = amount // buy_price

        # 최소 1주는 매수해야 함
        if quantity < 1:
            print(
                f"    ⚠️  매수 가능 수량이 1주 미만입니다 (금액: {amount:,}원, 가격: {buy_price:,}원)"
            )
            return None

        actual_amount = quantity * buy_price

        print(f"    💰 {amount:,}원 지정가 매수 주문")
        print(f"      - 주문 가격: {buy_price:,}원")
        print(f"      - 주문 수량: {quantity}주")
        print(f"      - 실제 금액: {actual_amount:,}원")

        # 지정가 매수 주문
        order_result = await kis.order_korea_stock(
            stock_code=stock_code,
            order_type="buy",
            quantity=quantity,
            price=buy_price,
            is_mock=False,
        )

        print("      ✅ 주문 성공:")
        print(f"        - 주문 ID: {order_result.get('odno')}")
        print(f"        - 주문 시간: {order_result.get('ord_tmd')}")

        return order_result

    except Exception as e:
        print(f"    ❌ {price_name} 매수 주문 실패: {e}")
        _print_error_hint(e)
        return None


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


async def main():
    """메인 실행 함수"""
    print("🚀 KIS 국내주식 자동 매수 주문 시스템 시작")
    print("=" * 70)

    # 환경 변수 확인
    from app.core.config import settings

    if not settings.kis_app_key or not settings.kis_app_secret:
        print("❌ KIS API 키가 설정되지 않았습니다.")
        print("   KIS_APP_KEY와 KIS_APP_SECRET 환경 변수를 확인해주세요.")
        return

    print(f"✅ API 키 확인: App Key {settings.kis_app_key[:8]}...")

    # 타겟 주식 정보 출력
    print(f"\n📋 타겟 주식: {len(TARGET_STOCKS)}개")
    for stock_name in TARGET_STOCKS:
        stock_code = await _resolve_kr_code(stock_name) or "???"
        print(f"  - {stock_name} ({stock_code})")

    print(f"\n💰 종목당 매수 금액: {BUY_AMOUNT_PER_STOCK:,}원")
    print("📊 전략: 분석 결과의 매수 가격들로 분할 매수")
    print("   → 보유 주식: 현재가가 평균 매수가보다 1% 낮을 때만 매수")
    print("   → 미보유 주식: 조건 없이 매수")
    print("   → 현재가보다 낮은 가격에만 주문")

    await process_buy_orders_for_stocks()

    print("\n" + "=" * 70)
    print("🏁 매수 주문 프로세스 완료")


if __name__ == "__main__":
    asyncio.run(main())
