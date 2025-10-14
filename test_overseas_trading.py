#!/usr/bin/env python3
"""
해외주식 거래 기능 테스트
- 증거금 조회
- 매수가능금액 조회
- 주문 테스트 (모의투자)
"""

import asyncio
from app.services.kis import kis


async def main():
    print("=" * 70)
    print("해외주식 거래 기능 테스트")
    print("=" * 70)

    try:
        # 1. 특정 종목 매수가능금액 조회 (시장가 기준)
        print("\n1. AAPL 매수가능금액 조회 (시장가 기준)")
        print("-" * 70)
        symbol = "AAPL"
        buyable = await kis.inquire_overseas_buyable_amount(
            symbol=symbol,
            exchange_code="NASD",
            price=0.0,  # 시장가 기준
            is_mock=True
        )

        print(f"종목: {symbol}")
        print(f"거래소: {buyable['ovrs_exchg']}")
        print(f"주문가능금액: ${float(buyable['ord_psbl_frcr_amt']):,.2f} {buyable.get('currency', 'USD')}")
        print(f"최대주문수량: {buyable['max_ord_psbl_qty']}주")

        # 2. 지정가 기준 매수가능금액 조회
        print("\n\n2. AAPL 매수가능금액 조회 (지정가 $180 기준)")
        print("-" * 70)
        buyable_limit = await kis.inquire_overseas_buyable_amount(
            symbol=symbol,
            exchange_code="NASD",
            price=180.0,  # 지정가
            is_mock=True
        )

        print(f"종목: {symbol}")
        print(f"주문가격: $180.00")
        print(f"최대주문수량: {buyable_limit['max_ord_psbl_qty']}주")

        # 3. 주문 가능 여부 확인
        print("\n\n3. 주문 시뮬레이션")
        print("-" * 70)
        max_qty = int(buyable['max_ord_psbl_qty']) if buyable['max_ord_psbl_qty'] else 0

        if max_qty > 0:
            print(f"✓ {symbol} 최대 {max_qty}주 매수 가능")
            print(f"  (주문가능금액: ${float(buyable['ord_psbl_frcr_amt']):,.2f})")

            # 실제 주문은 주석 처리 (테스트 시 활성화)
            # order_qty = min(1, max_qty)  # 1주만 테스트
            # print(f"\n  → {order_qty}주 시장가 매수 주문 테스트...")
            # result = await kis.buy_overseas_stock(
            #     symbol=symbol,
            #     exchange_code="NASD",
            #     quantity=order_qty,
            #     price=0,  # 시장가
            #     is_mock=True
            # )
            # print(f"  ✓ 주문완료 - 주문번호: {result['odno']}, 시각: {result['ord_tmd']}")

            print("\n  ※ 실제 주문 테스트는 코드에서 주석을 해제하여 실행하세요")
        else:
            print(f"✗ 매수 불가능 (잔고 부족)")

        print("\n" + "=" * 70)
        print("테스트 완료")
        print("=" * 70)

    except Exception as e:
        print(f"\n오류 발생: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
