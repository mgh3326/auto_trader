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


__all__ = ["_fetch_funding_rate", "_fetch_funding_rate_batch"]
