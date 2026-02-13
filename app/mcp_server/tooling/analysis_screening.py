"""Analysis and screening helper functions.

This module contains pure calculation and transformation helpers for stock screening,
rankings, and analysis. Service-dependent functions remain in tools.py.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.mcp_server.tooling.shared import (
    _to_float,
    _to_int,
    _to_optional_float,
    _to_optional_int,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Naver Data Parsing Helpers
# ---------------------------------------------------------------------------


def _parse_naver_num(value: Any) -> float | None:
    """Parse a naver number which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    """Parse a naver integer which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Change Rate Normalization
# ---------------------------------------------------------------------------


def _parse_change_rate(value: Any) -> float | None:
    val = _to_optional_float(value)
    if val is None:
        return None
    return val


def _normalize_change_rate_equity(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val


def _normalize_change_rate_crypto(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val * 100


# ---------------------------------------------------------------------------
# Ranking Row Mapping
# ---------------------------------------------------------------------------


def _map_kr_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd", "")
    name = row.get("hts_kor_isnm", "")
    price = _to_float(row.get("stck_prpr"))
    change_rate = _normalize_change_rate_equity(row.get("prdy_ctrt"))
    volume = _to_int(row.get("acml_vol") or row.get("frgn_ntby_qty"))
    market_cap = _to_float(row.get("hts_avls"))
    trade_amount = _to_float(row.get("acml_tr_pbmn") or row.get("frgn_ntby_tr_pbmn"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_us_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("symbol", "")
    name = row.get("longName", "") or row.get("shortName", symbol)
    price = _to_float(row.get("regularMarketPrice"))
    prev_close = _to_float(row.get("previousClose"))

    if price is not None and prev_close is not None and prev_close > 0:
        change_rate = ((price - prev_close) / prev_close) * 100
    else:
        change_rate = _to_float(row.get("regularMarketChangePercent", 0))

    volume = _to_int(row.get("regularMarketVolume"))
    market_cap = _to_float(row.get("marketCap"))
    trade_amount = None

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_crypto_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("market", "")
    name = symbol.replace("KRW-", "") if symbol.startswith("KRW-") else symbol
    price = _to_float(row.get("trade_price"))
    change_rate = _normalize_change_rate_crypto(row.get("signed_change_rate"))
    volume = _to_float(row.get("acc_trade_volume_24h"))
    market_cap = None
    trade_amount = _to_float(row.get("acc_trade_price_24h"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


# ---------------------------------------------------------------------------
# Crypto Symbol Normalization
# ---------------------------------------------------------------------------


def _normalize_crypto_base_symbol(symbol: str) -> str:
    """Normalize crypto symbol to base currency (e.g., 'KRW-BTC' -> 'BTC')."""
    normalized = symbol.upper().strip()
    if normalized.startswith("KRW-"):
        normalized = normalized[len("KRW-") :]
    if normalized.startswith("USDT-"):
        normalized = normalized[len("USDT-") :]
    if normalized.endswith("-KRW"):
        normalized = normalized[: -len("-KRW")]
    if normalized.endswith("-USDT"):
        normalized = normalized[: -len("-USDT")]
    if normalized.endswith("USDT"):
        normalized = normalized[: -len("USDT")]

    return normalized


# ---------------------------------------------------------------------------
# CoinGecko Helpers
# ---------------------------------------------------------------------------


def _coingecko_cache_valid(expires_at: Any, now: float) -> bool:
    try:
        return float(expires_at) > now
    except Exception:
        return False


def _to_optional_money(value: Any) -> int | None:
    numeric = _to_optional_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _clean_description_one_line(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    if len(text) > 240:
        text = text[:240].rstrip() + "..."
    return text


def _map_coingecko_profile_to_output(profile: dict[str, Any]) -> dict[str, Any]:
    market_data = profile.get("market_data") or {}
    description_map = profile.get("description") or {}

    description = _clean_description_one_line(
        description_map.get("ko") or description_map.get("en")
    )

    market_cap_krw = _to_optional_money(
        (market_data.get("market_cap") or {}).get("krw")
    )
    total_volume_krw = _to_optional_money(
        (market_data.get("total_volume") or {}).get("krw")
    )
    ath_krw = _to_optional_money((market_data.get("ath") or {}).get("krw"))

    ath_change_pct = _to_optional_float(
        (market_data.get("ath_change_percentage") or {}).get("krw")
    )
    change_7d = _to_optional_float(
        (market_data.get("price_change_percentage_7d_in_currency") or {}).get("krw")
    )
    if change_7d is None:
        change_7d = _to_optional_float(market_data.get("price_change_percentage_7d"))

    change_30d = _to_optional_float(
        (market_data.get("price_change_percentage_30d_in_currency") or {}).get("krw")
    )
    if change_30d is None:
        change_30d = _to_optional_float(market_data.get("price_change_percentage_30d"))

    categories = profile.get("categories")
    if not isinstance(categories, list):
        categories = []

    return {
        "name": profile.get("name"),
        "symbol": str(profile.get("symbol") or "").upper() or None,
        "market_cap": market_cap_krw,
        "market_cap_rank": _to_optional_int(profile.get("market_cap_rank")),
        "total_volume_24h": total_volume_krw,
        "circulating_supply": _to_optional_float(market_data.get("circulating_supply")),
        "total_supply": _to_optional_float(market_data.get("total_supply")),
        "max_supply": _to_optional_float(market_data.get("max_supply")),
        "categories": categories,
        "description": description,
        "ath": ath_krw,
        "ath_change_percentage": ath_change_pct,
        "price_change_percentage_7d": change_7d,
        "price_change_percentage_30d": change_30d,
    }


# ---------------------------------------------------------------------------
# Funding Rate Helpers
# ---------------------------------------------------------------------------


def _funding_interpretation_text(rate: float) -> str:
    if rate > 0:
        return "positive (롱이 숏에게 지불, 롱 과열)"
    if rate < 0:
        return "negative (숏이 롱에게 지불, 숏 과열)"
    return "neutral"


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

ANALYSIS_TOOL_NAMES: set[str] = {
    "analyze_stock",
    "analyze_portfolio",
    "screen_stocks",
    "recommend_stocks",
    "get_top_stocks",
    "get_disclosures",
    "get_correlation",
    "get_dividends",
    "get_fear_greed_index",
}


def register_analysis_tools(mcp: FastMCP) -> None:
    from app.mcp_server.tooling.registrars import register_tool_subset

    register_tool_subset(mcp, ANALYSIS_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Backward Compatibility Aliases
# ---------------------------------------------------------------------------

__all__ = [
    "_parse_naver_num",
    "_parse_naver_int",
    "_parse_change_rate",
    "_normalize_change_rate_equity",
    "_normalize_change_rate_crypto",
    "_map_kr_row",
    "_map_us_row",
    "_map_crypto_row",
    "_normalize_crypto_base_symbol",
    "_coingecko_cache_valid",
    "_to_optional_money",
    "_clean_description_one_line",
    "_map_coingecko_profile_to_output",
    "_funding_interpretation_text",
    "ANALYSIS_TOOL_NAMES",
    "register_analysis_tools",
]
