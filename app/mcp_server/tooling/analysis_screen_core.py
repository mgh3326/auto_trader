"""Stock screening helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

import httpx
import yfinance as yf

from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling.analysis_crypto_score import (
    calculate_crypto_metrics_from_ohlcv,
)
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_rsi,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.services import upbit as upbit_service
from app.services.krx import (
    classify_etf_category,
    fetch_etf_all_cached,
    fetch_stock_all_cached,
    fetch_valuation_all_cached,
)

logger = logging.getLogger(__name__)

DROP_THRESHOLD = -0.30
MARKET_PANIC = -0.10
CRYPTO_TOP_BY_VOLUME = 100
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_optional_int(value: Any) -> int | None:
    if value is None:
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
        if field == "market_cap" and (value is None or value == 0):
            value = item.get("trade_amount_24h")
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
    """Screen Korean market stocks/ETFs."""
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    filters_applied = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    if category is not None and asset_type is None:
        asset_type = "etf"
        filters_applied["asset_type"] = "etf"

    candidates = []

    if asset_type is None or asset_type == "stock":
        if market == "kospi":
            candidates.extend(await fetch_stock_all_cached(market="STK"))
        elif market == "kosdaq":
            candidates.extend(await fetch_stock_all_cached(market="KSQ"))
        else:
            candidates.extend(await fetch_stock_all_cached(market="STK"))
            candidates.extend(await fetch_stock_all_cached(market="KSQ"))

    if asset_type is None or asset_type == "etf":
        etfs: list[dict[str, Any]] = []
        if market != "kosdaq":
            etfs = await fetch_etf_all_cached()

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

    for item in candidates:
        if "change_rate" not in item:
            item["change_rate"] = 0.0

        if "market" not in item:
            item["market"] = "kr"

        if "asset_type" not in item:
            item["asset_type"] = "stock"

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

    advanced_filters_applied = {
        "min_market_cap": min_market_cap,
        "max_per": max_per,
        "max_pbr": max_pbr,
        "min_dividend_yield": min_dividend_yield_normalized,
        "max_rsi": max_rsi,
    }

    filtered_non_rsi = _apply_basic_filters(
        candidates,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield_normalized,
        max_rsi=None,
    )
    sorted_candidates = _sort_and_limit(
        filtered_non_rsi,
        sort_by,
        sort_order,
        len(filtered_non_rsi),
    )

    rsi_enrichment = _empty_rsi_enrichment_diagnostics()
    rsi_subset_limit = (
        min(len(sorted_candidates), limit)
        if max_rsi is None
        else min(len(sorted_candidates), limit * 3, 150)
    )
    rsi_subset = sorted_candidates[:rsi_subset_limit]

    if enrich_rsi and rsi_subset:
        rsi_enrichment["attempted"] = len(rsi_subset)
        statuses = ["pending" for _ in rsi_subset]
        errors: list[str | None] = [None for _ in rsi_subset]
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
                    df = await _fetch_ohlcv_for_indicators(
                        symbol, "equity_kr", count=50
                    )
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
                        for i, item in enumerate(rsi_subset)
                    ],
                    return_exceptions=True,
                ),
                timeout=30.0,
            )
            for i, result in enumerate(subset_results):
                if isinstance(result, Exception):
                    symbol = rsi_subset[i].get("short_code") or rsi_subset[i].get(
                        "code"
                    )
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
                if result.get("rsi") is not None:
                    rsi_subset[i]["rsi"] = result["rsi"]
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

    filters_applied.update(advanced_filters_applied)
    filters_applied["sort_by"] = sort_by
    filters_applied["sort_order"] = sort_order
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    if max_rsi is not None:
        filtered = _apply_basic_filters(
            rsi_subset,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=max_rsi,
        )
    else:
        filtered = sorted_candidates

    results = filtered[:limit]
    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
    )


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
    """Screen US market stocks using yfinance screener."""
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    filters_applied = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    def _complete_filters_applied():
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

    try:
        from yfinance.screener import EquityQuery

        conditions = []
        if min_market_cap is not None:
            conditions.append(EquityQuery("gte", ["intradaymarketcap", min_market_cap]))
        if max_per is not None:
            conditions.append(EquityQuery("lte", ["peratio.lasttwelvemonths", max_per]))
        if min_dividend_yield is not None:
            conditions.append(
                EquityQuery(
                    "gte", ["forward_dividend_yield", min_dividend_yield_normalized]
                )
            )
        if category:
            conditions.append(EquityQuery("eq", ["sector", category]))

        conditions.append(EquityQuery("eq", ["region", "us"]))
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

        screen_result = await asyncio.to_thread(
            lambda: yf.screen(
                query,
                size=fetch_size,
                sortField=sort_field,
                sortAsc=(sort_order == "asc"),
            )
        )

        if screen_result is None:
            _complete_filters_applied()
            return _build_screen_response([], 0, filters_applied, market)

        quotes = (
            screen_result.get("quotes", []) if isinstance(screen_result, dict) else []
        )
        if not quotes:
            _complete_filters_applied()
            return _build_screen_response([], 0, filters_applied, market)

        def _first_value(quote: dict[str, Any], *keys: str) -> Any:
            for key in keys:
                value = quote.get(key)
                if value is not None:
                    return value
            return None

        results = []
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
            results.append(mapped)

        if enrich_rsi and results:
            semaphore = asyncio.Semaphore(10)

            async def calculate_rsi_for_stock(item: dict[str, Any]):
                async with semaphore:
                    item_copy = item.copy()
                    symbol = item["code"]

                    try:
                        df = await _fetch_ohlcv_for_indicators(symbol, "us", count=50)
                        if not df.empty and "close" in df.columns:
                            rsi_result = _calculate_rsi(df["close"])
                            if rsi_result and "14" in rsi_result:
                                item_copy["rsi"] = rsi_result["14"]
                    except Exception:
                        pass

                    return item_copy

            subset_limit = min(len(results), limit * 3, 150)
            subset = results[:subset_limit]
            try:
                subset_with_rsi = await asyncio.wait_for(
                    asyncio.gather(
                        *[calculate_rsi_for_stock(item) for item in subset],
                        return_exceptions=True,
                    ),
                    timeout=30.0,
                )
                for i, enriched in enumerate(subset_with_rsi):
                    if isinstance(enriched, Exception):
                        continue
                    rsi_value = enriched.get("rsi")
                    if rsi_value is not None:
                        results[i]["rsi"] = rsi_value
            except TimeoutError:
                pass
            except Exception:
                pass

        if max_rsi is not None:
            results = _apply_basic_filters(
                results,
                min_market_cap=None,
                max_per=None,
                max_pbr=None,
                min_dividend_yield=None,
                max_rsi=max_rsi,
            )

        _complete_filters_applied()
        pre_limit_count = len(results)
        results = _sort_and_limit(results, sort_by, sort_order, limit)
        return _build_screen_response(results, pre_limit_count, filters_applied, market)
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


async def _enrich_crypto_rsi_subset(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    rsi_enrichment = _empty_rsi_enrichment_diagnostics()
    if not candidates:
        return rsi_enrichment

    rsi_enrichment["attempted"] = len(candidates)
    statuses = ["pending" for _ in candidates]
    errors: list[str | None] = [None for _ in candidates]
    semaphore = asyncio.Semaphore(10)

    async def calculate_rsi_for_coin(item: dict[str, Any], index: int) -> None:
        async with semaphore:
            symbol = (
                item.get("original_market") or item.get("symbol") or item.get("market")
            )

            if not symbol:
                statuses[index] = "error"
                errors[index] = "No valid symbol found"
                return

            if item.get("rsi") is not None:
                statuses[index] = "success"
                return

            try:
                df = await _fetch_ohlcv_for_indicators(symbol, "crypto", count=50)
                if df is None or df.empty:
                    statuses[index] = "error"
                    errors[index] = "Missing OHLCV data"
                    return

                metrics = calculate_crypto_metrics_from_ohlcv(df)
                item["rsi"] = metrics.get("rsi")
                item["volume_24h"] = metrics.get("volume_24h")
                item["volume_ratio"] = metrics.get("volume_ratio")
                item["candle_type"] = metrics.get("candle_type")
                item["adx"] = metrics.get("adx")
                item["plus_di"] = metrics.get("plus_di")
                item["minus_di"] = metrics.get("minus_di")
                item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))

                if item.get("rsi") is None:
                    statuses[index] = "error"
                    errors[index] = "RSI calculation returned None"
                else:
                    statuses[index] = "success"
            except Exception as exc:
                logger.error(
                    "[RSI-Crypto] ❌ Failed for %s: %s: %s",
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

    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(
                *[calculate_rsi_for_coin(item, i) for i, item in enumerate(candidates)],
                return_exceptions=True,
            ),
            timeout=30.0,
        )
        for i, result in enumerate(gathered):
            if isinstance(result, Exception) and statuses[i] == "pending":
                symbol = (
                    candidates[i].get("original_market")
                    or candidates[i].get("symbol")
                    or candidates[i].get("market")
                )
                logger.error(
                    "[RSI-Crypto] gather returned exception for %s: %s: %s",
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
    except TimeoutError:
        logger.warning("[RSI-Crypto] RSI enrichment timed out after 30 seconds")
        for i, status in enumerate(statuses):
            if status == "pending":
                statuses[i] = "timeout"
                errors[i] = "Timed out after 30 seconds"
    except Exception as exc:
        logger.error(
            "[RSI-Crypto] RSI enrichment batch failed: %s: %s",
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

    return rsi_enrichment


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
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    warnings: list[str] = []
    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    all_candidates = await upbit_service.fetch_top_traded_coins(fiat="KRW")
    total_markets = len(all_candidates)
    top_candidates = all_candidates[:CRYPTO_TOP_BY_VOLUME]
    top_by_volume = len(top_candidates)

    btc_change_24h = 0.0
    btc_item = next(
        (
            item
            for item in all_candidates
            if str(item.get("market") or "").upper() == "KRW-BTC"
        ),
        None,
    )
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
        market_details = await upbit_service.fetch_all_market_codes(
            fiat="KRW",
            include_details=True,
        )
        for detail in market_details:
            if not isinstance(detail, dict):
                continue
            market_code = str(detail.get("market") or "").strip().upper()
            if not market_code:
                continue
            market_event = detail.get("market_event")
            if isinstance(market_event, dict) and _is_market_warning(
                market_event.get("warning")
            ):
                warning_markets.add(market_code)
    except Exception as exc:
        warnings.append(
            "market warning details unavailable; warning filter skipped "
            f"({type(exc).__name__}: {exc})"
        )

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

    async def _run_rsi_enrichment() -> dict[str, Any]:
        if not enrich_rsi or not candidates:
            return _empty_rsi_enrichment_diagnostics()
        try:
            return await _enrich_crypto_rsi_subset(candidates)
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

    coingecko_data = coingecko_payload.get("data") or {}
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

    if max_rsi is not None:
        filtered = [
            item
            for item in candidates
            if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
        ]
    else:
        filtered = candidates

    applied_sort_order = sort_order
    if sort_by == "rsi":
        if sort_order == "desc":
            warnings.append(
                "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
            )
        applied_sort_order = "asc"
        ordered = _sort_crypto_by_rsi_bucket(filtered)
    else:
        ordered = _sort_and_limit(filtered, sort_by, sort_order, len(filtered))

    results = ordered[:limit]
    for item in results:
        item.pop("score", None)

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
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
        warnings=warnings if warnings else None,
        meta_fields=meta_fields,
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
]
