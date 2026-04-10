"""Shared screening utilities — constants, converters, normalization, filters, response builder."""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DROP_THRESHOLD = -0.30
MARKET_PANIC = -0.10
CRYPTO_TOP_BY_VOLUME = 100
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

# ---------------------------------------------------------------------------
# Timeout Policy Configuration
# ---------------------------------------------------------------------------

# Default timeout values (in seconds) for different pipeline stages
DEFAULT_TIMEOUTS = {
    "tvscreener": 30.0,
    "rsi_enrichment": 30.0,
    "crypto_enrichment": 30.0,
    "http_client": 10.0,
    "candidate_collection": 60.0,
}


def _timeout_seconds(name: str) -> float:
    return float(DEFAULT_TIMEOUTS[name])


class TimeoutBehavior:
    """Timeout handling behavior enum."""

    RAISE = "raise"  # Propagate the timeout exception
    RETURN_PARTIAL = "return_partial"  # Return partial results with diagnostics
    FALLBACK = "fallback"  # Trigger fallback logic


async def _with_timeout(
    coro,
    timeout_seconds: float,
    *,
    behavior: str = TimeoutBehavior.RAISE,
    fallback_value: Any = None,
    error_context: dict[str, Any] | None = None,
) -> Any:
    """Execute a coroutine with a timeout and configurable behavior.

    Args:
        coro: The coroutine to execute
        timeout_seconds: Timeout in seconds
        behavior: How to handle timeout (RAISE, RETURN_PARTIAL, FALLBACK)
        fallback_value: Value to return on timeout if behavior is FALLBACK
        error_context: Additional context for error logging

    Returns:
        The coroutine result, or fallback_value on timeout depending on behavior

    Raises:
        asyncio.TimeoutError: If behavior is RAISE and timeout occurs
    """
    import asyncio

    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError:
        context_msg = ""
        if error_context:
            context_msg = f" | context: {error_context}"
        logger.warning(
            "Operation timed out after %.1fs (behavior=%s)%s",
            timeout_seconds,
            behavior,
            context_msg,
        )

        if behavior == TimeoutBehavior.RAISE:
            raise
        elif behavior == TimeoutBehavior.FALLBACK:
            return fallback_value
        elif behavior == TimeoutBehavior.RETURN_PARTIAL:
            # Return a dict with partial results indicator
            return {
                "_timeout_occurred": True,
                "_timeout_seconds": timeout_seconds,
                "result": fallback_value,
            }
        else:
            # Unknown behavior, default to raising
            raise


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _to_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rank_priority(rank: int | None) -> int:
    if rank is None or rank <= 0:
        return 1_000_000_000
    return rank


def is_safe_drop(coin_change_24h: Any, btc_change_24h: Any) -> bool:
    coin_change = _to_optional_float(coin_change_24h)
    if coin_change is None:
        return True
    btc_change = _to_optional_float(btc_change_24h)
    if btc_change is None:
        btc_change = 0.0
    return not (coin_change <= DROP_THRESHOLD and btc_change > MARKET_PANIC)


def _extract_market_symbol(symbol: Any) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if "-" in text:
        token = text.split("-", maxsplit=1)[1].strip()
        return token or None
    return text


