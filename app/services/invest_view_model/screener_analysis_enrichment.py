from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from app.schemas.invest_screener import (
    ScreenerAnalysisConsensus,
)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def build_rsi14_from_closes(closes: Sequence[Any]) -> float | None:
    values = [_to_float(v) for v in closes]
    clean = [v for v in values if v is not None]
    if len(clean) < 15:
        return None
    close = pd.Series(clean)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_value = rsi.iloc[-1]
    return round(float(rsi_value), 2) if pd.notna(rsi_value) else None


def normalize_consensus_payload(
    payload: dict[str, Any] | None,
) -> tuple[ScreenerAnalysisConsensus | None, list[str]]:
    if not payload or payload.get("error"):
        return None, ["analyst_consensus_unavailable"]

    raw = payload.get("consensus") or {}
    if not isinstance(raw, dict):
        return None, ["analyst_consensus_unavailable"]

    total = _to_int(raw.get("total_count") or raw.get("totalCount"))
    if total is None or total <= 0:
        return None, ["analyst_consensus_missing"]

    consensus = ScreenerAnalysisConsensus(
        source=payload.get("source") or payload.get("provider"),
        buyCount=_to_int(raw.get("buy_count") or raw.get("buyCount")),
        holdCount=_to_int(raw.get("hold_count") or raw.get("holdCount")),
        sellCount=_to_int(raw.get("sell_count") or raw.get("sellCount")),
        strongBuyCount=_to_int(
            raw.get("strong_buy_count") or raw.get("strongBuyCount")
        ),
        totalCount=total,
        avgTargetPrice=_to_float(
            raw.get("avg_target_price") or raw.get("avgTargetPrice")
        ),
        medianTargetPrice=_to_float(
            raw.get("median_target_price") or raw.get("medianTargetPrice")
        ),
        minTargetPrice=_to_float(
            raw.get("min_target_price") or raw.get("minTargetPrice")
        ),
        maxTargetPrice=_to_float(
            raw.get("max_target_price") or raw.get("maxTargetPrice")
        ),
        upsidePct=_to_float(raw.get("upside_pct") or raw.get("upsidePct")),
        currentPrice=_to_float(raw.get("current_price") or raw.get("currentPrice")),
    )

    warnings: list[str] = []
    if (
        consensus.avgTargetPrice is not None
        and consensus.currentPrice is not None
        and consensus.currentPrice > 0
    ):
        computed = round(
            (consensus.avgTargetPrice - consensus.currentPrice)
            / consensus.currentPrice
            * 100,
            2,
        )
        if (
            consensus.upsidePct is not None
            and abs(computed - consensus.upsidePct) > 1.0
        ):
            warnings.append("consensus_upside_mismatch")
        if (
            consensus.avgTargetPrice < consensus.currentPrice
            and (consensus.buyCount or 0)
            > ((consensus.holdCount or 0) + (consensus.sellCount or 0))
        ):
            warnings.append("consensus_target_below_current_with_bullish_votes")
    return consensus, warnings


def build_analyst_label(
    consensus: ScreenerAnalysisConsensus | None, *, warnings: list[str] | None = None
) -> str:
    if consensus is None or not consensus.totalCount:
        return "-"
    if warnings:
        return "컨센 확인필요"
    buy = consensus.buyCount or 0
    hold = consensus.holdCount or 0
    sell = consensus.sellCount or 0
    base = f"매수 {buy} / 보유 {hold} / 매도 {sell}"
    if consensus.upsidePct is None:
        return base
    return f"{base} · 목표 {consensus.upsidePct:+.1f}%"
