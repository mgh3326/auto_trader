#!/usr/bin/env python3
"""
KIS 해외주식 JSON 분석 실행 예시
debug_yahoo_json.py와 유사한 방식으로 KIS API를 사용하여 해외주식 분석
"""

import asyncio

from app.analysis.service_analyzers import KISAnalyzer
from app.services.kis import kis


async def main():
    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

    try:
        print("=" * 70)
        exchange_list  = ['NASD', 'NYSE', 'AMEX']
        for exchange in exchange_list:
            overseas_stocks = await kis.fetch_my_us_stocks(is_mock=False, exchange=exchange)
            if not overseas_stocks:
                print(f"   보유 중인 {exchange} 증권거래소 주식이 없습니다.\n")
            else:
                print(f"   총 {len(overseas_stocks)}개 종목 보유 중\n")
                for stock in overseas_stocks:
                    stock_symbol = stock.get('ovrs_pdno')
                    await analyzer.analyze_overseas_stock_json(stock_symbol)

    except Exception as e:
        print(f"에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
