from typing import List, Iterable

import pandas as pd
import ta

from .indicators import add_indicators


def build_prompt(
    df: pd.DataFrame,
    ticker: str,
    stock_name: str,
    currency: str = "₩",
    unit_shares: str = "주",
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
        number_fmt="{:,.2f}",
        suffix=currency,
    )  # 통화 단위
    volume_line = format_ma_line(
        df2,
        column="volume",
        windows=(5, 20, 60, 120, 200),
        label_prefix="VMA",
        number_fmt="{:,.0f}",
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
    prompt = f"""
    {stock_name}({ticker}) (관측일 {obs_date})
    {tech_summary}

    [가격 지표]
    {price_line}
    - 현재가 : {today.close:,.2f}{currency}
    - 전일 대비 : {today_diff:+,.2f}{currency} ({today_pct:+.2f}%)
    - RSI(14)   : {rsi14:.1f}

    [거래량 지표]
    {volume_line}
    - 오늘 거래량 : {today.volume:,.0f}{unit_shares}
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
    values = " / ".join(f"{last[f'ma{w}']:,.2f}" for w in avail)
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
