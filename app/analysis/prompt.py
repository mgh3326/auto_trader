import pandas as pd
import ta

from .indicators import add_indicators


def build_prompt(df: pd.DataFrame, ticker: str) -> str:
    df = add_indicators(df).sort_values("date").reset_index(drop=True)
    """
    df : FHKST03010100 로 가져온 100-행 DataFrame
         (컬럼: date • open • high • low • close • volume • value)
    """
    # ─ 1) 지표·통계 계산 ────────────────────────────────
    # 이동평균 & RSI
    latest = df.iloc[-1]

    tech_summary = (
        f"MACD 히스토 {latest.macd_diff:+.2f}, "
        f"RSI14 {latest.rsi14:.1f}, "
        f"BB폭 {(latest.bb_width / latest.close) * 100:.1f}%, "
        f"Stoch %K {latest.stoch_k:.1f}"
    )
    ma5 = df.close.rolling(5).mean().iloc[-1]
    ma20 = df.close.rolling(20).mean().iloc[-1]
    ma60 = df.close.rolling(60).mean().iloc[-1]
    rsi14 = ta.momentum.RSIIndicator(df.close).rsi().iloc[-1]

    # 전일 대비·등락률·거래량 증감
    df["diff"] = df.close.diff()
    df["pct"] = df.close.pct_change() * 100
    df["vol_rate"] = df.volume.pct_change() * 100

    today = df.iloc[-1]
    yday = df.iloc[-2]

    # 최근 10 봉만 미니 테이블로 추림 → 토큰 절약
    recent10 = (
        df.iloc[-11:-1][["date", "close", "volume"]]
        .to_string(index=False, header=False)
    )
    today_diff = today["diff"]
    today_pct = today["pct"]

    # ─ 2) 프롬프트 구성 ────────────────────────────────
    prompt = f"""
    종목코드 {ticker} (관측일 {today.date.date()})
    {tech_summary}

    [가격 지표]
    - 현재가 : {today.close:,.0f}원
    - MA 5/20/60 : {ma5:,.0f} / {ma20:,.0f} / {ma60:,.0f}
    - 전일 대비 : {today_diff:+,.0f}원 ({today_pct:+.2f}%)
    - RSI(14)   : {rsi14:.1f}
    [거래량 지표]
    - 오늘 거래량 : {today.volume:,.0f}주
    - 전일 대비   : {today.vol_rate:+.2f}%

    [최근 10거래일 (날짜·종가·거래량)]
    {recent10}

    [질문]
    위 정보만으로 오늘 매수·관망·매도 중 하나를 선택하고,
    근거를 3줄 이내로 한글로 설명해 주세요.
    """
    return prompt.strip()
