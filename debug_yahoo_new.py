import asyncio

from app.analysis.service_analyzers import YahooAnalyzer


async def main():
    # 분석기 초기화
    analyzer = YahooAnalyzer()

    # 분석할 주식 심볼 목록
    stock_symbols = [
        "TSLA",
        "AAPL",
        "NVDA",
        "AMZN",
        "PLTR",
        "QQQM",
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
        "AMD"
    ])
    stock_symbols = list(set(stock_symbols))
    # 주식 분석 실행
    await analyzer.analyze_stocks(stock_symbols)


if __name__ == "__main__":
    asyncio.run(main())
