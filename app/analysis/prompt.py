from typing import Iterable, List, Optional

import pandas as pd
import ta

from .indicators import add_indicators


def format_decimal(value: float, currency: str = "₩") -> str:
    """
    값의 크기에 따라 적절한 소수점 자릿수를 결정하여 포맷팅
    
    Args:
        value: 포맷팅할 값
        currency: 통화 단위 (₩, $ 등)
    
    Returns:
        포맷팅된 문자열
    """
    if value == 0:
        return "0"
    
    abs_value = abs(value)
    
    # 한국 원화 (₩) 기준
    if currency == "₩":
        if abs_value >= 1000000:  # 100만원 이상
            return f"{value:,.0f}"
        elif abs_value >= 10000:   # 1만원 이상
            return f"{value:,.1f}"
        elif abs_value >= 1000:    # 1천원 이상
            return f"{value:,.2f}"
        elif abs_value >= 100:     # 100원 이상
            return f"{value:,.2f}"
        else:                       # 100원 미만
            return f"{value:,.2f}"
    
    # 미국 달러 ($) 기준
    elif currency == "$":
        if abs_value >= 1000:      # $1,000 이상
            return f"{value:,.2f}"
        elif abs_value >= 100:     # $100 이상
            return f"{value:,.2f}"
        elif abs_value >= 10:      # $10 이상
            return f"{value:,.2f}"
        else:                       # $10 미만
            return f"{value:,.3f}"
    
    # 암호화폐 등 기타 통화 (기본값)
    else:
        if abs_value >= 1000:      # 1000 이상
            return f"{value:,.2f}"
        elif abs_value >= 100:     # 100 이상
            return f"{value:,.3f}"
        elif abs_value >= 10:      # 10 이상
            return f"{value:,.4f}"
        elif abs_value >= 1:       # 1 이상
            return f"{value:,.5f}"
        elif abs_value >= 0.1:     # 0.1 이상
            return f"{value:,.6f}"
        elif abs_value >= 0.01:    # 0.01 이상
            return f"{value:,.7f}"
        else:                       # 0.01 미만
            return f"{value:,.8f}"


def format_quantity(quantity: float, unit_shares: str = "개") -> str:
    """
    수량을 적절한 소수점 자릿수로 포맷팅
    
    Args:
        quantity: 수량
        unit_shares: 단위 (개, 주 등)
    
    Returns:
        포맷팅된 문자열
    """
    if quantity == 0:
        return "0"
    
    abs_quantity = abs(quantity)
    
    # 주식의 경우 (보통 정수 단위)
    if unit_shares == "주":
        if abs_quantity >= 1000:   # 1000주 이상
            return f"{quantity:,.0f}"
        elif abs_quantity >= 100:  # 100주 이상
            return f"{quantity:,.0f}"
        else:                       # 100주 미만
            return f"{quantity:,.0f}"
    
    # 암호화폐의 경우 (소수점 포함)
    elif unit_shares == "개":
        if abs_quantity >= 1000:   # 1000개 이상
            return f"{quantity:,.2f}"
        elif abs_quantity >= 100:  # 100개 이상
            return f"{quantity:,.3f}"
        elif abs_quantity >= 10:   # 10개 이상
            return f"{quantity:,.4f}"
        elif abs_quantity >= 1:    # 1개 이상
            return f"{quantity:,.5f}"
        elif abs_quantity >= 0.1:  # 0.1개 이상
            return f"{quantity:,.6f}"
        elif abs_quantity >= 0.01: # 0.01개 이상
            return f"{quantity:,.7f}"
        else:                       # 0.01개 미만
            return f"{quantity:,.8f}"
    
    # 기타 단위
    else:
        if abs_quantity >= 1000:
            return f"{quantity:,.2f}"
        elif abs_quantity >= 100:
            return f"{quantity:,.3f}"
        elif abs_quantity >= 10:
            return f"{quantity:,.4f}"
        elif abs_quantity >= 1:
            return f"{quantity:,.5f}"
        else:
            return f"{quantity:,.6f}"


