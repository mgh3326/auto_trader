from __future__ import annotations

from typing import Any

__all__ = [
    "build_rsi_card",
    "build_stoch_rsi_card",
    "build_macd_card",
    "build_bollinger_card",
    "build_ema_card",
    "build_sma_card",
]


def build_rsi_card(rsi_14: Any) -> dict[str, str] | None:
    if not isinstance(rsi_14, (int, float)):
        return None
    if rsi_14 < 30:
        tone, meaning = "oversold", "과매도"
    elif rsi_14 > 70:
        tone, meaning = "overbought", "과매수"
    else:
        tone, meaning = "neutral", "중립"
    return {
        "label": "RSI(14)",
        "value": f"{rsi_14:.1f}",
        "tone": tone,
        "description": meaning,
    }


def build_stoch_rsi_card(k: Any, d: Any) -> dict[str, str] | None:
    if not isinstance(k, (int, float)) or not isinstance(d, (int, float)):
        return None
    if k < 20 and d < 20:
        description, tone = "과매도 구간", "oversold"
    elif k > 80 and d > 80:
        description, tone = "과매수 구간", "overbought"
    else:
        description, tone = "중립 구간", "neutral"
    return {
        "label": "Stoch RSI",
        "value": f"K {k:.1f} / D {d:.1f}",
        "tone": tone,
        "description": description,
    }


def build_macd_card(macd: Any, signal: Any, histogram: Any) -> dict[str, str] | None:
    if not isinstance(macd, (int, float)) or not isinstance(signal, (int, float)):
        return None
    bullish = macd >= signal
    return {
        "label": "MACD",
        "value": "Bullish" if bullish else "Bearish",
        "tone": "bullish" if bullish else "bearish",
        "description": (
            f"MACD {macd:.2f} / Signal {signal:.2f}"
            + (
                f" / Hist {histogram:.2f}"
                if isinstance(histogram, (int, float))
                else ""
            )
        ),
    }


def build_bollinger_card(
    price: Any, upper: Any, middle: Any, lower: Any
) -> dict[str, str] | None:
    if not all(isinstance(v, (int, float)) for v in (price, upper, middle, lower)):
        return None
    if abs(price - lower) <= abs(price - upper) and abs(price - lower) <= abs(
        price - middle
    ):
        description, tone = "하단 근처", "oversold"
    elif abs(price - upper) < abs(price - middle):
        description, tone = "상단 근처", "overbought"
    else:
        description, tone = "중단 근처", "neutral"
    return {
        "label": "Bollinger",
        "value": description,
        "tone": tone,
        "description": f"상단 {upper:.2f} / 중단 {middle:.2f} / 하단 {lower:.2f}",
    }


def build_ema_card(
    price: Any, ema20: Any, ema60: Any, ema200: Any
) -> dict[str, str] | None:
    if not isinstance(price, (int, float)) or not isinstance(ema20, (int, float)):
        return None
    if (
        isinstance(ema60, (int, float))
        and isinstance(ema200, (int, float))
        and price > ema20 > ema60 > ema200
    ):
        tone, description = "bullish", "상방 정렬"
    elif (
        isinstance(ema60, (int, float))
        and isinstance(ema200, (int, float))
        and price < ema20 < ema60 < ema200
    ):
        tone, description = "bearish", "하방 정렬"
    else:
        tone, description = "neutral", "혼조"
    return {
        "label": "EMA",
        "value": description,
        "tone": tone,
        "description": (
            f"20 {ema20:.2f}"
            + (f" / 60 {ema60:.2f}" if isinstance(ema60, (int, float)) else "")
            + (f" / 200 {ema200:.2f}" if isinstance(ema200, (int, float)) else "")
        ),
    }


def build_sma_card(
    price: Any, sma20: Any, sma60: Any, sma200: Any
) -> dict[str, str] | None:
    if not isinstance(price, (int, float)) or not isinstance(sma20, (int, float)):
        return None
    if (
        isinstance(sma60, (int, float))
        and isinstance(sma200, (int, float))
        and price > sma20 > sma60 > sma200
    ):
        tone, description = "bullish", "상방 정렬"
    elif (
        isinstance(sma60, (int, float))
        and isinstance(sma200, (int, float))
        and price < sma20 < sma60 < sma200
    ):
        tone, description = "bearish", "하방 정렬"
    else:
        tone, description = "neutral", "혼조"
    return {
        "label": "SMA",
        "value": description,
        "tone": tone,
        "description": (
            f"20 {sma20:.2f}"
            + (f" / 60 {sma60:.2f}" if isinstance(sma60, (int, float)) else "")
            + (f" / 200 {sma200:.2f}" if isinstance(sma200, (int, float)) else "")
        ),
    }
