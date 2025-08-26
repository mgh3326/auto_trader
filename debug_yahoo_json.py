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
        "QQQM",
            "TSLA",
            "AAPL",
            "NVDA",
            "AMZN",
            "PLTR",
            "QQQ",
            "BRK-B",
            "SPLG",
            "IVV",
            "VOO",
            "AAPL",
            "CONY",
            "CWD",
            "GOOGL",
            "IONQ",
            "TSLL",
            "SOXL",
        ]
        stock_symbols.extend([
            "NVDL",
            "PLTU",
        ])
        stock_symbols.extend([
            "RGTI",
            "UVIX",
            "FIG",
            "LLY",
            "SPOT",
            "INTC"
        ])
        stock_symbols.extend([
            "PLTR",
            "TSLL",
            "QQQM",
            "MSTY",
            "SMCX",
            "BITX",
            "SMR"
        ])
        stock_symbols.extend([
            "ETHU"
        ])
        stock_symbols.extend([
            "ALTS",
            "NFLX",
            "TSM",
            "PLTR",
            "TSLL",
            "QQQM",
        ])
        stock_symbols.extend([
            "PLTR"
        ])
        stock_symbols= ([
            "PTIR"
        ])
        stock_symbols = list(set(stock_symbols))
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