def build_prompt(
    df: pd.DataFrame,
    ticker: str,
    stock_name: str,
    currency: str = "₩",
    unit_shares: str = "주",
    fundamental_info: Optional[dict] = None,
    position_info: Optional[dict] = None,
    minute_candles: Optional[dict] = None,
    news_info: Optional[dict] = None,
) -> str:
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

    # === 사용 예시 ===

    # 1) MA 컬럼 추가 (가격 + 거래량)
    # df: 최소한 ['close', 'volume']를 포함
    df2 = add_ma_multi(df, columns=("close", "volume"), windows=(5, 20, 60, 120, 200))

    # 2) 프롬프트용 라인 만들기
    price_line = format_ma_line(
        df2,
        column="close",
        windows=(5, 20, 60, 120, 200),
        label_prefix="MA",
        suffix=currency,
    )  # 통화 단위
    volume_line = format_ma_line(
        df2,
        column="volume",
        windows=(5, 20, 60, 120, 200),
        label_prefix="VMA",
        suffix="vol",
    )  # 개수/주수 등 단위

    df = add_ma(df, windows=(5, 20, 60, 120, 200))
    ma_line = format_ma_line(df, currency)
    rsi14 = ta.momentum.RSIIndicator(df.close).rsi().iloc[-1]

    # 전일 대비·등락률·거래량 증감
    df["diff"] = df.close.diff()
    df["pct"] = df.close.pct_change() * 100
    df["vol_rate"] = df.volume.pct_change() * 100

    today = df.iloc[-1]
    yday = df.iloc[-2]

    # 최근 10 봉만 미니 테이블로 추림 → 토큰 절약
    recent10 = df.iloc[-11:-1][["date", "close", "volume"]].to_string(
        index=False, header=False
    )
    today_diff = today["diff"]
    today_pct = today["pct"]
    obs_date = today["date"]
    if hasattr(obs_date, "date"):  # Timestamp → date 로 변환
        obs_date = obs_date.date()

    # ─ 2) 프롬프트 구성 ────────────────────────────────
    
    # 기본 정보 섹션 구성
    fundamental_section = ""
    if fundamental_info:
        fundamental_section = "\n[기본 정보]\n"
        for key, value in fundamental_info.items():
            if value is not None and value != "":
                # 숫자 형식인 경우 천 단위 구분자 추가
                if isinstance(value, (int, float)):
                    if isinstance(value, int):
                        formatted_value = f"{value:,}"
                    else:
                        formatted_value = f"{value:,.2f}"
                else:
                    formatted_value = str(value)
                fundamental_section += f"- {key}: {formatted_value}\n"
    
    # 보유 자산 정보 섹션 구성
    position_section = ""
    if position_info:
        position_section = "\n[보유 자산 정보]\n"
        # 보유 수량
        if position_info.get("quantity"):
            quantity = float(position_info["quantity"])
            formatted_quantity = format_quantity(quantity, unit_shares)
            position_section += f"- 보유 수량: {formatted_quantity}{unit_shares}\n"
        
        # 평균 매수가
        if position_info.get("avg_price"):
            avg_price = float(position_info["avg_price"])
            formatted_avg_price = format_decimal(avg_price, currency)
            position_section += f"- 평균 매수가: {formatted_avg_price}{currency}\n"
        
        # 총 평가 금액
        if position_info.get("total_value"):
            total_value = float(position_info["total_value"])
            formatted_total_value = format_decimal(total_value, currency)
            position_section += f"- 총 평가 금액: {formatted_total_value}{currency}\n"
        
        # 거래 중인 수량 (잠긴 수량)
        if position_info.get("locked_quantity") and float(position_info["locked_quantity"]) > 0:
            locked = float(position_info["locked_quantity"])
            formatted_locked = format_quantity(locked, unit_shares)
            position_section += f"- 거래 중인 수량: {formatted_locked}{unit_shares}\n"
    
    # 뉴스 정보 섹션 구성
    news_section = ""
    if news_info and news_info.get("summary"):
        news_section = f"\n{news_info['summary']}\n"

    # 분봉 캔들 정보 섹션 구성
    minute_candles_section = ""
    if minute_candles:
        minute_candles_section = "\n[단기(분) 캔들 정보]\n"
        
        # 60분 캔들 (최근 12개)
        if "60min" in minute_candles and not minute_candles["60min"].empty:
            df_60min = minute_candles["60min"]
            recent_60min = df_60min.tail(12)
            candles_60min = []
            for _, row in recent_60min.iterrows():
                time_str = row["time"].strftime("%H:%M") if hasattr(row["time"], "strftime") else str(row["time"])
                close_str = format_decimal(row["close"], currency)
                volume_str = format_quantity(row["volume"], unit_shares)
                candles_60min.append(f"{time_str} {close_str} {volume_str}")
            minute_candles_section += f"- 60분 캔들 (최근 {len(recent_60min)}개, 시간·종가·거래량):\n  ({', '.join(candles_60min)})\n"
        
        # 5분 캔들 (최근 12개)
        if "5min" in minute_candles and not minute_candles["5min"].empty:
            df_5min = minute_candles["5min"]
            recent_5min = df_5min.tail(12)
            candles_5min = []
            for _, row in recent_5min.iterrows():
                time_str = row["time"].strftime("%H:%M") if hasattr(row["time"], "strftime") else str(row["time"])
                close_str = format_decimal(row["close"], currency)
                volume_str = format_quantity(row["volume"], unit_shares)
                candles_5min.append(f"{time_str} {close_str} {volume_str}")
            minute_candles_section += f"- 5분 캔들 (최근 {len(recent_5min)}개, 시간·종가·거래량):\n  ({', '.join(candles_5min)})\n"
        
        # 1분 캔들 (최근 10개)
        if "1min" in minute_candles and not minute_candles["1min"].empty:
            df_1min = minute_candles["1min"]
            recent_1min = df_1min.tail(10)
            candles_1min = []
            for _, row in recent_1min.iterrows():
                time_str = row["time"].strftime("%H:%M:%S") if hasattr(row["time"], "strftime") else str(row["time"])
                close_str = format_decimal(row["close"], currency)
                volume_str = format_quantity(row["volume"], unit_shares)
                candles_1min.append(f"{time_str} {close_str} {volume_str}")
            minute_candles_section += f"- 1분 캔들 (최근 {len(recent_1min)}개, 시간·종가·거래량):\n  ({', '.join(candles_1min)})\n"
    
    prompt = f"""
    {stock_name}({ticker}) (관측일 {obs_date})
    {tech_summary}{fundamental_section}{position_section}{news_section}{minute_candles_section}

    [가격 지표]
    {price_line}
    - 현재가 : {format_decimal(today.close, currency)}{currency}
    - 전일 대비 : {format_decimal(today_diff, currency)}{currency} ({today_pct:+.2f}%)
    - RSI(14)   : {rsi14:.1f}

    [거래량 지표]
    {volume_line}
    - 오늘 거래량 : {format_quantity(today.volume, unit_shares)}{unit_shares}
    - 전일 대비   : {today.vol_rate:+.2f}%

    [최근 10거래일 (날짜·종가·거래량)]
    {recent10}

    [질문]
    위 정보만으로 오늘 매수·관망·매도 중 하나를 선택하고,
    근거를 3줄 이내로 한글로 설명해 주세요.
    적절한 매수,매도 가격도 알려줘
    매수 희망가, 매도 목표가도 부탁해
    """
    return prompt.strip()


