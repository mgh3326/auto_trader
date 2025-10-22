#!/usr/bin/env python3
"""
Upbit 암호화폐 분석 블로그용 간단 예시
DB 연결 없이 프롬프트 생성과 AI 분석 실행
"""

import asyncio
from google import genai

from app.services import upbit
from data.coins_info import upbit_pairs
from app.analysis.analyzer import DataProcessor


def add_indicators(df):
    """기술적 지표 추가"""
    import ta

    df = df.copy()

    # 이동평균선 (5, 20, 60, 120, 200일)
    for window in [5, 20, 60, 120, 200]:
        df[f"ma{window}"] = df["close"].rolling(window=window).mean()

    # RSI (14일)
    df["rsi14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

    # MACD
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()

    # 볼린저 밴드
    bb = ta.volatility.BollingerBands(df["close"])
    df["bb_high"] = bb.bollinger_hband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()

    # 스토캐스틱
    stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"])
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    return df


def build_prompt(df, ticker: str, coin_name: str, fundamental_info: dict = None) -> str:
    """
    AI 분석을 위한 프롬프트 생성
    """
    # 1. 기술적 지표 계산
    df = add_indicators(df).sort_values("date").reset_index(drop=True)

    latest = df.iloc[-1]     # 오늘
    yesterday = df.iloc[-2]  # 어제

    # 2. 전일 대비 계산
    price_diff = latest["close"] - yesterday["close"]
    price_pct = (price_diff / yesterday["close"]) * 100
    vol_pct = ((latest["volume"] - yesterday["volume"]) / yesterday["volume"]) * 100

    # 3. 기술적 지표 요약 (한 줄로)
    tech_summary = (
        f"MACD 히스토 {latest.macd_diff:+.2f}, "
        f"RSI14 {latest.rsi14:.1f}, "
        f"BB폭 {(latest.bb_width / latest.close) * 100:.1f}%, "
        f"Stoch %K {latest.stoch_k:.1f}"
    )

    # 4. 이동평균선 정리
    ma_5 = latest["ma5"]
    ma_20 = latest["ma20"]
    ma_60 = latest["ma60"]
    ma_120 = latest["ma120"]
    ma_200 = latest["ma200"]

    # 5. 기본 정보 섹션
    info_section = ""
    if fundamental_info:
        info_section = "\n[기본 정보]\n"
        for key, value in fundamental_info.items():
            if value:
                info_section += f"- {key}: {value}\n"

    # 6. 최근 10거래일 데이터
    recent_10 = df.iloc[-11:-1][["date", "close", "volume"]].to_string(
        index=False, header=False
    )

    # 7. 프롬프트 조합
    prompt = f"""
{coin_name}({ticker}) (관측일 {latest.date})
{tech_summary}{info_section}

[가격 지표]
- MA 5/20/60/120/200 : {ma_5:,.0f} / {ma_20:,.0f} / {ma_60:,.0f} / {ma_120:,.0f} / {ma_200:,.0f} ₩
- 현재가 : {latest.close:,.0f}₩
- 전일 대비 : {price_diff:+,.0f}₩ ({price_pct:+.2f}%)
- RSI(14) : {latest.rsi14:.1f}

[거래량 지표]
- 오늘 거래량 : {latest.volume:,.0f}
- 전일 대비 : {vol_pct:+.2f}%

[최근 10거래일 (날짜·종가·거래량)]
{recent_10}

[질문]
위 정보만으로 오늘 매수·관망·매도 중 하나를 선택하고,
근거를 3줄 이내로 한글로 설명해 주세요.
적절한 매수가, 매도가, 매수 희망가, 매도 목표가도 제시해 주세요.
"""
    return prompt.strip()


async def main():
    # Upbit 상수 초기화
    await upbit_pairs.prime_upbit_constants()

    # 비트코인 데이터 수집
    coin_name = "비트코인"
    ticker = "KRW-BTC"

    print(f"1단계: {coin_name} 데이터 수집 중...")

    # 1. 데이터 수집
    df_historical = await upbit.fetch_ohlcv(ticker, days=200)
    df_current = await upbit.fetch_price(ticker)
    fundamental_info = await upbit.fetch_fundamental_info(ticker)

    print(f"  - 일봉 데이터: {len(df_historical)}개")
    print(f"  - 현재가: {df_current.iloc[0]['close']:,.0f}원")

    # 2. 데이터 병합
    df_merged = DataProcessor.merge_historical_and_current(df_historical, df_current)
    print(f"  - 병합 완료: {len(df_merged)}개 데이터")

    # 3. 프롬프트 생성
    print(f"\n2단계: AI 분석용 프롬프트 생성 중...")
    prompt = build_prompt(df_merged, ticker, coin_name, fundamental_info)

    print("\n생성된 프롬프트:")
    print("=" * 80)
    print(prompt)
    print("=" * 80)

    # 4. AI 분석 (Google Gemini)
    print("\n3단계: Gemini AI에 분석 요청 중...")

    # GOOGLE_API_KEY 환경 변수에서 자동으로 가져옴
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    print("\nGemini AI 분석 결과:")
    print("=" * 80)
    print(response.text)
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
