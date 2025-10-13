#!/usr/bin/env python3
"""
KIS 해외주식 JSON 분석 실행 예시
debug_yahoo_json.py와 유사한 방식으로 KIS API를 사용하여 해외주식 분석
"""

import asyncio
from app.analysis.service_analyzers import KISAnalyzer


async def main():
    # JSON 분석기 초기화
    analyzer = KISAnalyzer()

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
            "PLTR",
            "PTIR"
        ])
        stock_symbols.extend([
            "INTC",
            "AMD",
            "XXRP"
        ])
        stock_symbols.extend([
            "UPSX", "CONL", "CRCA"
        ])

        # 중복 제거
        stock_symbols = list(set(stock_symbols))

        # 테스트용으로 2개만 선택
        stock_symbols = ["NVDA", "DUOL"]

        print(f"분석할 주식 목록: {stock_symbols}")
        print(f"총 {len(stock_symbols)}개 주식 분석 시작\n")

        # JSON 형식으로 해외주식 분석 실행 (KIS API 사용)
        await analyzer.analyze_overseas_stocks_json(stock_symbols)

    except Exception as e:
        print(f"에러 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
