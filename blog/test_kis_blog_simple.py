"""
KIS API로 삼성전자 데이터를 수집하고 프롬프트를 생성한 후 Gemini에 분석 요청 (DB 저장 없이)
"""
import asyncio
from app.services import kis
from app.analysis.analyzer import DataProcessor
from app.analysis.prompt import build_prompt
from google import genai
from app.core.config import settings


async def main():
    print("=" * 80)
    print("KIS API로 삼성전자 분석 시작")
    print("=" * 80)

    stock_name = "삼성전자"
    stock_code = "005930"

    print(f"\n1단계: {stock_name}({stock_code}) 데이터 수집 중...")

    # 1. 데이터 수집
    df_historical = await kis.kis.inquire_daily_itemchartprice(stock_code)
    df_current = await kis.kis.inquire_price(stock_code)
    fundamental_info = await kis.kis.fetch_fundamental_info(stock_code)

    print(f"  - 일봉 데이터: {len(df_historical)}개")
    print(f"  - 현재가: {df_current.iloc[0]['close']:,.0f}원")

    # 2. 데이터 병합
    df_merged = DataProcessor.merge_historical_and_current(df_historical, df_current)
    print(f"  - 병합 완료: {len(df_merged)}개 데이터")

    # 3. 프롬프트 생성
    print("\n2단계: AI 분석용 프롬프트 생성 중...")
    prompt = build_prompt(
        df=df_merged,
        ticker=stock_code,
        stock_name=stock_name,
        currency="₩",
        unit_shares="주",
        fundamental_info=fundamental_info,
    )

    print("\n" + "=" * 80)
    print("생성된 프롬프트:")
    print("=" * 80)
    print(prompt)
    print("=" * 80)

    # 4. Gemini 분석
    print("\n3단계: Google Gemini AI에 분석 요청 중...")
    client = genai.Client(api_key=settings.get_random_key())

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        print("\n" + "=" * 80)
        print("Gemini AI 분석 결과:")
        print("=" * 80)
        print(response.text)
        print("=" * 80)

    except Exception as e:
        print(f"Gemini 분석 실패: {e}")

    print("\n분석 완료!")


if __name__ == "__main__":
    asyncio.run(main())
