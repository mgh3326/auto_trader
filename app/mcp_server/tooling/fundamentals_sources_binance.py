"""Binance/crypto derivatives provider helpers."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import httpx

from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.mcp_server.tooling.shared import (
    to_optional_int as _to_optional_int,
)

BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
BINANCE_OPEN_INTEREST_URL = "https://fapi.binance.com/fapi/v1/openInterest"
BINANCE_OPEN_INTEREST_HIST_URL = (
    "https://fapi.binance.com/futures/data/openInterestHist"
)


def _oi_interpretation_text(oi_change_pct: float | None) -> str:
    if oi_change_pct is None:
        return "데이터 부족 — OI 추세 판단 불가"
    if oi_change_pct > 0:
        return "OI 증가 — 신규 포지션 유입 (추세 강화 가능)"
    if oi_change_pct < 0:
        return "OI 감소 — 포지션 청산/이탈"
    return "OI 변동 없음"


def _funding_interpretation_text(rate: float) -> str:
    if rate > 0:
        return "positive (롱이 숏에게 지불, 롱 과열)"
    if rate < 0:
        return "negative (숏이 롱에게 지불, 숏 과열)"
    return "neutral"


async def _fetch_funding_rate_batch(symbols: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []

    pair_to_symbol = {f"{symbol.upper()}USDT": symbol.upper() for symbol in symbols}

    async with httpx.AsyncClient(timeout=10) as cli:
        response = await cli.get(BINANCE_PREMIUM_INDEX_URL)
        response.raise_for_status()
        payload = response.json()

    rows: list[dict[str, Any]] = []
    data_list: list[dict[str, Any]]
    if isinstance(payload, list):
        data_list = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        data_list = [payload]
    else:
        data_list = []

    for row in data_list:
        pair = str(row.get("symbol") or "").upper()
        base_symbol = pair_to_symbol.get(pair)
        if not base_symbol:
            continue

        funding_rate = _to_optional_float(row.get("lastFundingRate"))
        next_ts = _to_optional_int(row.get("nextFundingTime"))
        if funding_rate is None or next_ts is None or next_ts <= 0:
            continue

        next_funding_time = datetime.datetime.fromtimestamp(
            next_ts / 1000, tz=datetime.UTC
        )
        rows.append(
            {
                "symbol": base_symbol,
                "funding_rate": funding_rate,
                "next_funding_time": next_funding_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "interpretation": _funding_interpretation_text(funding_rate),
            }
        )

    rows.sort(key=lambda item: str(item.get("symbol", "")))
    return rows


async def _fetch_funding_rate(symbol: str, limit: int) -> dict[str, Any]:
    pair = f"{symbol.upper()}USDT"

    async with httpx.AsyncClient(timeout=10) as cli:
        premium_resp, history_resp = await asyncio.gather(
            cli.get(BINANCE_PREMIUM_INDEX_URL, params={"symbol": pair}),
            cli.get(BINANCE_FUNDING_RATE_URL, params={"symbol": pair, "limit": limit}),
        )
        premium_resp.raise_for_status()
        history_resp.raise_for_status()

        premium_data = premium_resp.json()
        current_rate = float(premium_data.get("lastFundingRate", 0))
        next_funding_ts = int(premium_data.get("nextFundingTime", 0))
        next_funding_time = datetime.datetime.fromtimestamp(
            next_funding_ts / 1000, tz=datetime.UTC
        )

        funding_history: list[dict[str, Any]] = []
        rates_for_avg: list[float] = []
        for entry in history_resp.json():
            rate = float(entry.get("fundingRate", 0))
            ts = int(entry.get("fundingTime", 0))
            time_str = datetime.datetime.fromtimestamp(
                ts / 1000, tz=datetime.UTC
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            funding_history.append(
                {
                    "time": time_str,
                    "rate": rate,
                    "rate_pct": round(rate * 100, 4),
                }
            )
            rates_for_avg.append(rate)

        avg_rate = (
            round(sum(rates_for_avg) / len(rates_for_avg) * 100, 4)
            if rates_for_avg
            else None
        )

        return {
            "symbol": pair,
            "current_funding_rate": current_rate,
            "current_funding_rate_pct": round(current_rate * 100, 4),
            "next_funding_time": next_funding_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "funding_history": funding_history,
            "avg_funding_rate_pct": avg_rate,
            "interpretation": {
                "positive": "롱이 숏에게 지불 (롱 과열 — 시장이 과도하게 강세)",
                "negative": "숏이 롱에게 지불 (숏 과열 — 시장이 과도하게 약세)",
            },
        }


async def _fetch_open_interest(symbol: str, period: str, limit: int) -> dict[str, Any]:
    pair = f"{symbol.upper()}USDT"

    async with httpx.AsyncClient(timeout=10) as cli:
        current_resp, hist_resp = await asyncio.gather(
            cli.get(BINANCE_OPEN_INTEREST_URL, params={"symbol": pair}),
            cli.get(
                BINANCE_OPEN_INTEREST_HIST_URL,
                params={"symbol": pair, "period": period, "limit": limit},
            ),
        )
        current_resp.raise_for_status()
        hist_resp.raise_for_status()
        current_data = current_resp.json()
        hist_data = hist_resp.json()

    current_oi = _to_optional_float((current_data or {}).get("openInterest"))

    history: list[dict[str, Any]] = []
    for entry in hist_data if isinstance(hist_data, list) else []:
        if not isinstance(entry, dict):
            continue
        ts = _to_optional_int(entry.get("timestamp"))
        if ts is None:
            continue
        history.append(
            {
                "_ts": ts,
                "time": datetime.datetime.fromtimestamp(
                    ts / 1000, tz=datetime.UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sum_open_interest": _to_optional_float(entry.get("sumOpenInterest")),
                "sum_open_interest_value_usd": _to_optional_float(
                    entry.get("sumOpenInterestValue")
                ),
            }
        )
    history.sort(key=lambda e: e["_ts"])
    for e in history:
        del e["_ts"]

    oi_change_pct: float | None = None
    if len(history) >= 2:
        first = history[0]["sum_open_interest"]
        last = history[-1]["sum_open_interest"]
        if first is not None and last is not None and first != 0:
            oi_change_pct = round((last - first) / first * 100, 4)

    return {
        "symbol": pair,
        "current_open_interest": current_oi,
        "period": period,
        "open_interest_history": history,
        "oi_change_pct": oi_change_pct,
        "interpretation": _oi_interpretation_text(oi_change_pct),
    }


BINANCE_GLOBAL_LSR_URL = (
    "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
)
BINANCE_TOP_POSITION_LSR_URL = (
    "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
)


def _lsr_leg_interpretation(role: str, ratio: float | None) -> str:
    if ratio is None:
        return f"{role}: 데이터 없음"
    if ratio > 1:
        return f"{role} 롱 우위 (ratio={ratio:.2f}>1)"
    if ratio < 1:
        return f"{role} 숏 우위 (ratio={ratio:.2f}<1)"
    return f"{role} 롱숏 균형 (ratio=1)"


def _build_lsr_leg(data: Any, *, role: str) -> dict[str, Any] | None:
    rows = data if isinstance(data, list) else []
    history: list[dict[str, Any]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        ts = _to_optional_int(entry.get("timestamp"))
        if ts is None:
            continue
        long_acc = _to_optional_float(entry.get("longAccount"))
        short_acc = _to_optional_float(entry.get("shortAccount"))
        history.append(
            {
                "_ts": ts,
                "time": datetime.datetime.fromtimestamp(
                    ts / 1000, tz=datetime.UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ratio": _to_optional_float(entry.get("longShortRatio")),
                "long_pct": round(long_acc * 100, 2) if long_acc is not None else None,
                "short_pct": round(short_acc * 100, 2)
                if short_acc is not None
                else None,
            }
        )
    if not history:
        return None
    history.sort(key=lambda e: e["_ts"])
    for e in history:
        del e["_ts"]
    current = history[-1]
    return {
        "ratio": current["ratio"],
        "long_pct": current["long_pct"],
        "short_pct": current["short_pct"],
        "history": history,
        "interpretation": _lsr_leg_interpretation(role, current["ratio"]),
    }


def _lsr_divergence_note(
    global_leg: dict[str, Any] | None, top_leg: dict[str, Any] | None
) -> str:
    if (
        not global_leg
        or not top_leg
        or global_leg.get("ratio") is None
        or top_leg.get("ratio") is None
    ):
        return "divergence 판단 불가 (일부 데이터 없음)"
    g_long = global_leg["ratio"] > 1
    t_long = top_leg["ratio"] > 1
    if g_long == t_long:
        side = "롱" if g_long else "숏"
        return f"리테일·스마트머니 동일 방향 ({side} 우위) — 신호 정렬"
    if g_long and not t_long:
        return "리테일 롱 / 스마트머니 숏 — contrarian 주의"
    return "리테일 숏 / 스마트머니 롱 — contrarian 주의"


async def _fetch_long_short_ratio(
    symbol: str, period: str, limit: int
) -> dict[str, Any]:
    pair = f"{symbol.upper()}USDT"
    params = {"symbol": pair, "period": period, "limit": limit}

    async with httpx.AsyncClient(timeout=10) as cli:
        global_resp, top_resp = await asyncio.gather(
            cli.get(BINANCE_GLOBAL_LSR_URL, params=params),
            cli.get(BINANCE_TOP_POSITION_LSR_URL, params=params),
        )
        global_resp.raise_for_status()
        top_resp.raise_for_status()
        global_data = global_resp.json()
        top_data = top_resp.json()

    global_leg = _build_lsr_leg(global_data, role="리테일 계정")
    top_leg = _build_lsr_leg(top_data, role="상위 트레이더 포지션")

    return {
        "symbol": pair,
        "period": period,
        "global_account": global_leg,
        "top_position": top_leg,
        "divergence_note": _lsr_divergence_note(global_leg, top_leg),
    }


__all__ = [
    "_fetch_funding_rate",
    "_fetch_funding_rate_batch",
    "_fetch_long_short_ratio",
    "_fetch_open_interest",
]
