import asyncio
from app.analysis.service_analyzers import KISAnalyzer


async def main():
    # 분석기 초기화
    analyzer = KISAnalyzer()
    stock_names = [
        "삼양컨텍",
        "한화오션",
        "펩트론",
        "카카오",
        "HMM",
        "지투지바이오",
        "STX엔진",
        "삼성SDI",
        "현대로템",
        "에이피알",
        "알테오젠",
        "LG디스플레이",
        "삼양식품",
        "블루엠텍",
        "기아",
        "현대모비스"]
    # 분석할 국내주식 목록
    stock_names = [
        "삼천리자전거",
        "삼성전자",
        "SK하이닉스",
        "LG에너지솔루션",
        "한국타이어앤테크놀로지",
        "현대차",
        "한국전력",
        "두산에너빌리티",
        "한화에어로스페이스",
        "에코프로머티"
        , "삼성전자우",
          "삼양식품",
    ]
    stock_names = [
        "크래프톤",

        "한미반도체"
        ,
        "한화시스템",
        "NAVER",
        "두산"
        ,
        "삼양컨텍",
        "한화오션",
        "펩트론",
        "카카오",
        "HMM",
        "지투지바이오",
        "STX엔진",
        "삼성SDI",
        "현대로템",
        "에이피알",
        "알테오젠",
        "LG디스플레이",
        "삼양식품",
        "블루엠텍",
        "기아",
        "현대모비스"
    ]

    # 주식 분석 실행
    await analyzer.analyze_stocks(stock_names)


if __name__ == "__main__":
    asyncio.run(main())