def add_ma(df: pd.DataFrame, windows=(5, 20, 60, 120, 200)) -> pd.DataFrame:
    df = df.copy()
    for w in windows:
        df[f"ma{w}"] = df["close"].rolling(window=w, min_periods=w).mean()
    return df


def format_ma_line(df: pd.DataFrame, currency, windows=(5, 20, 60, 120, 200)) -> str:
    """마지막 행 기준으로 값이 있는 MA만 골라 'MA 5/20/... : v1 / v2 / ...' 형태로 반환"""
    last = df.iloc[-1]
    avail = [w for w in windows if not pd.isna(last[f"ma{w}"])]
    if not avail:
        return "- MA : 자료 부족"
    labels = "/".join(str(w) for w in avail)
    values = " / ".join(format_decimal(last[f'ma{w}'], currency) for w in avail)
    return f"- MA {labels} : {values} {currency}"


def add_ma_multi(
    df: pd.DataFrame,
    columns: Iterable[str] = ("close", "volume"),
    windows: Iterable[int] = (5, 20, 60, 120, 200),
    name_pattern: str = "{col}_ma{w}",
) -> pd.DataFrame:
    """
    지정한 컬럼들에 대해 동일한 윈도우로 단순이동평균(SMA)을 추가.
    결과 컬럼명 예: close_ma5, close_ma20, volume_ma5, volume_ma20 ...
    """
    out = df.copy()
    for col in columns:
        if col not in out.columns:
            # 컬럼이 없는 경우 스킵(프롬프트에서 graceful degrade)
            continue
        for w in windows:
            out[name_pattern.format(col=col, w=w)] = (
                out[col].rolling(window=w, min_periods=w).mean()
            )
    return out


