"""Stock screening helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from importlib import import_module
from typing import Any, cast

import httpx
import yfinance as yf

import app.services.brokers.upbit.client as upbit_service
from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling.analysis_crypto_score import (
    calculate_crypto_metrics_from_ohlcv,
)
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_rsi,
    _fetch_ohlcv_for_indicators,
    _normalize_crypto_symbol,
    compute_crypto_realtime_rsi_map,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.monitoring import build_yfinance_tracing_session
from app.services.krx import (
    classify_etf_category,
    fetch_etf_all_cached,
    fetch_stock_all_cached,
    fetch_valuation_all_cached,
)
from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerRateLimitError,
    TvScreenerService,
    TvScreenerTimeoutError,
    _import_tvscreener,
)
from app.utils.symbol_mapping import (
    SymbolMappingError,
    tradingview_to_upbit,
    upbit_to_tradingview,
)

logger = logging.getLogger(__name__)

DROP_THRESHOLD = -0.30
MARKET_PANIC = -0.10
CRYPTO_TOP_BY_VOLUME = 100
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
_UPBIT_DISPLAY_NAMES_FUNC = "get_upbit_market_display_names"
_UPBIT_WARNING_MARKETS_FUNC = "get_upbit_warning_markets"


async def get_upbit_market_display_names(
    markets: list[str],
) -> dict[str, dict[str, str | None]]:
    module = import_module("app.services.upbit_symbol_universe_service")
    fetch_display_names = getattr(module, _UPBIT_DISPLAY_NAMES_FUNC)
    return await fetch_display_names(markets)


async def get_upbit_warning_markets(
    *, quote_currency: str | None = None, fiat: str | None = None, db: Any = None
) -> set[str]:
    module = import_module("app.services.upbit_symbol_universe_service")
    fetch_warning_markets = getattr(module, _UPBIT_WARNING_MARKETS_FUNC)
    return await fetch_warning_markets(
        db=db,
        quote_currency=quote_currency,
        fiat=fiat,
    )


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if value != value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if value != value:
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
    if value != value:
        return ""
    return str(value).strip()


def _pick_display_name(row: Any) -> str:
    description = _clean_text(row.get("description"))
    if description:
        return description
    return _clean_text(row.get("name"))


def _resolve_crypto_display_name(
    upbit_symbol: str,
    row: Any,
    display_names: dict[str, dict[str, str | None]],
) -> str:
    display_name_data = display_names.get(upbit_symbol) if display_names else None
    for value in (
        display_name_data.get("korean_name") if display_name_data else None,
        display_name_data.get("english_name") if display_name_data else None,
        row.get("description"),
        row.get("name"),
        upbit_symbol,
    ):
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return upbit_symbol


def _tradingview_symbol_name(symbol: str) -> str:
    return symbol.split(":", maxsplit=1)[-1].strip().upper()


def _is_market_warning(value: Any) -> bool:
    if value is True:
        return True
    normalized = str(value or "").strip().upper()
    return normalized in {"CAUTION", "WARNING", "TRUE", "Y", "1"}


def _sort_crypto_by_rsi_bucket(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            int(item.get("rsi_bucket", 999)),
            -float(item.get("trade_amount_24h") or 0.0),
        ),
    )


def _sort_crypto_by_rsi_value(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            _to_optional_float(item.get("rsi"))
            if _to_optional_float(item.get("rsi")) is not None
            else 999.0,
            -float(item.get("trade_amount_24h") or 0.0),
        ),
    )


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


_CRYPTO_MARKET_CAP_CACHE = MarketCapCache(ttl=600)


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


def _normalize_dividend_yield_threshold(
    value: float | None,
) -> tuple[float | None, float | None]:
    """Normalize dividend yield threshold to decimal format."""
    if value is None:
        return None, None
    normalized_value = value / 100 if value >= 1 else value
    return value, normalized_value


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
        if sort_by == "rsi":
            raise ValueError("RSI sorting is only supported for crypto market")
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


# =============================================================================
# KR Screening Stage Functions
# =============================================================================


async def _stage_fetch_kr_candidates(
    market: str,
    asset_type: str | None,
    category: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 1: Fetch KR stock/ETF candidates based on market and asset_type.

    Args:
        market: Market filter ('kospi', 'kosdaq', or 'all')
        asset_type: Asset type filter ('stock', 'etf', or None for both)
        category: ETF category filter (only applies to ETFs)

    Returns:
        Tuple of (candidates list, filters_applied dict)
    """
    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    def copy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(item) for item in rows]

    # If category is specified without asset_type, default to ETF
    effective_asset_type = asset_type
    if category is not None and asset_type is None:
        effective_asset_type = "etf"
        filters_applied["asset_type"] = "etf"

    candidates: list[dict[str, Any]] = []

    # Fetch stocks if needed
    if effective_asset_type is None or effective_asset_type == "stock":
        if market == "kospi":
            candidates.extend(copy_rows(await fetch_stock_all_cached(market="STK")))
        elif market == "kosdaq":
            candidates.extend(copy_rows(await fetch_stock_all_cached(market="KSQ")))
        else:
            candidates.extend(copy_rows(await fetch_stock_all_cached(market="STK")))
            candidates.extend(copy_rows(await fetch_stock_all_cached(market="KSQ")))

    # Fetch ETFs if needed
    if effective_asset_type is None or effective_asset_type == "etf":
        etfs: list[dict[str, Any]] = []
        if market != "kosdaq":
            etfs = copy_rows(await fetch_etf_all_cached())

            for etf in etfs:
                etf["asset_type"] = "etf"
                categories = classify_etf_category(
                    etf["name"], etf.get("index_name", "")
                )
                etf["category"] = categories[0] if categories else "기타"
                etf["categories"] = categories

            if category:
                etfs = [
                    etf
                    for etf in etfs
                    if any(
                        cat.lower() == category.lower()
                        for cat in etf.get("categories", [])
                    )
                ]

        candidates.extend(etfs)

    # Set defaults for missing fields
    for item in candidates:
        if "change_rate" not in item:
            item["change_rate"] = 0.0
        if "market" not in item:
            item["market"] = "kr"
        if "asset_type" not in item:
            item["asset_type"] = "stock"

    # Fetch valuation data
    valuation_market = {"kospi": "STK", "kosdaq": "KSQ"}.get(market, "ALL")
    try:
        valuations = await fetch_valuation_all_cached(market=valuation_market)
        for item in candidates:
            code = item.get("short_code") or item.get("code", "")
            val = valuations.get(code, {})
            if item.get("per") is None:
                item["per"] = val.get("per")
            if item.get("pbr") is None:
                item["pbr"] = val.get("pbr")
            if item.get("dividend_yield") is None:
                item["dividend_yield"] = val.get("dividend_yield")
    except Exception:
        pass

    return candidates, filters_applied


def _stage_filter_kr(
    candidates: list[dict[str, Any]],
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None = None,
) -> list[dict[str, Any]]:
    """Stage 2: Apply basic numeric filters to KR candidates.

    Args:
        candidates: List of candidate items from fetch stage
        min_market_cap: Minimum market cap filter
        max_per: Maximum P/E ratio filter
        max_pbr: Maximum P/B ratio filter
        min_dividend_yield: Minimum dividend yield filter
        max_rsi: Maximum RSI filter (optional, typically applied after enrichment)

    Returns:
        Filtered list of candidates
    """
    return _apply_basic_filters(
        candidates,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
    )


