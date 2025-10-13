"""
KIS 보유 주식 조회 테스트 스크립트
Upbit의 fetch_my_coins와 유사한 기능을 테스트합니다.
"""
import asyncio
import pprint

from app.services.kis import kis


async def main():
    print("=== KIS 보유 주식 조회 테스트 ===\n")

    try:
        # 실전투자 계좌 조회
        print("1. 실전투자 계좌 보유 주식:")
        my_stocks = await kis.fetch_my_stocks(is_mock=False)

        if not my_stocks:
            print("   보유 중인 주식이 없습니다.\n")
        else:
            print(f"   총 {len(my_stocks)}개 종목 보유 중\n")
            for stock in my_stocks:
                print(f"   종목코드: {stock.get('pdno')}")
                print(f"   종목명: {stock.get('prdt_name')}")
                print(f"   보유수량: {stock.get('hldg_qty')}주")
                print(f"   매입평균가: {stock.get('pchs_avg_pric')}원")
                print(f"   현재가: {stock.get('prpr')}원")
                print(f"   평가금액: {stock.get('evlu_amt')}원")
                print(f"   평가손익: {stock.get('evlu_pfls_amt')}원 ({stock.get('evlu_pfls_rt')}%)")
                print("-" * 50)

        # 전체 응답 구조 확인 (디버깅용)
        print("\n2. 전체 응답 데이터 구조:")
        pprint.pp(my_stocks)

    except Exception as e:
        print(f"오류 발생: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