def _compute_rsi_bucket(rsi: Any) -> int:
    rsi_value = _to_optional_float(rsi)
    if rsi_value is None:
        return 999
    return int(rsi_value // 5) * 5


def _strip_exchange_prefix(symbol: Any) -> str:
    text = str(symbol or "").strip()
    if not text:
        return ""
    return text.split(":", maxsplit=1)[-1].strip()


def _get_first_present(mapping: Any, *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def _get_tvscreener_attr(enum_obj: Any, *names: str) -> Any | None:
    for name in names:
        value = getattr(enum_obj, name, None)
        if value is not None:
            return value
    return None


def _extract_kr_stock_code(value: Any) -> str:
    return _strip_exchange_prefix(value).upper()


def _kr_market_codes(market: str) -> tuple[list[str], str]:
    if market == "kospi":
        return ["STK"], "STK"
    if market == "kosdaq":
        return ["KSQ"], "KSQ"
    return ["STK", "KSQ"], "ALL"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        if math.isnan(float(text)):
            return ""
    except (TypeError, ValueError):
        pass
    return text


# ---------------------------------------------------------------------------
# MarketCapCache
# ---------------------------------------------------------------------------


class MarketCapCache:
    def __init__(self, ttl: int = 600) -> None:
        self.ttl = ttl
        self._lock = asyncio.Lock()
        self._symbol_map: dict[str, dict[str, Any]] = {}
        self._updated_at: float | None = None

    def _age_seconds(self, now: float) -> float | None:
        if self._updated_at is None:
            return None
        return round(max(0.0, now - self._updated_at), 3)

    def _is_fresh(self, now: float) -> bool:
        if not self._symbol_map or self._updated_at is None:
            return False
        return (now - self._updated_at) <= self.ttl

    async def _fetch_market_caps(self) -> dict[str, dict[str, Any]]:
        params = {
            "vs_currency": "krw",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(COINGECKO_MARKETS_URL, params=params)
            response.raise_for_status()
            rows = response.json()

        if not isinstance(rows, list):
            raise ValueError("Unexpected CoinGecko response format")

        symbol_map: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            market_cap = _to_optional_float(row.get("market_cap"))
            market_cap_rank = _to_optional_int(row.get("market_cap_rank"))

            selected = {
                "market_cap": market_cap,
                "market_cap_rank": market_cap_rank,
            }
            existing = symbol_map.get(symbol)
            if existing is None or _rank_priority(market_cap_rank) < _rank_priority(
                _to_optional_int(existing.get("market_cap_rank"))
            ):
                symbol_map[symbol] = selected

        return symbol_map

    async def get(self) -> dict[str, Any]:
        now = time.time()
        if self._is_fresh(now):
            return {
                "data": self._symbol_map,
                "cached": True,
                "age_seconds": self._age_seconds(now),
                "stale": False,
                "error": None,
            }

        async with self._lock:
            now = time.time()
            if self._is_fresh(now):
                return {
                    "data": self._symbol_map,
                    "cached": True,
                    "age_seconds": self._age_seconds(now),
                    "stale": False,
                    "error": None,
                }
            try:
                fetched = await self._fetch_market_caps()
                self._symbol_map = fetched
                self._updated_at = now
                return {
                    "data": fetched,
                    "cached": False,
                    "age_seconds": 0.0,
                    "stale": False,
                    "error": None,
                }
            except Exception as exc:
                if self._symbol_map:
                    return {
                        "data": self._symbol_map,
                        "cached": True,
                        "age_seconds": self._age_seconds(now),
                        "stale": True,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                return {
                    "data": {},
                    "cached": False,
                    "age_seconds": None,
                    "stale": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_screen_market(market: str | None) -> str:
    """Normalize market parameter to internal format."""
    if not market:
        return "kr"
    return market.lower()


def _normalize_asset_type(asset_type: str | None) -> str | None:
    """Normalize asset_type parameter."""
    if asset_type is None:
        return None
    return asset_type.lower()


def _normalize_sort_by(sort_by: str | None) -> str:
    """Normalize sort_by parameter."""
    if not sort_by:
        return "volume"
    return sort_by.lower()


def _normalize_sort_order(sort_order: str | None) -> str:
    """Normalize sort_order parameter."""
    if not sort_order:
        return "desc"
    return sort_order.lower()


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized or None


def _normalize_sector_value(sector: str | None) -> str | None:
    return _normalize_optional_text(sector)


def _normalize_sector_compare_key(sector: str | None) -> str | None:
    normalized = _normalize_optional_text(sector)
    if normalized is None:
        return None
    if normalized.isascii():
        return normalized.casefold()
    return normalized


def _canonicalize_us_sector_label(sector: str) -> str:
    """Canonicalize a US sector label for the TradingView tvscreener provider.

    General ASCII strings → title case (``"technology"`` → ``"Technology"``).
    Short ASCII tokens (≤3 chars) → upper case (``"ai"`` → ``"AI"``).
    Non-ASCII values → whitespace cleanup only (unchanged).
    """
    if not sector.isascii():
        return sector
    if len(sector) <= 3:
        return sector.upper()
    return sector.title()


def _normalize_min_analyst_buy(value: float | None) -> float | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError("min_analyst_buy must be >= 0")
    return value


def _normalize_dividend_yield_threshold(
    value: float | None,
) -> tuple[float | None, float | None]:
    """Normalize dividend yield threshold to decimal format."""
    if value is None:
        return None, None
    normalized_value = value / 100 if value >= 1 else value
    return value, normalized_value


def _normalize_min_dividend_value(
    *,
    min_dividend_yield: float | None,
    min_dividend: float | None,
) -> tuple[float | None, float | None]:
    if min_dividend is None and min_dividend_yield is None:
        return None, None
    if min_dividend is not None and min_dividend_yield is not None:
        _, normalized_min_dividend = _normalize_dividend_yield_threshold(min_dividend)
        _, normalized_min_dividend_yield = _normalize_dividend_yield_threshold(
            min_dividend_yield
        )
        if normalized_min_dividend != normalized_min_dividend_yield:
            raise ValueError(
                "min_dividend and min_dividend_yield cannot specify different values"
            )
        return min_dividend, normalized_min_dividend
    canonical_input = min_dividend if min_dividend is not None else min_dividend_yield
    _, normalized_value = _normalize_dividend_yield_threshold(canonical_input)
    return canonical_input, normalized_value


def normalize_screen_request(
    *,
    market: str,
    asset_type: str | None,
    category: str | None,
    sector: str | None,
    strategy: str | None,
    sort_by: str | None,
    sort_order: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    min_dividend: float | None,
    min_analyst_buy: float | None,
    max_rsi: float | None,
    limit: int,
) -> dict[str, Any]:
    normalized_market = _normalize_screen_market(market)
    normalized_asset_type = _normalize_asset_type(asset_type)
    normalized_category = _normalize_optional_text(category)
    normalized_sector = _normalize_sector_value(sector)
    normalized_strategy = _normalize_optional_text(strategy)
    normalized_sort_by = _normalize_sort_by(sort_by)
    normalized_sort_order = _normalize_sort_order(sort_order)
    normalized_min_analyst_buy = _normalize_min_analyst_buy(min_analyst_buy)
    min_dividend_input, normalized_min_dividend_yield = _normalize_min_dividend_value(
        min_dividend_yield=min_dividend_yield,
        min_dividend=min_dividend,
    )

    if normalized_category is not None and normalized_sector is not None:
        category_alias = (
            _normalize_sector_compare_key(normalized_category)
            if normalized_market == "us"
            else _normalize_optional_text(normalized_category)
        )
        if category_alias != _normalize_sector_compare_key(normalized_sector):
            raise ValueError("category and sector cannot specify different values")

    effective_sector = normalized_sector
    if (
        effective_sector is None
        and normalized_market == "us"
        and normalized_category is not None
    ):
        effective_sector = _normalize_sector_value(normalized_category)
    if normalized_market == "us" and effective_sector is not None:
        effective_sector = _canonicalize_us_sector_label(effective_sector)

    if effective_sector is not None:
        if normalized_market == "crypto":
            raise ValueError("crypto market does not support sector filter")
        if normalized_market in {"kr", "kospi", "kosdaq"} and normalized_asset_type in {
            "etf",
            "etn",
        }:
            raise ValueError("sector filter is only supported for stock requests")

    if normalized_min_analyst_buy is not None:
        if normalized_market == "crypto":
            raise ValueError("crypto market does not support min_analyst_buy filter")
        if normalized_asset_type not in {None, "stock"}:
            raise ValueError("min_analyst_buy is only supported for stock requests")

    if min_dividend is not None and normalized_market == "crypto":
        raise ValueError("crypto market does not support min_dividend filter")

    category_for_filters = normalized_category
    effective_category = normalized_category
    if normalized_market == "us" and effective_sector is not None:
        effective_category = effective_sector
        if category_for_filters is None:
            category_for_filters = effective_sector

    return {
        "market": normalized_market,
        "asset_type": normalized_asset_type,
        "category": normalized_category,
        "category_for_filters": category_for_filters,
        "effective_category": effective_category,
        "sector": effective_sector,
        "strategy": normalized_strategy,
        "sort_by": normalized_sort_by,
        "sort_order": normalized_sort_order,
        "min_market_cap": min_market_cap,
        "max_per": max_per,
        "max_pbr": max_pbr,
        "min_dividend_yield": normalized_min_dividend_yield,
        "min_dividend_input": min_dividend_input,
        "min_analyst_buy": normalized_min_analyst_buy,
        "max_rsi": max_rsi,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _validate_screen_filters(
    market: str,
    asset_type: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str | None,
) -> None:
    """Validate screening filters and raise ValueError for unsupported combinations."""
    _ = min_market_cap
    _ = max_rsi

    if market == "crypto":
        if max_per is not None:
            raise ValueError(
                "Crypto market does not support 'max_per' filter (no P/E ratio)"
            )
        if min_dividend_yield is not None:
            raise ValueError(
                "Crypto market does not support 'min_dividend_yield' filter (no dividends)"
            )
        if sort_by == "volume":
            raise ValueError(
                "Crypto market does not support sorting by 'volume'; use 'trade_amount'"
            )
        if sort_by == "dividend_yield":
            raise ValueError(
                "Crypto market does not support sorting by 'dividend_yield'"
            )
    else:
        if sort_by == "trade_amount":
            raise ValueError(
                "'trade_amount' sorting is only supported for crypto market"
            )

    if market in ("kr", "kospi", "kosdaq") and asset_type == "etn":
        raise ValueError(
            "Korean market (KR/KOSPI/KOSDAQ) does not support ETN (Exchange Traded Notes) asset_type"
        )


def _apply_basic_filters(
    candidates: list[dict[str, Any]],
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
) -> list[dict[str, Any]]:
    """Apply basic numeric filters to candidate stocks."""
    filtered = []

    for item in candidates:
        skip = False

        if min_market_cap is not None:
            if item.get("market_cap") is None:
                skip = True
            elif item["market_cap"] < min_market_cap:
                skip = True

        if not skip and max_per is not None:
            if item.get("per") is None:
                skip = True
            elif item["per"] > max_per:
                skip = True

        if not skip and max_pbr is not None:
            if item.get("pbr") is None:
                skip = True
            elif item["pbr"] > max_pbr:
                skip = True

        if not skip and min_dividend_yield is not None:
            if item.get("dividend_yield") is None:
                skip = True
            elif item["dividend_yield"] < min_dividend_yield:
                skip = True

        if not skip and max_rsi is not None:
            if item.get("rsi") is None:
                skip = True
            elif item["rsi"] > max_rsi:
                skip = True

        if not skip:
            filtered.append(item)

    return filtered


def _sort_and_limit(
    results: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Sort and limit results."""
    if not results:
        return []

    sort_field_map = {
        "volume": "volume",
        "trade_amount": "trade_amount_24h",
        "market_cap": "market_cap",
        "change_rate": "change_rate",
        "dividend_yield": "dividend_yield",
        "rsi": "rsi",  # crypto only
        "score": "score",
    }
    field = sort_field_map.get(sort_by, "volume")
    reverse = sort_order == "desc"

    def sort_value(item: dict[str, Any]) -> float:
        value = item.get(field)
        if field in {"rsi", "score"} and value is None:
            return -999.0 if reverse else 999.0
        return float(value or 0)

    results.sort(key=sort_value, reverse=reverse)
    return results[:limit]


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def _empty_rsi_enrichment_diagnostics() -> dict[str, Any]:
    return {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "rate_limited": 0,
        "timeout": 0,
        "error_samples": [],
    }


def _finalize_rsi_enrichment_diagnostics(
    diagnostics: dict[str, Any],
    statuses: list[str],
    errors: list[str | None],
) -> dict[str, Any]:
    diagnostics["succeeded"] = sum(1 for status in statuses if status == "success")
    diagnostics["failed"] = sum(1 for status in statuses if status == "error")
    diagnostics["rate_limited"] = sum(
        1 for status in statuses if status == "rate_limited"
    )
    diagnostics["timeout"] = sum(1 for status in statuses if status == "timeout")

    samples: list[str] = []
    for error in errors:
        if not error:
            continue
        samples.append(str(error)[:100])
        if len(samples) >= 3:
            break

    diagnostics["error_samples"] = samples
    return diagnostics


def _build_screen_response(
    results: list[dict[str, Any]],
    total_count: int,
    filters_applied: dict[str, Any],
    market: str,
    rsi_enrichment: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    meta_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the final screening response."""
    diagnostics = _empty_rsi_enrichment_diagnostics()
    if rsi_enrichment:
        diagnostics.update(
            {
                "attempted": int(rsi_enrichment.get("attempted", 0) or 0),
                "succeeded": int(rsi_enrichment.get("succeeded", 0) or 0),
                "failed": int(rsi_enrichment.get("failed", 0) or 0),
                "rate_limited": int(rsi_enrichment.get("rate_limited", 0) or 0),
                "timeout": int(rsi_enrichment.get("timeout", 0) or 0),
                "error_samples": [
                    str(message)[:100]
                    for message in (rsi_enrichment.get("error_samples") or [])[:3]
                ],
            }
        )

    response_meta: dict[str, Any] = {"rsi_enrichment": diagnostics}
    if meta_fields:
        response_meta.update(meta_fields)

    response: dict[str, Any] = {
        "results": results,
        "total_count": total_count,
        "returned_count": len(results),
        "filters_applied": filters_applied,
        "market": market,
        "meta": response_meta,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }

    if warnings:
        response["warnings"] = warnings

    return response


# ---------------------------------------------------------------------------
# Tvscreener shared helpers
# ---------------------------------------------------------------------------


def _init_tvscreener_result(filters_applied: dict[str, Any]) -> dict[str, Any]:
    """Create the standard tvscreener result dict."""
    return {
        "stocks": [],
        "source": "tvscreener",
        "count": 0,
        "filters_applied": dict(filters_applied),
        "error": None,
    }


def _aggregate_analyst_recommendations(row: Any) -> dict[str, Any]:
    """Compute analyst_buy/hold/sell from recommendation_buy/over/hold/sell/under.

    Returns a dict with only the keys that have non-None source values.
    """
    recommendation_buy = _to_optional_int(_get_first_present(row, "recommendation_buy"))
    recommendation_over = _to_optional_int(
        _get_first_present(row, "recommendation_over")
    )
    recommendation_hold = _to_optional_int(
        _get_first_present(row, "recommendation_hold")
    )
    recommendation_sell = _to_optional_int(
        _get_first_present(row, "recommendation_sell")
    )
    recommendation_under = _to_optional_int(
        _get_first_present(row, "recommendation_under")
    )

    result: dict[str, Any] = {}
    if recommendation_buy is not None or recommendation_over is not None:
        result["analyst_buy"] = (recommendation_buy or 0) + (recommendation_over or 0)
    if recommendation_hold is not None:
        result["analyst_hold"] = recommendation_hold
    if recommendation_sell is not None or recommendation_under is not None:
        result["analyst_sell"] = (recommendation_sell or 0) + (
            recommendation_under or 0
        )
    return result


def _filter_by_min_analyst_buy(
    stocks: list[dict[str, Any]],
    min_analyst_buy: float | None,
) -> list[dict[str, Any]]:
    """Filter stocks by min_analyst_buy threshold. Returns input list if threshold is None."""
    if min_analyst_buy is None:
        return stocks
    return [
        stock
        for stock in stocks
        if _to_optional_float(stock.get("analyst_buy")) is not None
        and float(stock["analyst_buy"]) >= min_analyst_buy
    ]


def _build_rsi_adx_conditions(
    *,
    min_rsi: float | None,
    max_rsi: float | None,
    min_adx: float | None,
    rsi_field: Any = None,
    adx_field: Any = None,
) -> list[Any]:
    """Build RSI/ADX where-clause conditions for tvscreener queries.

    If rsi_field/adx_field are not provided, callers must use the returned
    condition *callables* with their own field references — but the typical
    usage is to pass StockField / CryptoField constants directly.
    """
    conditions: list[Any] = []
    if rsi_field is not None:
        if min_rsi is not None:
            conditions.append(rsi_field >= min_rsi)
        if max_rsi is not None:
            conditions.append(rsi_field <= max_rsi)
    if adx_field is not None and min_adx is not None:
        conditions.append(adx_field >= min_adx)
    return conditions


def _compute_avg_target_and_upside(
    row: Any,
    *,
    current_price: float | None,
) -> tuple[float | None, float | None]:
    """Extract avg_target and upside_pct from a tvscreener row.

    Tries price_target_1y first, then price_target_average / target_price_average.
    If upside_pct (price_target_1y_delta) is missing, computes it from avg_target
    and current_price.
    """
    from app.mcp_server.tooling.screening.enrichment import _compute_target_upside_pct

    avg_target = _to_optional_float(
        _get_first_present(
            row,
            "price_target_1y",
            "price_target_average",
            "target_price_average",
        )
    )
    upside_pct = _to_optional_float(_get_first_present(row, "price_target_1y_delta"))
    if upside_pct is None:
        upside_pct = _compute_target_upside_pct(
            avg_target=avg_target,
            current_price=current_price,
        )
    return avg_target, upside_pct