async def _stage_enrich_kr_rsi(
    candidates: list[dict[str, Any]],
    enrich_rsi: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 3: Enrich KR candidates with RSI values.

    Args:
        candidates: List of filtered candidates
        enrich_rsi: Whether to perform RSI enrichment

    Returns:
        Tuple of (enriched candidates list, rsi_enrichment diagnostics dict)
    """
    rsi_enrichment = _empty_rsi_enrichment_diagnostics()

    if not enrich_rsi or not candidates:
        return candidates, rsi_enrichment

    rsi_enrichment["attempted"] = len(candidates)
    statuses = ["pending" for _ in candidates]
    errors: list[str | None] = [None for _ in candidates]
    semaphore = asyncio.Semaphore(10)

    async def calculate_rsi_for_stock(item: dict[str, Any], index: int):
        async with semaphore:
            item_copy = item.copy()
            symbol = item.get("short_code") or item.get("code")
            logger.info("[RSI-KR] Starting calculation for symbol: %s", symbol)

            if not symbol:
                logger.warning(
                    "[RSI-KR] No valid symbol found in item keys=%s",
                    sorted(item.keys()),
                )
                statuses[index] = "error"
                errors[index] = "No valid symbol found"
                return item_copy

            if item_copy.get("rsi") is not None:
                logger.debug(
                    "[RSI-KR] RSI already exists for %s, skipping recalculation",
                    symbol,
                )
                statuses[index] = "success"
                return item_copy

            try:
                logger.debug("[RSI-KR] Fetching OHLCV data for %s", symbol)
                df = await _fetch_ohlcv_for_indicators(symbol, "equity_kr", count=50)
                candle_count = len(df) if df is not None else 0
                logger.info("[RSI-KR] Got %d candles for %s", candle_count, symbol)

                if df is None or df.empty or "close" not in df.columns:
                    logger.warning(
                        "[RSI-KR] Missing OHLCV close data for %s (columns=%s)",
                        symbol,
                        list(df.columns) if df is not None else [],
                    )
                    statuses[index] = "error"
                    errors[index] = "Missing OHLCV close data"
                    return item_copy

                if candle_count < 14:
                    logger.warning(
                        "[RSI-KR] Insufficient candles (%d) for %s",
                        candle_count,
                        symbol,
                    )
                    statuses[index] = "error"
                    errors[index] = f"Insufficient candles ({candle_count})"
                    return item_copy

                logger.debug("[RSI-KR] Calculating RSI for %s", symbol)
                rsi_result = _calculate_rsi(df["close"])
                rsi_value = rsi_result.get("14") if rsi_result else None
                if rsi_value is None:
                    logger.warning(
                        "[RSI-KR] RSI calculation returned None for %s", symbol
                    )
                    statuses[index] = "error"
                    errors[index] = "RSI calculation returned None"
                    return item_copy

                item_copy["rsi"] = rsi_value
                statuses[index] = "success"
                logger.info("[RSI-KR] ✅ Success: %s RSI=%.2f", symbol, rsi_value)
            except Exception as exc:
                logger.error(
                    "[RSI-KR] ❌ Failed for %s: %s: %s",
                    symbol or "UNKNOWN",
                    type(exc).__name__,
                    exc,
                )
                statuses[index] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[index] = f"{type(exc).__name__}: {exc}"

            return item_copy

    try:
        subset_results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    calculate_rsi_for_stock(item, i)
                    for i, item in enumerate(candidates)
                ],
                return_exceptions=True,
            ),
            timeout=30.0,
        )
        for i, result in enumerate(subset_results):
            if isinstance(result, Exception):
                symbol = candidates[i].get("short_code") or candidates[i].get("code")
                logger.error(
                    "[RSI-KR] gather returned exception for %s: %s: %s",
                    symbol or "UNKNOWN",
                    type(result).__name__,
                    result,
                )
                statuses[i] = (
                    "rate_limited"
                    if isinstance(result, RateLimitExceededError)
                    else "error"
                )
                errors[i] = f"{type(result).__name__}: {result}"
                continue
            if not isinstance(result, dict):
                continue
            if result.get("rsi") is not None:
                candidates[i]["rsi"] = result["rsi"]
                if statuses[i] == "pending":
                    statuses[i] = "success"
            elif statuses[i] == "pending":
                statuses[i] = "error"
                errors[i] = "RSI calculation returned None"
    except TimeoutError:
        logger.warning("[RSI-KR] RSI enrichment timed out after 30 seconds")
        for i, status in enumerate(statuses):
            if status == "pending":
                statuses[i] = "timeout"
                errors[i] = "Timed out after 30 seconds"
    except Exception as exc:
        logger.error(
            "[RSI-KR] RSI enrichment batch failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for i, status in enumerate(statuses):
            if status == "pending":
                statuses[i] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[i] = f"{type(exc).__name__}: {exc}"
    finally:
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)

    return candidates, rsi_enrichment


def _stage_sort_kr(
    candidates: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
    limit: int,
    max_rsi: float | None = None,
) -> list[dict[str, Any]]:
    """Stage 4: Sort and limit KR candidates.

    Args:
        candidates: List of candidates (optionally enriched with RSI)
        sort_by: Field to sort by
        sort_order: Sort order ('asc' or 'desc')
        limit: Maximum number of results
        max_rsi: Optional RSI filter to apply before final limiting

    Returns:
        Sorted and limited list of candidates
    """
    # Apply RSI filter if specified
    if max_rsi is not None:
        filtered = _apply_basic_filters(
            candidates,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=max_rsi,
        )
    else:
        filtered = candidates

    # Sort and limit using the shared helper
    return _sort_and_limit(filtered, sort_by, sort_order, limit)


async def _screen_kr(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
    enrich_rsi: bool = True,
) -> dict[str, Any]:
    """Screen Korean market stocks/ETFs using staged pipeline.

    Orchestrates the following stages:
    1. Fetch candidates from KRX data sources
    2. Apply basic numeric filters (market cap, PER, PBR, dividend yield)
    3. Sort and determine subset for RSI enrichment
    4. Enrich subset with RSI values
    5. Apply RSI filter and final limit
    """
    # Normalize dividend yield threshold
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    # Stage 1: Fetch KR candidates
    candidates, filters_applied = await _stage_fetch_kr_candidates(
        market=market,
        asset_type=asset_type,
        category=category,
    )

    # Stage 2: Apply basic filters (excluding RSI)
    filtered_non_rsi = _stage_filter_kr(
        candidates=candidates,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield_normalized,
        max_rsi=None,
    )

    # Sort all filtered candidates to determine enrichment subset
    sorted_candidates = _sort_and_limit(
        filtered_non_rsi,
        sort_by,
        sort_order,
        len(filtered_non_rsi),
    )
    effective_max_rsi = max_rsi if enrich_rsi else None

    # Determine subset for RSI enrichment
    rsi_subset_limit = (
        min(len(sorted_candidates), limit)
        if effective_max_rsi is None
        else min(len(sorted_candidates), limit * 3, 150)
    )
    rsi_subset = sorted_candidates[:rsi_subset_limit]

    # Stage 3: Enrich subset with RSI
    rsi_subset, rsi_enrichment = await _stage_enrich_kr_rsi(
        candidates=rsi_subset,
        enrich_rsi=enrich_rsi,
    )

    # Merge RSI values back into sorted_candidates
    if rsi_subset:
        rsi_by_code = {
            item.get("short_code") or item.get("code"): item.get("rsi")
            for item in rsi_subset
            if item.get("rsi") is not None
        }
        for item in sorted_candidates:
            code = item.get("short_code") or item.get("code")
            if code in rsi_by_code and item.get("rsi") is None:
                item["rsi"] = rsi_by_code[code]

    # Build filters_applied for response
    filters_applied.update(
        {
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield_normalized,
            "max_rsi": max_rsi,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
    )
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    # Stage 4: Apply RSI filter and final limit
    if effective_max_rsi is not None:
        # Use enriched subset when RSI filter is applied
        results = _stage_sort_kr(
            candidates=rsi_subset,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            max_rsi=effective_max_rsi,
        )
        total_count = len(
            _apply_basic_filters(
                rsi_subset,
                min_market_cap=None,
                max_per=None,
                max_pbr=None,
                min_dividend_yield=None,
                max_rsi=effective_max_rsi,
            )
        )
    else:
        # Use all sorted candidates when no RSI filter
        results = _stage_sort_kr(
            candidates=sorted_candidates,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            max_rsi=None,
        )
        total_count = len(sorted_candidates)

    return _build_screen_response(
        results,
        total_count,
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
    )


async def _stage_fetch_us_candidates(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
    max_rsi: float | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 1: Fetch US stock candidates using yfinance screener.

    Args:
        market: Market identifier ('us')
        asset_type: Asset type filter (unused for US, always 'stock')
        category: Sector category filter
        min_market_cap: Minimum market cap filter
        max_per: Maximum P/E ratio filter
        min_dividend_yield: Minimum dividend yield filter (normalized to decimal)
        sort_by: Field to sort by
        sort_order: Sort order ('asc' or 'desc')
        limit: Maximum number of results
        max_rsi: Maximum RSI filter (affects fetch size for RSI enrichment)

    Returns:
        Tuple of (candidates list, filters_applied dict)

    Raises:
        ImportError: If yfinance screener module not available
        Exception: If yfinance API call fails
    """
    from yfinance.screener import EquityQuery

    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    conditions = []
    if min_market_cap is not None:
        conditions.append(
            EquityQuery(
                "gte",
                cast(Any, ["intradaymarketcap", min_market_cap]),
            )
        )
    if max_per is not None:
        conditions.append(
            EquityQuery(
                "lte",
                cast(Any, ["peratio.lasttwelvemonths", max_per]),
            )
        )
    if min_dividend_yield is not None:
        conditions.append(
            EquityQuery(
                "gte",
                cast(Any, ["forward_dividend_yield", min_dividend_yield]),
            )
        )
    if category:
        conditions.append(EquityQuery("eq", cast(Any, ["sector", category])))

    conditions.append(EquityQuery("eq", cast(Any, ["region", "us"])))
    if len(conditions) == 1:
        query = conditions[0]
    else:
        query = EquityQuery("and", conditions)

    sort_field_map = {
        "volume": "dayvolume",
        "market_cap": "intradaymarketcap",
        "change_rate": "percentchange",
        "dividend_yield": "forward_dividend_yield",
    }
    sort_field = sort_field_map.get(sort_by, "dayvolume")
    fetch_size = min(limit * 3, 150) if max_rsi is not None else limit
    session = build_yfinance_tracing_session()

    screen_result = await asyncio.to_thread(
        lambda: yf.screen(
            query,
            size=fetch_size,
            sortField=sort_field,
            sortAsc=(sort_order == "asc"),
            session=session,
        )
    )

    if screen_result is None:
        return [], filters_applied

    quotes = screen_result.get("quotes", []) if isinstance(screen_result, dict) else []
    if not quotes:
        return [], filters_applied

    def _first_value(quote: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = quote.get(key)
            if value is not None:
                return value
        return None

    candidates: list[dict[str, Any]] = []
    for quote in quotes:
        mapped = {
            "code": quote.get("symbol"),
            "name": _first_value(
                quote, "shortName", "longName", "shortname", "longname"
            ),
            "close": _first_value(
                quote, "regularMarketPrice", "lastPrice", "lastprice"
            ),
            "change_rate": _first_value(
                quote,
                "regularMarketChangePercent",
                "percentchange",
            )
            or 0,
            "volume": _first_value(
                quote, "regularMarketVolume", "dayVolume", "dayvolume"
            )
            or 0,
            "market_cap": _first_value(
                quote, "marketCap", "intradayMarketCap", "intradaymarketcap"
            )
            or 0,
            "per": _first_value(
                quote,
                "trailingPE",
                "forwardPE",
                "peRatio",
                "peratio",
            ),
            "dividend_yield": _first_value(
                quote,
                "dividendYield",
                "forwardDividendYield",
                "forward_dividend_yield",
            ),
            "market": "us",
        }
        # Drop rows without usable price; these often come from stale/partial screener rows.
        if mapped["close"] in (None, 0):
            continue
        candidates.append(mapped)

    return candidates, filters_applied


def _stage_filter_us(
    candidates: list[dict[str, Any]],
    max_rsi: float | None = None,
) -> list[dict[str, Any]]:
    """Stage 2: Apply post-fetch filters to US candidates.

    Note: Most filters (market_cap, per, dividend_yield) are applied in the
    yfinance screener query itself. This function applies RSI filter if needed.

    Args:
        candidates: List of candidate items from fetch stage
        max_rsi: Maximum RSI filter (applied after RSI enrichment)

    Returns:
        Filtered list of candidates
    """
    return _apply_basic_filters(
        candidates,
        min_market_cap=None,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        max_rsi=max_rsi,
    )


async def _stage_enrich_us_rsi(
    candidates: list[dict[str, Any]],
    enrich_rsi: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 3: Enrich US candidates with RSI values.

    Args:
        candidates: List of filtered candidates
        enrich_rsi: Whether to perform RSI enrichment

    Returns:
        Tuple of (enriched candidates list, rsi_enrichment diagnostics dict)
    """
    rsi_enrichment = _empty_rsi_enrichment_diagnostics()

    if not enrich_rsi or not candidates:
        return candidates, rsi_enrichment

    rsi_enrichment["attempted"] = len(candidates)
    statuses = ["pending" for _ in candidates]
    errors: list[str | None] = [None for _ in candidates]
    semaphore = asyncio.Semaphore(10)

    async def calculate_rsi_for_stock(item: dict[str, Any], index: int):
        async with semaphore:
            item_copy = item.copy()
            symbol = item.get("code")
            logger.info("[RSI-US] Starting calculation for symbol: %s", symbol)

            if not symbol:
                logger.warning(
                    "[RSI-US] No valid symbol found in item keys=%s",
                    sorted(item.keys()),
                )
                statuses[index] = "error"
                errors[index] = "No valid symbol found"
                return item_copy

            if item_copy.get("rsi") is not None:
                logger.debug(
                    "[RSI-US] RSI already exists for %s, skipping recalculation",
                    symbol,
                )
                statuses[index] = "success"
                return item_copy

            try:
                logger.debug("[RSI-US] Fetching OHLCV data for %s", symbol)
                df = await _fetch_ohlcv_for_indicators(symbol, "us", count=50)
                candle_count = len(df) if df is not None else 0
                logger.info("[RSI-US] Got %d candles for %s", candle_count, symbol)

                if df is None or df.empty or "close" not in df.columns:
                    logger.warning(
                        "[RSI-US] Missing OHLCV close data for %s (columns=%s)",
                        symbol,
                        list(df.columns) if df is not None else [],
                    )
                    statuses[index] = "error"
                    errors[index] = "Missing OHLCV close data"
                    return item_copy

                if candle_count < 14:
                    logger.warning(
                        "[RSI-US] Insufficient candles (%d) for %s",
                        candle_count,
                        symbol,
                    )
                    statuses[index] = "error"
                    errors[index] = f"Insufficient candles ({candle_count})"
                    return item_copy

                logger.debug("[RSI-US] Calculating RSI for %s", symbol)
                rsi_result = _calculate_rsi(df["close"])
                rsi_value = rsi_result.get("14") if rsi_result else None
                if rsi_value is None:
                    logger.warning(
                        "[RSI-US] RSI calculation returned None for %s", symbol
                    )
                    statuses[index] = "error"
                    errors[index] = "RSI calculation returned None"
                    return item_copy

                item_copy["rsi"] = rsi_value
                statuses[index] = "success"
                logger.info("[RSI-US] ✅ Success: %s RSI=%.2f", symbol, rsi_value)
            except Exception as exc:
                logger.error(
                    "[RSI-US] ❌ Failed for %s: %s: %s",
                    symbol or "UNKNOWN",
                    type(exc).__name__,
                    exc,
                )
                statuses[index] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[index] = f"{type(exc).__name__}: {exc}"

            return item_copy

    try:
        subset_results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    calculate_rsi_for_stock(item, i)
                    for i, item in enumerate(candidates)
                ],
                return_exceptions=True,
            ),
            timeout=30.0,
        )
        for i, result in enumerate(subset_results):
            if isinstance(result, Exception):
                symbol = candidates[i].get("code")
                logger.error(
                    "[RSI-US] gather returned exception for %s: %s: %s",
                    symbol or "UNKNOWN",
                    type(result).__name__,
                    result,
                )
                statuses[i] = (
                    "rate_limited"
                    if isinstance(result, RateLimitExceededError)
                    else "error"
                )
                errors[i] = f"{type(result).__name__}: {result}"
                continue
            if not isinstance(result, dict):
                continue
            if result.get("rsi") is not None:
                candidates[i]["rsi"] = result["rsi"]
                if statuses[i] == "pending":
                    statuses[i] = "success"
            elif statuses[i] == "pending":
                statuses[i] = "error"
                errors[i] = "RSI calculation returned None"
    except TimeoutError:
        logger.warning("[RSI-US] RSI enrichment timed out after 30 seconds")
        for i, status in enumerate(statuses):
            if status == "pending":
                statuses[i] = "timeout"
                errors[i] = "Timed out after 30 seconds"
    except Exception as exc:
        logger.error(
            "[RSI-US] RSI enrichment batch failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for i, status in enumerate(statuses):
            if status == "pending":
                statuses[i] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[i] = f"{type(exc).__name__}: {exc}"
    finally:
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)

    return candidates, rsi_enrichment


def _stage_sort_us(
    candidates: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
    limit: int,
    max_rsi: float | None = None,
) -> list[dict[str, Any]]:
    """Stage 4: Sort and limit US candidates.

    Args:
        candidates: List of candidates (optionally enriched with RSI)
        sort_by: Field to sort by
        sort_order: Sort order ('asc' or 'desc')
        limit: Maximum number of results
        max_rsi: Optional RSI filter to apply before final limiting

    Returns:
        Sorted and limited list of candidates
    """
    # Apply RSI filter if specified
    if max_rsi is not None:
        filtered = _apply_basic_filters(
            candidates,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=max_rsi,
        )
    else:
        filtered = candidates

    # Sort and limit using the shared helper
    return _sort_and_limit(filtered, sort_by, sort_order, limit)


async def _screen_us(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
    enrich_rsi: bool = True,
) -> dict[str, Any]:
    """Screen US market stocks using staged pipeline.

    Orchestrates the following stages:
    1. Fetch candidates from yfinance screener
    2. Apply basic filters (RSI filter applied after enrichment)
    3. Enrich with RSI values
    4. Apply final RSI filter and limit
    """
    # Normalize dividend yield threshold
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    # Stage 1: Fetch US candidates
    try:
        candidates, filters_applied = await _stage_fetch_us_candidates(
            market=market,
            asset_type=asset_type,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            min_dividend_yield=min_dividend_yield_normalized,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            max_rsi=max_rsi,
        )
    except ImportError:
        return _error_payload(
            source="yfinance",
            message="yfinance screener module not available. Install latest version of yfinance.",
        )
    except Exception as exc:
        return _error_payload(
            source="yfinance",
            message=str(exc),
        )

    if not candidates:
        filters_applied.update(
            {
                "min_market_cap": min_market_cap,
                "max_per": max_per,
                "min_dividend_yield": min_dividend_yield_normalized,
                "max_rsi": max_rsi,
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
        )
        if min_dividend_yield_input is not None:
            filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
        if min_dividend_yield_normalized is not None:
            filters_applied["min_dividend_yield_normalized"] = (
                min_dividend_yield_normalized
            )
        return _build_screen_response([], 0, filters_applied, market)

    # Stage 2: Apply basic filters (for US, most filters applied in yfinance query)
    # Note: RSI filter is applied after enrichment
    filtered = _stage_filter_us(
        candidates=candidates,
        max_rsi=None,  # Don't apply RSI filter yet
    )
    effective_max_rsi = max_rsi if enrich_rsi else None

    # Stage 3: Enrich with RSI
    enriched_candidates, rsi_enrichment = await _stage_enrich_us_rsi(
        candidates=filtered,
        enrich_rsi=enrich_rsi,
    )

    # Build filters_applied for response
    filters_applied.update(
        {
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "min_dividend_yield": min_dividend_yield_normalized,
            "max_rsi": max_rsi,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
    )
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    # Stage 4: Sort and apply final RSI filter
    results = _stage_sort_us(
        candidates=enriched_candidates,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        max_rsi=effective_max_rsi,
    )

    # Calculate total count before final limiting
    if effective_max_rsi is not None:
        total_count = len(
            _apply_basic_filters(
                enriched_candidates,
                min_market_cap=None,
                max_per=None,
                max_pbr=None,
                min_dividend_yield=None,
                max_rsi=effective_max_rsi,
            )
        )
    else:
        total_count = len(enriched_candidates)

    return _build_screen_response(
        results,
        total_count,
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
    )


# =============================================================================
# Crypto Screening Stage Functions
# =============================================================================


async def _stage_fetch_crypto_candidates(
    market: str,
    asset_type: str | None,
    category: str | None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    float,
    set[str],
    dict[str, Any],
    list[str],
]:
    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }
    warnings: list[str] = []

    all_candidates = await upbit_service.fetch_top_traded_coins(fiat="KRW")
    top_candidates = all_candidates[:CRYPTO_TOP_BY_VOLUME]

    # Get BTC change rate for crash detection
    btc_change_24h = 0.0
    btc_item = next(
        (
            item
            for item in all_candidates
            if str(item.get("market") or "").upper() == "KRW-BTC"
        ),
        None,
    )
    if btc_item is not None:
        btc_change_24h = _to_optional_float(
            btc_item.get("signed_change_rate")
            if btc_item.get("signed_change_rate") is not None
            else btc_item.get("change_rate")
        )
        if btc_change_24h is None:
            btc_change_24h = 0.0
            warnings.append(
                "KRW-BTC change rate missing; btc_change_24h=0.0 fallback applied for crash detection."
            )
    else:
        warnings.append(
            "KRW-BTC ticker missing; btc_change_24h=0.0 fallback applied for crash detection."
        )

    # Get warning markets
    warning_markets: set[str] = set()
    try:
        warning_markets = await get_upbit_warning_markets(quote_currency="KRW")
    except Exception as exc:
        warnings.append(
            "warning filter skipped: "
            f"get_upbit_warning_markets failed ({type(exc).__name__}: {exc})"
        )

    return (
        top_candidates,
        all_candidates,
        btc_change_24h,
        warning_markets,
        filters_applied,
        warnings,
    )


def _stage_filter_crypto(
    top_candidates: list[dict[str, Any]],
    btc_change_24h: float,
    warning_markets: set[str],
    min_market_cap: float | None,
) -> tuple[list[dict[str, Any]], int, int, list[str]]:
    """Stage 2: Filter crypto candidates by warning markets and crash detection.

    Args:
        top_candidates: List of top traded coins from fetch stage
        btc_change_24h: BTC 24h change rate for crash detection
        warning_markets: Set of warning market codes to exclude
        min_market_cap: Minimum market cap filter (not supported for crypto)

    Returns:
        Tuple of (
            candidates: Filtered list of candidates,
            filtered_by_warning: Count of items filtered by warning status,
            filtered_by_crash: Count of items filtered by crash detection,
            warnings: List of warning messages
        )
    """
    warnings: list[str] = []
    filtered_by_warning = 0
    filtered_by_crash = 0
    candidates: list[dict[str, Any]] = []

    for raw_item in top_candidates:
        market_code = str(raw_item.get("market") or "").strip().upper()
        if market_code in warning_markets:
            filtered_by_warning += 1
            continue

        coin_change_24h = raw_item.get("signed_change_rate")
        if coin_change_24h is None:
            coin_change_24h = raw_item.get("change_rate")
        if not is_safe_drop(coin_change_24h, btc_change_24h):
            filtered_by_crash += 1
            continue

        volume_24h = _to_optional_float(
            raw_item.get("acc_trade_volume_24h") or raw_item.get("volume")
        )
        trade_amount_24h = _to_optional_float(
            raw_item.get("trade_amount_24h") or raw_item.get("acc_trade_price_24h")
        )

        item = dict(raw_item)
        item["original_market"] = raw_item.get("market")
        item["market"] = "crypto"
        if market_code:
            item["symbol"] = market_code
        item["name"] = (
            raw_item.get("name")
            or raw_item.get("korean_name")
            or raw_item.get("english_name")
        )
        item["change_rate"] = (
            _to_optional_float(
                raw_item.get("change_rate")
                if raw_item.get("change_rate") is not None
                else raw_item.get("signed_change_rate")
            )
            or 0.0
        )
        item["trade_amount_24h"] = trade_amount_24h or 0.0
        item.pop("volume", None)
        item["market_cap"] = None
        item["market_cap_rank"] = None
        item["market_warning"] = None
        item["rsi"] = _to_optional_float(raw_item.get("rsi"))
        item["volume_24h"] = volume_24h or 0.0
        item["volume_ratio"] = _to_optional_float(raw_item.get("volume_ratio"))
        item["candle_type"] = raw_item.get("candle_type") or "flat"
        item["adx"] = _to_optional_float(raw_item.get("adx"))
        item["plus_di"] = _to_optional_float(raw_item.get("plus_di"))
        item["minus_di"] = _to_optional_float(raw_item.get("minus_di"))
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        item.pop("score", None)
        candidates.append(item)

    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    return candidates, filtered_by_warning, filtered_by_crash, warnings


async def _stage_enrich_crypto_indicators(
    candidates: list[dict[str, Any]],
    enrich_rsi: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[str]]:
    """Stage 3: Enrich crypto candidates with RSI and market cap data.

    Runs RSI enrichment via CryptoScreener and CoinGecko market cap fetch in parallel.

    Args:
        candidates: List of filtered candidates
        enrich_rsi: Whether to perform RSI enrichment

    Returns:
        Tuple of (
            candidates: Enriched candidates (modified in place),
            rsi_enrichment: RSI enrichment diagnostics,
            coingecko_payload: CoinGecko market cap data and metadata,
            warnings: List of warning messages
        )
    """
    warnings: list[str] = []

    async def _run_rsi_enrichment() -> dict[str, Any]:
        if not enrich_rsi or not candidates:
            return _empty_rsi_enrichment_diagnostics()
        try:
            return await _enrich_crypto_indicators(candidates)
        except Exception as exc:
            warnings.append(
                f"Crypto RSI enrichment failed: {type(exc).__name__}: {exc}; partial results returned"
            )
            return _empty_rsi_enrichment_diagnostics()

    try:
        parallel_results = await asyncio.gather(
            _run_rsi_enrichment(),
            _CRYPTO_MARKET_CAP_CACHE.get(),
        )
        if len(parallel_results) == 2:
            rsi_enrichment = parallel_results[0]
            coingecko_payload = parallel_results[1]
        else:
            warnings.append(
                "Crypto enrichment parallel execution returned unexpected shape; "
                "partial results returned"
            )
            rsi_enrichment = _empty_rsi_enrichment_diagnostics()
            coingecko_payload = {
                "data": {},
                "cached": False,
                "age_seconds": None,
                "stale": False,
                "error": "parallel_result_shape_error",
            }
    except Exception as exc:
        warnings.append(
            "Crypto enrichment parallel execution failed; partial results returned "
            f"({type(exc).__name__}: {exc})"
        )
        rsi_enrichment = _empty_rsi_enrichment_diagnostics()
        coingecko_payload = {
            "data": {},
            "cached": False,
            "age_seconds": None,
            "stale": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    timeout_count = int(rsi_enrichment.get("timeout", 0) or 0)
    if timeout_count > 0:
        warnings.append(
            f"Crypto RSI enrichment timed out for {timeout_count} symbols; partial results returned"
        )

    rate_limited_count = int(rsi_enrichment.get("rate_limited", 0) or 0)
    if rate_limited_count > 0:
        warnings.append(
            "Crypto RSI enrichment hit rate limits for "
            f"{rate_limited_count} symbols; partial results returned"
        )

    # Update candidates with CoinGecko market cap data
    coingecko_data = cast(
        dict[str, dict[str, Any]],
        coingecko_payload.get("data") or {},
    )
    for item in candidates:
        symbol = _extract_market_symbol(
            item.get("symbol") or item.get("original_market")
        )
        cap_data = coingecko_data.get(symbol or "") if symbol else None
        if cap_data:
            item["market_cap"] = cap_data.get("market_cap")
            item["market_cap_rank"] = cap_data.get("market_cap_rank")
        else:
            item["market_cap"] = None
            item["market_cap_rank"] = None
        item["market_warning"] = None
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        item.pop("score", None)

    coingecko_error = coingecko_payload.get("error")
    if coingecko_error:
        if coingecko_payload.get("stale"):
            warnings.append(
                "CoinGecko market-cap refresh failed; stale cache was used."
            )
        else:
            warnings.append(
                "CoinGecko market-cap data unavailable; market_cap fields remain null."
            )

    return candidates, rsi_enrichment, coingecko_payload, warnings


def _stage_sort_crypto(
    candidates: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
    limit: int,
    max_rsi: float | None = None,
) -> tuple[list[dict[str, Any]], int, str, list[str]]:
    """Stage 4: Sort and limit crypto candidates.

    Args:
        candidates: List of enriched candidates
        sort_by: Field to sort by
        sort_order: Sort order ('asc' or 'desc')
        limit: Maximum number of results
        max_rsi: Optional RSI filter to apply before final limiting

    Returns:
        Tuple of (
            results: Sorted and limited list of candidates,
            total_count: Count before limiting (after RSI filter if applied),
            applied_sort_order: Actual sort order used (may differ from input for RSI sort),
            warnings: List of warning messages
        )
    """
    warnings: list[str] = []

    # Apply RSI filter if specified
    if max_rsi is not None:
        filtered = [
            item
            for item in candidates
            if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
        ]
    else:
        filtered = candidates

    applied_sort_order = sort_order

    # Sort by RSI bucket or other criteria
    if sort_by == "rsi":
        if sort_order == "desc":
            warnings.append(
                "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
            )
        applied_sort_order = "asc"
        ordered = _sort_crypto_by_rsi_value(filtered)
    else:
        ordered = _sort_and_limit(filtered, sort_by, sort_order, len(filtered))

    results = ordered[:limit]
    for item in results:
        item.pop("score", None)

    return results, len(filtered), applied_sort_order, warnings


async def _enrich_crypto_indicators(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich crypto candidates with RSI, ADX, and volume using CryptoScreener.

    This function uses the tvscreener library to bulk query technical indicators
    from TradingView instead of manually calculating them. Symbol conversion from
    Upbit format (KRW-BTC) to TradingView format (UPBIT:BTCKRW) is handled
    automatically.

    Parameters
    ----------
    candidates : list[dict[str, Any]]
        List of candidate dictionaries to enrich with indicator values

    Returns
    -------
    dict[str, Any]
        Enrichment diagnostics with counts of succeeded/failed/timeout cases
    """
    rsi_enrichment = _empty_rsi_enrichment_diagnostics()
    if not candidates:
        return rsi_enrichment

    rsi_enrichment["attempted"] = len(candidates)
    statuses = ["pending" for _ in candidates]
    errors: list[str | None] = [None for _ in candidates]

    # Map Upbit symbols to TradingView format and track indices
    symbols_by_index: list[str | None] = [None for _ in candidates]
    tv_symbols_by_index: list[str | None] = [None for _ in candidates]
    tv_symbols_to_upbit: dict[str, str] = {}  # TradingView -> Upbit mapping
    unique_tv_symbols: list[str] = []
    seen_tv_symbols: set[str] = set()

    for index, item in enumerate(candidates):
        # Skip if RSI already exists
        if item.get("rsi") is not None:
            statuses[index] = "success"
            continue

        # Extract and normalize Upbit symbol
        symbol = item.get("original_market") or item.get("symbol") or item.get("market")
        normalized_symbol = _normalize_crypto_symbol(str(symbol or ""))
        if not normalized_symbol:
            statuses[index] = "error"
            errors[index] = "No valid symbol found"
            continue

        symbols_by_index[index] = normalized_symbol

        # Convert to TradingView format
        try:
            tv_symbol = upbit_to_tradingview(normalized_symbol)
            tv_symbols_by_index[index] = tv_symbol

            # Track unique TradingView symbols for batch query
            if tv_symbol not in seen_tv_symbols:
                seen_tv_symbols.add(tv_symbol)
                unique_tv_symbols.append(tv_symbol)
                tv_symbols_to_upbit[tv_symbol] = normalized_symbol

        except SymbolMappingError as exc:
            statuses[index] = "error"
            errors[index] = f"Symbol mapping failed: {exc}"
            logger.warning(
                "[Indicators-Crypto] Failed to map symbol %s: %s",
                normalized_symbol,
                exc,
            )
            continue

    # Query CryptoScreener for indicators if we have symbols to query
    if not unique_tv_symbols:
        logger.info("[Indicators-Crypto] No symbols to enrich with CryptoScreener")
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)
        return rsi_enrichment

    try:
        # Use CryptoScreener to bulk query indicators from TradingView
        logger.info(
            "[Indicators-Crypto] Querying CryptoScreener for %d symbols",
            len(unique_tv_symbols),
        )

        tvscreener_service = TvScreenerService(timeout=30.0)

        try:
            tvscreener = _import_tvscreener()
            CryptoField = tvscreener.CryptoField

            columns = [CryptoField.NAME, CryptoField.RELATIVE_STRENGTH_INDEX_14]

            try:
                adx_field = CryptoField.AVERAGE_DIRECTIONAL_INDEX_14
                columns.append(adx_field)
                has_adx = True
                logger.debug(
                    "[Indicators-Crypto] ADX field available for CryptoScreener"
                )
            except AttributeError:
                has_adx = False
                logger.info(
                    "[Indicators-Crypto] ADX field not available for CryptoScreener, skipping"
                )

            try:
                volume_field = CryptoField.VOLUME_24H_IN_USD
                columns.append(volume_field)
                has_volume = True
            except AttributeError:
                has_volume = False
                logger.warning(
                    "[Indicators-Crypto] VOLUME field not available for CryptoScreener"
                )

            requested_tv_names = [
                _tradingview_symbol_name(symbol)
                for symbol in unique_tv_symbols
                if _tradingview_symbol_name(symbol)
            ]
            where_conditions = []
            try:
                where_conditions.append(CryptoField.EXCHANGE == "UPBIT")
            except AttributeError:
                logger.warning(
                    "[Indicators-Crypto] EXCHANGE field not available for CryptoScreener"
                )

            if requested_tv_names:
                try:
                    where_conditions.append(CryptoField.NAME.isin(requested_tv_names))
                except AttributeError:
                    logger.warning(
                        "[Indicators-Crypto] NAME.isin not available for CryptoScreener"
                    )

            df = await tvscreener_service.query_crypto_screener(
                columns=columns,
                where_clause=where_conditions,
                limit=300,
            )

            rsi_map: dict[str, float | None] = {}
            adx_map: dict[str, float | None] = {}
            volume_map: dict[str, float | None] = {}

            if not df.empty:
                for _, row in df.iterrows():
                    tradingview_symbol = str(row.get("symbol", "")).strip().upper()
                    if not tradingview_symbol:
                        continue
                    try:
                        upbit_symbol = tradingview_to_upbit(tradingview_symbol)
                    except SymbolMappingError:
                        continue
                    if upbit_symbol not in tv_symbols_to_upbit.values():
                        continue

                    rsi_value = _to_optional_float(
                        row.get("relative_strength_index_14")
                    )
                    rsi_map[upbit_symbol] = rsi_value

                    if has_adx:
                        adx_value = _to_optional_float(
                            row.get("average_directional_index_14")
                        )
                        adx_map[upbit_symbol] = adx_value

                    if has_volume:
                        volume_value = _to_optional_float(row.get("volume_24h_in_usd"))
                        volume_map[upbit_symbol] = volume_value

            logger.info(
                "[Indicators-Crypto] CryptoScreener returned data for %d/%d symbols "
                "(RSI: %d, ADX: %d, Volume: %d)",
                len(rsi_map),
                len(unique_tv_symbols),
                len(rsi_map),
                len(adx_map) if has_adx else 0,
                len(volume_map) if has_volume else 0,
            )

        except ImportError:
            logger.warning(
                "[Indicators-Crypto] tvscreener not installed, falling back to manual calculation for RSI"
            )
            # Fallback to manual calculation if tvscreener is not available
            batch_symbols: list[str] = [
                symbol for symbol in symbols_by_index if symbol is not None
            ]
            rsi_map_manual = await asyncio.wait_for(
                compute_crypto_realtime_rsi_map(batch_symbols),
                timeout=30.0,
            )
            # Convert manual RSI map (Upbit symbols) to match our data structure
            rsi_map = {}
            adx_map = {}
            volume_map = {}
            for _, upbit_symbol in tv_symbols_to_upbit.items():
                if upbit_symbol in rsi_map_manual:
                    rsi_map[upbit_symbol] = rsi_map_manual[upbit_symbol]

        # Apply indicator values to candidates
        for index, item in enumerate(candidates):
            if statuses[index] != "pending":
                continue

            upbit_symbol = symbols_by_index[index]
            if upbit_symbol is None:
                statuses[index] = "error"
                errors[index] = "No valid Upbit symbol"
                continue

            rsi_value = rsi_map.get(upbit_symbol)
            item["rsi"] = rsi_value
            item["rsi_bucket"] = _compute_rsi_bucket(rsi_value)

            if upbit_symbol in adx_map:
                item["adx"] = adx_map[upbit_symbol]

            if upbit_symbol in volume_map:
                item["volume_24h"] = volume_map[upbit_symbol]

            if rsi_value is None:
                statuses[index] = "error"
                errors[index] = "RSI not found in CryptoScreener results"
            else:
                statuses[index] = "success"

    except TvScreenerTimeoutError as exc:
        logger.warning(
            "[Indicators-Crypto] Indicator enrichment timed out: %s",
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "timeout"
                errors[index] = f"CryptoScreener timeout: {exc}"
    except TvScreenerRateLimitError as exc:
        logger.error(
            "[Indicators-Crypto] CryptoScreener rate limited: %s",
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "rate_limited"
                errors[index] = f"CryptoScreener error: {exc}"
    except TvScreenerError as exc:
        logger.error(
            "[Indicators-Crypto] CryptoScreener query failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = (
                    "rate_limited" if "rate limit" in str(exc).lower() else "error"
                )
                errors[index] = f"CryptoScreener error: {exc}"
    except TimeoutError:
        logger.warning(
            "[Indicators-Crypto] Indicator enrichment timed out after 30 seconds"
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "timeout"
                errors[index] = "Timed out after 30 seconds"
    except Exception as exc:
        logger.error(
            "[Indicators-Crypto] Indicator enrichment batch failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[index] = f"{type(exc).__name__}: {exc}"
    finally:
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)

    return rsi_enrichment


async def _screen_kr_via_tvscreener(
    market: str = "kr",
    asset_type: str | None = "stock",
    category: str | None = None,
    min_market_cap: float | None = None,
    max_per: float | None = None,
    max_pbr: float | None = None,
    min_dividend_yield: float | None = None,
    min_rsi: float | None = None,
    max_rsi: float | None = None,
    min_adx: float | None = None,
    sort_by: str = "rsi",
    sort_order: str = "desc",
    limit: int = 50,
) -> dict[str, Any]:
    """Screen Korean stocks using TradingView StockScreener API.

    This function uses the tvscreener library to query Korean stocks from
    TradingView with technical indicators (RSI, ADX, volume, price) instead of
    relying on KRX data and manual indicator calculation.

    Parameters
    ----------
    min_rsi : float | None, optional
        Minimum RSI_14 value filter (default: None)
    max_rsi : float | None, optional
        Maximum RSI_14 value filter (default: None)
    min_adx : float | None, optional
        Minimum ADX_14 value filter (default: None)
    sort_by : str, optional
        Sort field - one of 'rsi', 'adx', 'volume', 'change' (default: 'rsi')
    limit : int, optional
        Maximum number of results to return (default: 50)

    Returns
    -------
    dict[str, Any]
        Dictionary containing:
        - stocks: List of stock dictionaries with indicator values
        - source: Data source identifier ('tvscreener')
        - count: Number of results returned
        - filters_applied: Dictionary of applied filter parameters
        - error: Error message if query failed (None on success)
    """
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    result: dict[str, Any] = {
        "stocks": [],
        "source": "tvscreener",
        "count": 0,
        "filters_applied": {
            "market": market,
            "asset_type": asset_type,
            "category": category,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield_normalized,
            "min_rsi": min_rsi,
            "max_rsi": max_rsi,
            "min_adx": min_adx,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
        },
        "error": None,
    }
    if min_dividend_yield_input is not None:
        result["filters_applied"]["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        result["filters_applied"]["min_dividend_yield_normalized"] = (
            min_dividend_yield_normalized
        )

    try:
        try:
            tvscreener = _import_tvscreener()
            StockField = tvscreener.StockField
            Market = tvscreener.Market
        except ImportError:
            error_msg = "tvscreener library not installed, cannot use StockScreener"
            logger.warning("[Screen-KR-TV] %s", error_msg)
            result["error"] = error_msg
            return result

        columns = [
            StockField.ACTIVE_SYMBOL,
            StockField.DESCRIPTION,
            StockField.NAME,
            StockField.PRICE,
            StockField.RELATIVE_STRENGTH_INDEX_14,
            StockField.AVERAGE_DIRECTIONAL_INDEX_14,
            StockField.VOLUME,
            StockField.CHANGE_PERCENT,
        ]

        try:
            columns.append(StockField.COUNTRY)
        except AttributeError:
            logger.warning("[Screen-KR-TV] COUNTRY field not available in StockField")

        where_conditions = []

        if min_rsi is not None:
            where_conditions.append(StockField.RELATIVE_STRENGTH_INDEX_14 >= min_rsi)
        if max_rsi is not None:
            where_conditions.append(StockField.RELATIVE_STRENGTH_INDEX_14 <= max_rsi)

        if min_adx is not None:
            where_conditions.append(StockField.AVERAGE_DIRECTIONAL_INDEX_14 >= min_adx)

        logger.info(
            "[Screen-KR-TV] Querying StockScreener for Korean stocks "
            "(filters: min_rsi=%s, max_rsi=%s, min_adx=%s, limit=%d)",
            min_rsi,
            max_rsi,
            min_adx,
            limit,
        )

        tvscreener_service = TvScreenerService(timeout=30.0)
        df = await tvscreener_service.query_stock_screener(
            columns=columns,
            where_clause=where_conditions,
            country=None,
            markets=[Market.KOREA],
            limit=None,
        )

        if df.empty:
            logger.info("[Screen-KR-TV] StockScreener returned no results")
            return result

        logger.info("[Screen-KR-TV] StockScreener returned %d Korean stocks", len(df))

        market_codes, valuation_market = _kr_market_codes(market)
        universe_rows: list[dict[str, Any]] = []
        for market_code in market_codes:
            universe_rows.extend(await fetch_stock_all_cached(market=market_code))

        allowed_by_code: dict[str, dict[str, Any]] = {}
        for item in universe_rows:
            code = str(item.get("short_code") or item.get("code") or "").strip().upper()
            if code:
                allowed_by_code[code] = dict(item)

        valuations: dict[str, dict[str, Any]] = {}
        try:
            valuations = await fetch_valuation_all_cached(market=valuation_market)
        except Exception:
            valuations = {}

        stocks = []
        for _, row in df.iterrows():
            code = _extract_kr_stock_code(row.get("symbol"))
            if not code or code not in allowed_by_code:
                continue

            base = allowed_by_code[code]
            valuation = valuations.get(code, {})
            stock = {
                "symbol": code,
                "short_code": code,
                "code": base.get("code") or code,
                "name": _pick_display_name(row),
                "price": _to_optional_float(row.get("price")),
                "rsi": _to_optional_float(row.get("relative_strength_index_14")),
                "adx": _to_optional_float(row.get("average_directional_index_14")),
                "volume": _to_optional_float(row.get("volume")),
                "change_percent": _to_optional_float(row.get("change_percent")),
                "market_cap": _to_optional_float(base.get("market_cap")),
                "per": _to_optional_float(valuation.get("per")),
                "pbr": _to_optional_float(valuation.get("pbr")),
                "dividend_yield": _to_optional_float(valuation.get("dividend_yield")),
                "market": base.get("market") or market,
                "country": str(row.get("country", "")).strip()
                if "country" in row
                else "South Korea",
            }
            stock["change_rate"] = stock["change_percent"]
            if not stock["name"]:
                stock["name"] = str(base.get("name") or "").strip()
            stocks.append(stock)

        filtered = _apply_basic_filters(
            stocks,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield_normalized,
            max_rsi=max_rsi,
        )
        ordered = _sort_and_limit(filtered, sort_by, sort_order, limit)

        result["count"] = len(filtered)
        result["stocks"] = ordered

        logger.info(
            "[Screen-KR-TV] Returning %d Korean stocks sorted by %s",
            len(stocks),
            sort_by,
        )

        return result

    except TvScreenerError as exc:
        error_msg = f"StockScreener query failed: {exc}"
        logger.error("[Screen-KR-TV] %s", error_msg)
        result["error"] = error_msg
        return result

    except TimeoutError:
        error_msg = "StockScreener query timed out after 30 seconds"
        logger.warning("[Screen-KR-TV] %s", error_msg)
        result["error"] = error_msg
        return result

    except Exception as exc:
        error_msg = (
            f"Unexpected error in Korean stock screening: {type(exc).__name__}: {exc}"
        )
        logger.error("[Screen-KR-TV] %s", error_msg)
        result["error"] = error_msg
        return result


async def _screen_us_via_tvscreener(
    market: str = "us",
    asset_type: str | None = None,
    category: str | None = None,
    min_market_cap: float | None = None,
    max_per: float | None = None,
    min_dividend_yield: float | None = None,
    min_rsi: float | None = None,
    max_rsi: float | None = None,
    min_adx: float | None = None,
    sort_by: str = "rsi",
    sort_order: str = "desc",
    limit: int = 50,
) -> dict[str, Any]:
    """Screen US stocks using TradingView StockScreener API.

    This function uses the tvscreener library to query US stocks from
    TradingView with technical indicators (RSI, ADX, volume, price) instead of
    relying on yfinance screener and manual indicator calculation.

    Parameters
    ----------
    min_rsi : float | None, optional
        Minimum RSI_14 value filter (default: None)
    max_rsi : float | None, optional
        Maximum RSI_14 value filter (default: None)
    min_adx : float | None, optional
        Minimum ADX_14 value filter (default: None)
    sort_by : str, optional
        Sort field - one of 'rsi', 'adx', 'volume', 'change' (default: 'rsi')
    limit : int, optional
        Maximum number of results to return (default: 50)

    Returns
    -------
    dict[str, Any]
        Dictionary containing:
        - stocks: List of stock dictionaries with indicator values
        - source: Data source identifier ('tvscreener')
        - count: Number of results returned
        - filters_applied: Dictionary of applied filter parameters
        - error: Error message if query failed (None on success)
    """
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    result: dict[str, Any] = {
        "stocks": [],
        "source": "tvscreener",
        "count": 0,
        "filters_applied": {
            "market": market,
            "asset_type": asset_type,
            "category": category,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "min_dividend_yield": min_dividend_yield_normalized,
            "min_rsi": min_rsi,
            "max_rsi": max_rsi,
            "min_adx": min_adx,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
        },
        "error": None,
    }
    if min_dividend_yield_input is not None:
        result["filters_applied"]["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        result["filters_applied"]["min_dividend_yield_normalized"] = (
            min_dividend_yield_normalized
        )

    try:
        try:
            tvscreener = _import_tvscreener()
            StockField = tvscreener.StockField
            Market = tvscreener.Market
        except ImportError:
            error_msg = "tvscreener library not installed, cannot use StockScreener"
            logger.warning("[Screen-US-TV] %s", error_msg)
            result["error"] = error_msg
            return result

        market_cap_field = _get_tvscreener_attr(
            StockField,
            "MARKET_CAPITALIZATION",
            "MARKET_CAP_BASIC",
        )
        pe_field = _get_tvscreener_attr(
            StockField,
            "PRICE_TO_EARNINGS_RATIO_TTM",
            "PRICE_TO_EARNINGS_TTM",
        )
        dividend_field = _get_tvscreener_attr(
            StockField,
            "DIVIDEND_YIELD_FORWARD",
            "DIVIDEND_YIELD_RECENT",
        )

        if sort_by == "market_cap" and market_cap_field is None:
            result["error"] = "tvscreener market-cap field unavailable"
            return result
        if sort_by == "dividend_yield" and dividend_field is None:
            result["error"] = "tvscreener dividend-yield field unavailable"
            return result
        if min_market_cap is not None and market_cap_field is None:
            result["error"] = "tvscreener market-cap field unavailable"
            return result
        if max_per is not None and pe_field is None:
            result["error"] = "tvscreener PE field unavailable"
            return result
        if min_dividend_yield is not None and dividend_field is None:
            result["error"] = "tvscreener dividend-yield field unavailable"
            return result

        columns = [
            StockField.ACTIVE_SYMBOL,
            StockField.DESCRIPTION,
            StockField.NAME,
            StockField.PRICE,
            StockField.RELATIVE_STRENGTH_INDEX_14,
            StockField.AVERAGE_DIRECTIONAL_INDEX_14,
            StockField.VOLUME,
            StockField.CHANGE_PERCENT,
        ]
        if market_cap_field is not None:
            columns.append(market_cap_field)
        if pe_field is not None:
            columns.append(pe_field)
        if dividend_field is not None:
            columns.append(dividend_field)

        try:
            columns.append(StockField.COUNTRY)
        except AttributeError:
            logger.warning("[Screen-US-TV] COUNTRY field not available in StockField")

        where_conditions = []

        if min_rsi is not None:
            where_conditions.append(StockField.RELATIVE_STRENGTH_INDEX_14 >= min_rsi)
        if max_rsi is not None:
            where_conditions.append(StockField.RELATIVE_STRENGTH_INDEX_14 <= max_rsi)

        if min_adx is not None:
            where_conditions.append(StockField.AVERAGE_DIRECTIONAL_INDEX_14 >= min_adx)
        if min_market_cap is not None and market_cap_field is not None:
            where_conditions.append(market_cap_field >= min_market_cap)
        if max_per is not None and pe_field is not None:
            where_conditions.append(pe_field <= max_per)
        if min_dividend_yield_normalized is not None and dividend_field is not None:
            where_conditions.append(dividend_field >= min_dividend_yield_normalized)

        logger.info(
            "[Screen-US-TV] Querying StockScreener for US stocks "
            "(filters: min_rsi=%s, max_rsi=%s, min_adx=%s, limit=%d)",
            min_rsi,
            max_rsi,
            min_adx,
            limit,
        )

        tvscreener_service = TvScreenerService(timeout=30.0)
        df = await tvscreener_service.query_stock_screener(
            columns=columns,
            where_clause=where_conditions,
            country="United States",
            markets=[Market.AMERICA],
            limit=None,
        )

        if df.empty:
            logger.info("[Screen-US-TV] StockScreener returned no results")
            return result

        logger.info("[Screen-US-TV] StockScreener returned %d US stocks", len(df))

        stocks = []
        for _, row in df.iterrows():
            price = _to_optional_float(row.get("price"))
            if price is None or price <= 0:
                continue
            stock = {
                "symbol": _strip_exchange_prefix(row.get("symbol")),
                "name": _pick_display_name(row),
                "price": price,
                "rsi": _to_optional_float(row.get("relative_strength_index_14")),
                "adx": _to_optional_float(row.get("average_directional_index_14")),
                "volume": _to_optional_float(row.get("volume")),
                "change_percent": _to_optional_float(row.get("change_percent")),
                "market_cap": _to_optional_float(
                    _get_first_present(row, "market_capitalization", "market_cap_basic")
                ),
                "per": _to_optional_float(
                    _get_first_present(
                        row,
                        "price_to_earnings_ratio_ttm",
                        "price_to_earnings_ttm",
                    )
                ),
                "dividend_yield": _to_optional_float(
                    _get_first_present(
                        row,
                        "dividend_yield_forward",
                        "dividend_yield_recent",
                    )
                ),
                "market": market,
                "country": str(row.get("country", "")).strip()
                if "country" in row
                else "United States",
            }
            stock["change_rate"] = stock["change_percent"]
            stocks.append(stock)

        filtered = _apply_basic_filters(
            stocks,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=None,
            min_dividend_yield=min_dividend_yield_normalized,
            max_rsi=max_rsi,
        )
        ordered = _sort_and_limit(filtered, sort_by, sort_order, limit)

        result["count"] = len(filtered)
        result["stocks"] = ordered

        logger.info(
            "[Screen-US-TV] Returning %d US stocks sorted by %s",
            len(stocks),
            sort_by,
        )

        return result

    except TvScreenerError as exc:
        error_msg = f"StockScreener query failed: {exc}"
        logger.error("[Screen-US-TV] %s", error_msg)
        result["error"] = error_msg
        return result

    except TimeoutError:
        error_msg = "StockScreener query timed out after 30 seconds"
        logger.warning("[Screen-US-TV] %s", error_msg)
        result["error"] = error_msg
        return result

    except Exception as exc:
        error_msg = (
            f"Unexpected error in US stock screening: {type(exc).__name__}: {exc}"
        )
        logger.error("[Screen-US-TV] %s", error_msg)
        result["error"] = error_msg
        return result


async def _screen_crypto(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
    enrich_rsi: bool = True,
) -> dict[str, Any]:
    """Screen crypto market using staged pipeline.

    Orchestrates the following stages:
    1. Fetch candidates from Upbit
    2. Filter by warning markets and crash detection
    3. Enrich with RSI and CoinGecko market cap data
    4. Sort and apply final limit
    """
    # Normalize dividend yield threshold
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    # Stage 1: Fetch crypto candidates
    (
        top_candidates,
        all_candidates,
        btc_change_24h,
        warning_markets,
        filters_applied,
        fetch_warnings,
    ) = await _stage_fetch_crypto_candidates(
        market=market,
        asset_type=asset_type,
        category=category,
    )

    total_markets = len(all_candidates)
    top_by_volume = len(top_candidates)

    # Stage 2: Filter by warning markets and crash detection
    candidates, filtered_by_warning, filtered_by_crash, filter_warnings = (
        _stage_filter_crypto(
            top_candidates=top_candidates,
            btc_change_24h=btc_change_24h,
            warning_markets=warning_markets,
            min_market_cap=min_market_cap,
        )
    )

    # Stage 3: Enrich with RSI and CoinGecko market cap data
    (
        candidates,
        rsi_enrichment,
        coingecko_payload,
        enrich_warnings,
    ) = await _stage_enrich_crypto_indicators(
        candidates=candidates,
        enrich_rsi=enrich_rsi,
    )

    # Stage 4: Sort and limit
    results, total_count, applied_sort_order, sort_warnings = _stage_sort_crypto(
        candidates=candidates,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        max_rsi=max_rsi,
    )

    # Aggregate all warnings
    all_warnings = fetch_warnings + filter_warnings + enrich_warnings + sort_warnings

    # Build filters_applied for response
    filters_applied.update(
        {
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "min_dividend_yield": min_dividend_yield_normalized,
            "max_rsi": max_rsi,
            "sort_by": sort_by,
            "sort_order": applied_sort_order,
        }
    )
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    meta_fields = {
        "total_markets": total_markets,
        "top_by_volume": top_by_volume,
        "filtered_by_warning": filtered_by_warning,
        "filtered_by_crash": filtered_by_crash,
        "rsi_enriched": int(rsi_enrichment.get("succeeded", 0) or 0),
        "final_count": len(results),
        "coingecko_cached": bool(coingecko_payload.get("cached")),
        "coingecko_age_seconds": coingecko_payload.get("age_seconds"),
    }

    return _build_screen_response(
        results,
        total_count,
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
        warnings=all_warnings if all_warnings else None,
        meta_fields=meta_fields,
    )


async def _screen_crypto_via_tvscreener(
    market: str,
    asset_type: str | None,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
) -> dict[str, Any]:
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
        "min_market_cap": min_market_cap,
        "max_per": max_per,
        "min_dividend_yield": min_dividend_yield_normalized,
        "max_rsi": max_rsi,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    tvscreener = _import_tvscreener()
    CryptoField = tvscreener.CryptoField

    value_traded_field = _get_tvscreener_attr(CryptoField, "VALUE_TRADED")
    if value_traded_field is None:
        raise TvScreenerError("CryptoScreener VALUE_TRADED field unavailable")
    description_field = _get_tvscreener_attr(CryptoField, "DESCRIPTION")
    market_cap_field = _get_tvscreener_attr(CryptoField, "MARKET_CAP")

    columns = [
        CryptoField.NAME,
        *([description_field] if description_field is not None else []),
        CryptoField.PRICE,
        CryptoField.CHANGE_PERCENT,
        value_traded_field,
        *([market_cap_field] if market_cap_field is not None else []),
        CryptoField.RELATIVE_STRENGTH_INDEX_14,
        CryptoField.AVERAGE_DIRECTIONAL_INDEX_14,
    ]
    volume_usd_field = _get_tvscreener_attr(CryptoField, "VOLUME_24H_IN_USD")
    if volume_usd_field is not None:
        columns.append(volume_usd_field)

    where_conditions = [CryptoField.EXCHANGE == "UPBIT"]
    if max_rsi is not None:
        where_conditions.append(CryptoField.RELATIVE_STRENGTH_INDEX_14 <= max_rsi)

    sort_field_map = {
        "trade_amount": value_traded_field,
        "market_cap": market_cap_field or value_traded_field,
        "rsi": CryptoField.RELATIVE_STRENGTH_INDEX_14,
        "change_rate": CryptoField.CHANGE_PERCENT,
    }
    sort_field = sort_field_map.get(sort_by, value_traded_field)
    query_limit = max(limit * 5, 50)

    tvscreener_service = TvScreenerService(timeout=30.0)
    df = await tvscreener_service.query_crypto_screener(
        columns=columns,
        where_clause=where_conditions,
        sort_by=sort_field,
        ascending=(sort_order == "asc"),
        limit=query_limit,
    )

    warnings: list[str] = []
    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    raw_results: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        tradingview_symbol = _clean_text(row.get("symbol")).upper()
        if not tradingview_symbol:
            continue
        try:
            upbit_symbol = tradingview_to_upbit(tradingview_symbol)
        except SymbolMappingError:
            continue

        raw_results.append(
            {
                "symbol": upbit_symbol,
                "market": upbit_symbol,
                "name": _clean_text(row.get("name")),
                "description": _clean_text(row.get("description")),
                "trade_price": _to_optional_float(row.get("price")),
                "signed_change_rate": _to_optional_float(row.get("change_percent")),
                "change_rate": _to_optional_float(row.get("change_percent")),
                "acc_trade_price_24h": _to_optional_float(row.get("value_traded")),
                "tv_market_cap": _to_optional_float(row.get("market_cap")),
                "rsi": _to_optional_float(row.get("relative_strength_index_14")),
                "adx": _to_optional_float(row.get("average_directional_index_14")),
                "tv_volume_24h_in_usd": _to_optional_float(
                    row.get("volume_24h_in_usd")
                ),
            }
        )

    market_codes = [
        str(item.get("symbol") or "").strip().upper() for item in raw_results
    ]
    try:
        display_names = await get_upbit_market_display_names(market_codes)
    except Exception as exc:
        display_names = {}
        warnings.append(
            "Upbit symbol-universe names unavailable; TradingView description/name fallback used "
            f"({type(exc).__name__}: {exc})"
        )

    ticker_volume_map: dict[str, float] = {}
    if market_codes:
        try:
            ticker_rows = await upbit_service.fetch_multiple_tickers(market_codes)
            ticker_volume_map = {
                str(row.get("market") or "").strip().upper(): (
                    _to_optional_float(row.get("acc_trade_volume_24h")) or 0.0
                )
                for row in ticker_rows
                if str(row.get("market") or "").strip()
            }
        except Exception as exc:
            warnings.append(
                "Upbit 24h volume enrichment failed; volume_24h defaulted to 0.0 "
                f"({type(exc).__name__}: {exc})"
            )

    btc_item = next(
        (
            item
            for item in raw_results
            if str(item.get("symbol") or "").upper() == "KRW-BTC"
        ),
        None,
    )
    btc_change_24h: float | None = None
    if btc_item is None:
        warnings.append(
            "KRW-BTC ticker not found; crash filter uses btc_change_24h=0.0 fallback."
        )
    else:
        btc_change_24h = _to_optional_float(
            btc_item.get("signed_change_rate") or btc_item.get("change_rate")
        )
        if btc_change_24h is None:
            btc_change_24h = 0.0
            warnings.append(
                "KRW-BTC change rate is missing; crash filter uses btc_change_24h=0.0 fallback."
            )

    warning_markets: set[str] = set()
    try:
        warning_markets = await get_upbit_warning_markets(quote_currency="KRW")
    except Exception as exc:
        warnings.append(
            "market warning details unavailable; warning filter skipped "
            f"({type(exc).__name__}: {exc})"
        )

    filtered_by_warning = 0
    filtered_by_crash = 0
    candidates: list[dict[str, Any]] = []
    for raw_item in raw_results:
        market_code = str(raw_item.get("symbol") or "").strip().upper()
        if market_code in warning_markets:
            filtered_by_warning += 1
            continue

        coin_change_24h = raw_item.get("signed_change_rate")
        if coin_change_24h is None:
            coin_change_24h = raw_item.get("change_rate")
        if not is_safe_drop(coin_change_24h, btc_change_24h):
            filtered_by_crash += 1
            continue

        trade_amount_24h = _to_optional_float(
            raw_item.get("acc_trade_price_24h") or raw_item.get("trade_amount_24h")
        )
        item = {
            "symbol": market_code,
            "original_market": market_code,
            "market": market,
            "name": _resolve_crypto_display_name(market_code, raw_item, display_names),
            "close": _to_optional_float(raw_item.get("trade_price")),
            "change_rate": _to_optional_float(
                raw_item.get("change_rate")
                if raw_item.get("change_rate") is not None
                else raw_item.get("signed_change_rate")
            )
            or 0.0,
            "trade_amount_24h": trade_amount_24h or 0.0,
            "volume_24h": ticker_volume_map.get(market_code, 0.0),
            "market_cap": _to_optional_float(raw_item.get("tv_market_cap")),
            "market_cap_rank": None,
            "market_warning": None,
            "rsi": _to_optional_float(raw_item.get("rsi")),
            "volume_ratio": None,
            "candle_type": "flat",
            "adx": _to_optional_float(raw_item.get("adx")),
            "plus_di": None,
            "minus_di": None,
        }
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        candidates.append(item)

    coingecko_payload = await _CRYPTO_MARKET_CAP_CACHE.get()
    coingecko_data = cast(
        dict[str, dict[str, Any]],
        coingecko_payload.get("data") or {},
    )
    for item in candidates:
        symbol = _extract_market_symbol(
            item.get("symbol") or item.get("original_market")
        )
        cap_data = coingecko_data.get(symbol or "") if symbol else None
        if cap_data:
            item["market_cap"] = cap_data.get("market_cap") or item.get("market_cap")
            item["market_cap_rank"] = cap_data.get("market_cap_rank")
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))

    coingecko_error = coingecko_payload.get("error")
    if coingecko_error:
        if coingecko_payload.get("stale"):
            warnings.append(
                "CoinGecko market-cap refresh failed; stale cache was used."
            )
        else:
            warnings.append(
                "CoinGecko market-cap data unavailable; market_cap fields remain null."
            )

    if max_rsi is not None:
        filtered = [
            item
            for item in candidates
            if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
        ]
    else:
        filtered = candidates

    metric_diagnostics = _empty_rsi_enrichment_diagnostics()
    metric_diagnostics["attempted"] = len(filtered[:limit])
    timeout_count = 0
    for item in filtered[:limit]:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        try:
            df = await asyncio.wait_for(
                _fetch_ohlcv_for_indicators(symbol, "crypto", count=50),
                timeout=30.0,
            )
            metrics = calculate_crypto_metrics_from_ohlcv(df)
        except TimeoutError:
            timeout_count += 1
            error_samples = cast(list[str], metric_diagnostics["error_samples"])
            if len(error_samples) < 3:
                error_samples.append(f"TimeoutError: {symbol}")
            continue
        except Exception as exc:
            metric_diagnostics["failed"] = int(metric_diagnostics["failed"] or 0) + 1
            error_samples = cast(list[str], metric_diagnostics["error_samples"])
            if len(error_samples) < 3:
                error_samples.append(f"{type(exc).__name__}: {exc}"[:100])
            continue
        metric_diagnostics["succeeded"] = int(metric_diagnostics["succeeded"] or 0) + 1
        item["volume_ratio"] = metrics.get("volume_ratio")
        item["candle_type"] = metrics.get("candle_type") or "flat"
        item["plus_di"] = metrics.get("plus_di")
        item["minus_di"] = metrics.get("minus_di")
        if item.get("adx") is None:
            item["adx"] = metrics.get("adx")
        if item.get("rsi") is None:
            item["rsi"] = metrics.get("rsi")
            item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))

    if timeout_count > 0:
        metric_diagnostics["timeout"] = timeout_count
        warnings.append(
            f"Crypto RSI enrichment timed out for {timeout_count} symbols; partial results returned"
        )

    applied_sort_order = sort_order
    if sort_by == "rsi":
        if sort_order == "desc":
            warnings.append(
                "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
            )
        applied_sort_order = "asc"
        ordered = _sort_crypto_by_rsi_bucket(filtered)
    elif sort_by == "market_cap":
        ordered = sorted(
            filtered,
            key=lambda item: float(item.get("market_cap") or 0.0),
            reverse=(sort_order == "desc"),
        )
    else:
        ordered = _sort_and_limit(filtered, sort_by, sort_order, len(filtered))

    results = ordered[:limit]
    filters_applied["sort_order"] = applied_sort_order
    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=metric_diagnostics,
        warnings=warnings if warnings else None,
        meta_fields={
            "source": "tvscreener",
            "total_markets": len(raw_results),
            "top_by_volume": len(raw_results),
            "filtered_by_warning": filtered_by_warning,
            "filtered_by_crash": filtered_by_crash,
            "final_count": len(results),
            "coingecko_cached": bool(coingecko_payload.get("cached")),
            "coingecko_age_seconds": coingecko_payload.get("age_seconds"),
        },
    )


__all__ = [
    "is_safe_drop",
    "_normalize_screen_market",
    "_normalize_asset_type",
    "_normalize_sort_by",
    "_normalize_sort_order",
    "_normalize_dividend_yield_threshold",
    "_validate_screen_filters",
    "_apply_basic_filters",
    "_sort_and_limit",
    "_build_screen_response",
    "_screen_kr",
    "_screen_us",
    "_screen_crypto",
    "_screen_crypto_via_tvscreener",
    # KR stage functions
    "_stage_fetch_kr_candidates",
    "_stage_filter_kr",
    "_stage_enrich_kr_rsi",
    "_stage_sort_kr",
    # US stage functions
    "_stage_fetch_us_candidates",
    "_stage_filter_us",
    "_stage_enrich_us_rsi",
    "_stage_sort_us",
    # Crypto stage functions
    "_stage_fetch_crypto_candidates",
    "_stage_filter_crypto",
    "_stage_enrich_crypto_indicators",
    "_stage_sort_crypto",
]