def format_ma_line(
    df: pd.DataFrame,
    column: str = "close",
    windows: Iterable[int] = (5, 20, 60, 120, 200),
    label_prefix: str = "MA",
    number_fmt: str = "{:,.2f}",
    suffix: str = "",
) -> str:
    """
    마지막 행 기준으로 값이 있는 MA만 골라
    '- MA 5/20/... : v1 / v2 / ... <suffix>' 형태로 문자열 생성.

    column에 'close'면 'close_ma{w}'를, 'volume'이면 'volume_ma{w}'를 참조.
    label_prefix로 'MA' / 'VMA' 등 지정.
    """
    if df.empty:
        return f"- {label_prefix} : 자료 부족"

    last = df.iloc[-1]
    keys = [f"{column}_ma{w}" for w in windows]
    avail = [
        w
        for w, k in zip(windows, keys)
        if k in df.columns and pd.notna(last.get(k, pd.NA))
    ]
    if not avail:
        return f"- {label_prefix} : 자료 부족"

    labels = "/".join(str(w) for w in avail)
    values: List[str] = []
    for w in avail:
        k = f"{column}_ma{w}"
        v = last[k]
        try:
            values.append(number_fmt.format(float(v)))
        except Exception:
            values.append(str(v))

    tail = f" {suffix}".rstrip()
    return f"- {label_prefix} {labels} : {' / '.join(values)}{(' ' + suffix) if suffix else ''}"


def build_json_prompt(
    df: pd.DataFrame,
    ticker: str,
    stock_name: str,
    currency: str = "₩",
    unit_shares: str = "주",
    fundamental_info: Optional[dict] = None,
    position_info: Optional[dict] = None,
    minute_candles: Optional[dict] = None,
    news_info: Optional[dict] = None,
) -> str:
    """
    JSON 형식의 응답을 받기 위한 프롬프트를 생성합니다.
    """
    # 기본 프롬프트 생성
    original_prompt = build_prompt(
        df, ticker, stock_name, currency, unit_shares,
        fundamental_info, position_info, minute_candles, news_info
    )
    
    # JSON 형식 프롬프트로 변환
    json_prompt = f"""
{original_prompt}

당신은 전문 주식 분석가입니다. 위 정보를 바탕으로 투자 결정을 내려주세요.

**가격 용어 정의:**
- **적절한 매수 범위**: 현재 시점에서 매수하기에 적정한 가격 범위 (현재가 기준)
- **적절한 매도 범위**: 보유중일 때 매도하기에 적정한 가격 범위 (단기 목표)
- **매수 희망 범위**: 조금 더 저렴하게 사고 싶은 이상적인 매수 가격 범위 (지정가 주문용)
- **매도 목표 범위**: 최종적으로 도달하기를 기대하는 매도 가격 범위 (장기 목표)

**중요**: 매수 희망가 ≤ 적절한 매수가, 적절한 매도가 ≤ 매도 목표가 관계를 유지하세요.

반드시 아래 JSON 형식으로만 답변하세요:

{{
    "decision": "매수/관망/매도 중 하나",
    "reasons": [
        "근거1 (기술적 분석 관점)",
        "근거2 (거래량 또는 모멘텀 관점)",
        "근거3 (위험도 또는 타이밍 관점)"
    ],
    "price_analysis": {{
        "appropriate_buy_range": {{"min": 숫자, "max": 숫자}},
        "appropriate_sell_range": {{"min": 숫자, "max": 숫자}},
        "buy_hope_range": {{"min": 숫자, "max": 숫자}},
        "sell_target_range": {{"min": 숫자, "max": 숫자}}
    }},
    "detailed_text": "**매수**\\n\\n**근거:**\\n1. 근거1\\n2. 근거2\\n3. 근거3\\n\\n**가격 제안:**\\n* **적절한 매수 가격:** X원 ~ Y원\\n* **적절한 매도 가격:** X원 ~ Y원\\n* **매수 희망가:** X원 ~ Y원\\n* **매도 목표가:** X원 ~ Y원",
    "confidence": 숫자0부터100
}}

다른 설명 없이 오직 JSON만 출력하세요.
"""
    return json_prompt.strip()
