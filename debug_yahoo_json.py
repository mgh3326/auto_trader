#!/usr/bin/env python3
"""
Yahoo Finance JSON 분석 실행 예시
"""

import asyncio
from app.analysis.service_analyzers import YahooAnalyzer


async def main():
    # JSON 분석기 초기화
    analyzer = YahooAnalyzer()
    
    try:
        # 분석할 미국 주식 심볼 목록
        stock_symbols = [
            "AAPL",   # 애플
            "MSFT",   # 마이크로소프트
            "GOOGL",  # 알파벳 (구글)
            "AMZN",   # 아마존
            "TSLA",   # 테슬라
            "NVDA",   # 엔비디아
            "META",   # 메타
            "NFLX",   # 넷플릭스
        ]
        
        print(f"분석할 주식 목록: {stock_symbols}")
        print(f"총 {len(stock_symbols)}개 주식 분석 시작\n")
        
        # JSON 형식으로 주식 분석 실행
        await analyzer.analyze_stocks_json(stock_symbols)
        
    except Exception as e:
        print(f"에러 발생: {e}")
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
