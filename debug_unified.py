import asyncio
from app.analysis.service_analyzers import UpbitAnalyzer, YahooAnalyzer, KISAnalyzer


async def main():
    print("=== 통합 분석기 시작 ===\n")
    
    # 1. Upbit 암호화폐 분석
    print("1. Upbit 암호화폐 분석")
    upbit_analyzer = UpbitAnalyzer()
    crypto_coins = ["비트코인", "이더리움", "솔라나"]
    await upbit_analyzer.analyze_coins(crypto_coins)
    
    print("\n" + "="*50 + "\n")
    
    # 2. Yahoo Finance 미국주식 분석
    print("2. Yahoo Finance 미국주식 분석")
    yahoo_analyzer = YahooAnalyzer()
    us_stocks = ["TSLA", "AAPL", "NVDA"]
    await yahoo_analyzer.analyze_stocks(us_stocks)
    
    print("\n" + "="*50 + "\n")
    
    # 3. KIS 국내주식 분석
    print("3. KIS 국내주식 분석")
    kis_analyzer = KISAnalyzer()
    kr_stocks = ["삼성전자", "SK하이닉스", "LG에너지솔루션"]
    await kis_analyzer.analyze_stocks(kr_stocks)
    
    print("\n=== 모든 분석 완료 ===")


if __name__ == "__main__":
    asyncio.run(main())
