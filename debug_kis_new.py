import asyncio
from app.analysis.service_analyzers import KISAnalyzer


async def main():
    # 분석기 초기화
    analyzer = KISAnalyzer()
    
    # 분석할 국내주식 목록
    stock_names = [
        "삼성전자",
        "SK하이닉스",
        "LG에너지솔루션",
        "한국타이어앤테크놀로지",
        "현대차"
    ]
    
    # 주식 분석 실행
    await analyzer.analyze_stocks(stock_names)


if __name__ == "__main__":
    asyncio.run(main())
