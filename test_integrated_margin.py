#!/usr/bin/env python3
"""
통합증거금 조회 테스트
원화와 외화(USD, JPY 등) 예수금을 한 번에 조회
"""

import asyncio
from app.services.kis import kis


async def main():
    print("=" * 70)
    print("통합증거금 조회 테스트")
    print("=" * 70)

    try:
        # 통합증거금 조회 (원화 + 외화)
        result = await kis.inquire_integrated_margin(is_mock=False)  # 실전투자로 테스트

        print("\n[ 원화 정보 ]")
        print(f"예수금 총액: {result['dnca_tot_amt']:,.0f}원")
        print(f"익일정산금액: {result['nxdy_excc_amt']:,.0f}원")
        print(f"CMA평가금액: {result['cma_evlu_amt']:,.0f}원")
        print(f"전일매수금액: {result['bfdy_buy_amt']:,.0f}원")
        print(f"금일매수금액: {result['thdt_buy_amt']:,.0f}원")

        print("\n[ 통화별 예수금 ]")
        for currency in result['currencies']:
            if currency['frcr_dncl_amt_2'] > 0:  # 잔고가 있는 통화만 출력
                print(f"{currency['crcy_cd']:>5s}: 예수금 {currency['frcr_dncl_amt_2']:>15,.2f}")

        # 주요 통화 정보
        if "krw_balance" in result:
            print(f"\n✓ 원화 예수금: {result['krw_balance']:,.0f}원")

        if "usd_balance" in result:
            print(f"✓ 달러 예수금: ${result['usd_balance']:,.2f}")
        else:
            print("\n✗ 달러 계좌 없음")

        print("\n" + "=" * 70)
        print("조회 완료")
        print("=" * 70)

    except Exception as e:
        print(f"\n오류 발생: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
