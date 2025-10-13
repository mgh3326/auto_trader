"""
KIS 보유 주식 조회 테스트 스크립트
Upbit의 fetch_my_coins와 유사한 기능을 테스트합니다.
국내주식과 해외주식 모두 조회 가능합니다.
"""
import asyncio
import pprint

from app.services.kis import kis


async def main():
    print("=== KIS 보유 주식 조회 테스트 ===\n")

    try:
        # 1. 국내주식 조회
        print("=" * 70)
        print("1. 국내주식 잔고 조회")
        print("=" * 70)
        domestic_stocks = await kis.fetch_my_stocks(is_mock=False, is_overseas=False)

        if not domestic_stocks:
            print("   보유 중인 국내주식이 없습니다.\n")
        else:
            print(f"   총 {len(domestic_stocks)}개 종목 보유 중\n")
            for stock in domestic_stocks:
                print(f"   종목코드: {stock.get('pdno')}")
                print(f"   종목명: {stock.get('prdt_name')}")
                print(f"   보유수량: {stock.get('hldg_qty')}주")
                print(f"   매입평균가: {float(stock.get('pchs_avg_pric', 0)):,.0f}원")
                print(f"   현재가: {float(stock.get('prpr', 0)):,.0f}원")
                print(f"   평가금액: {float(stock.get('evlu_amt', 0)):,.0f}원")
                print(f"   평가손익: {float(stock.get('evlu_pfls_amt', 0)):,.0f}원 ({stock.get('evlu_pfls_rt')}%)")
                print("-" * 70)

        # 2. 해외주식 조회 (나스닥)
        print("\n" + "=" * 70)
        print("2. 해외주식 잔고 조회 (나스닥 - USD)")
        print("=" * 70)
        overseas_stocks_nasd = await kis.fetch_my_us_stocks(is_mock=False, exchange="NASD")

        if not overseas_stocks_nasd:
            print("   보유 중인 나스닥 주식이 없습니다.\n")
        else:
            print(f"   총 {len(overseas_stocks_nasd)}개 종목 보유 중\n")
            for stock in overseas_stocks_nasd:
                print(f"   종목코드: {stock.get('ovrs_pdno')}")
                print(f"   종목명: {stock.get('ovrs_item_name')}")
                print(f"   보유수량: {stock.get('ovrs_cblc_qty')}주")
                print(f"   외화매입금액: ${float(stock.get('frcr_pchs_amt1', 0)):,.2f}")
                print(f"   외화평가금액: ${float(stock.get('ovrs_stck_evlu_amt', 0)):,.2f}")
                print(f"   외화평가손익: ${float(stock.get('frcr_evlu_pfls_amt', 0)):,.2f} ({stock.get('evlu_pfls_rt')}%)")
                print("-" * 70)

        # 3. 해외주식 조회 (뉴욕증권거래소)
        print("\n" + "=" * 70)
        print("3. 해외주식 잔고 조회 (뉴욕증권거래소 - USD)")
        print("=" * 70)
        overseas_stocks_nyse = await kis.fetch_my_us_stocks(is_mock=False, exchange="NYSE")

        if not overseas_stocks_nyse:
            print("   보유 중인 뉴욕증권거래소 주식이 없습니다.\n")
        else:
            print(f"   총 {len(overseas_stocks_nyse)}개 종목 보유 중\n")
            for stock in overseas_stocks_nyse:
                print(f"   종목코드: {stock.get('ovrs_pdno')}")
                print(f"   종목명: {stock.get('ovrs_item_name')}")
                print(f"   보유수량: {stock.get('ovrs_cblc_qty')}주")
                print(f"   외화매입금액: ${float(stock.get('frcr_pchs_amt1', 0)):,.2f}")
                print(f"   외화평가금액: ${float(stock.get('ovrs_stck_evlu_amt', 0)):,.2f}")
                print(f"   외화평가손익: ${float(stock.get('frcr_evlu_pfls_amt', 0)):,.2f} ({stock.get('evlu_pfls_rt')}%)")
                print("-" * 70)

        # 4. 전체 응답 구조 확인 (디버깅용)
        print("\n" + "=" * 70)
        print("4. 전체 응답 데이터 구조")
        print("=" * 70)
        print("\n국내주식:")
        pprint.pp(domestic_stocks)
        print("\n해외주식 (나스닥):")
        pprint.pp(overseas_stocks_nasd)
        print("\n해외주식 (뉴욕):")
        pprint.pp(overseas_stocks_nyse)

    except Exception as e:
        print(f"오류 발생: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
