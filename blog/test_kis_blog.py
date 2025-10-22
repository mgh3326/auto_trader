"""
KIS API로 삼성전자 데이터를 수집하고 프롬프트를 생성한 후 Gemini에 분석 요청
"""
import asyncio
from app.analysis.service_analyzers import KISAnalyzer


async def main():
    print("=" * 80)
    print("KIS API로 삼성전자 분석 시작")
    print("=" * 80)

    analyzer = KISAnalyzer()

    # 삼성전자 분석 (JSON 형식)
    await analyzer.analyze_stock_json("삼성전자")

    print("\n" + "=" * 80)
    print("분석 완료!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
