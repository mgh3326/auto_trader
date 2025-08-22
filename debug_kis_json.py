#!/usr/bin/env python3
"""
KIS 국내주식 JSON 분석 실행 예시
"""

import asyncio
from app.analysis.service_analyzers import KISAnalyzer


async def main():
    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

    try:
        # 분석할 국내 주식명 목록
        stock_names = [
            "삼양식품",
            "삼천리자전거",
            "삼성전자",
            "SK하이닉스",
            "LG에너지솔루션",
            "한국타이어앤테크놀로지",
            "현대차",
            "한국전력",
            "두산에너빌리티",
            "한화에어로스페이스",
            "에코프로머티",
            "삼성전자우",
        ]
        stock_names = [
            "TIGER 미국나스닥100",
            "TIGER 미국S&P500",
            "RISE 미국나스닥100",
            "RISE 미국S&P500",
            "KODEX 미국나스닥100",
            "TIGER 미국테크TOP10 INDXX",
            "TIGER 미국필라델피아반도체나스닥",
            "SOL 미국S&P500",

        ]
        print(f"분석할 주식 목록: {stock_names}")
        print(f"총 {len(stock_names)}개 주식 분석 시작\n")

        # JSON 형식으로 주식 분석 실행
        await analyzer.analyze_stocks_json(stock_names)

    except Exception as e:
        print(f"에러 발생: {e}")
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
