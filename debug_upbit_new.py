import asyncio
from app.analysis.service_analyzers import UpbitAnalyzer


async def main():
    # 분석기 초기화
    analyzer = UpbitAnalyzer()
    
    # 분석할 코인 목록
    coin_names = [
        "비트코인",
        "이더리움", 
        "솔라나",
        "엑스알피(리플)",
        "도지코인"
    ]
    
    # 코인 분석 실행
    await analyzer.analyze_coins(coin_names)


if __name__ == "__main__":
    asyncio.run(main())
