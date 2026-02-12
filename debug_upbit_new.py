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
    coin_names = [
        "웨이브",
        "서싱트",
        "에테나",
        "맨틀",
        "펏지펭귄",
        "가스",
        "오피셜트럼프",
        "베라체인",
        "사이버,"
        "도지코인",
        "아비트럼",
        "세이",
        "온도파이낸스",
        "알파쿼크"
        "에이피아이쓰리",
        "체인링크",
        "하이퍼레인",
        "아테나",
        "네오",
        "펫지펭귄",
        "맨틀",
        "퀀텀",
        "크레딧코인",
        "토카막네트워크",
        "버추얼프로토콜"
        "사하라에이아이",
        "시바이누",
        "헤데라",
        "이더리움클래식"
    ]
    coin_names = [
        "웨이브"
    ]
    # 코인 분석 실행
    await analyzer.analyze_coins(coin_names)


if __name__ == "__main__":
    asyncio.run(main())
