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
        "PLTR"
    ]
    
    # 주식 분석 실행
    await analyzer.analyze_stocks(stock_symbols)


if __name__ == "__main__":
    asyncio.run(main())
