"""Stock screening helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import yfinance as yf

from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling.analysis_crypto_score import (
    calculate_crypto_composite_score,
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

    response: dict[str, Any] = {
        "results": results,
        "total_count": total_count,
        "returned_count": len(results),
        "filters_applied": filters_applied,
        "market": market,
        "meta": {"rsi_enrichment": diagnostics},
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
    """Screen crypto market coins using Upbit."""
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

    candidates = await upbit_service.fetch_top_traded_coins(fiat="KRW")

    for item in candidates:
        market_code = item.get("market")
        item["original_market"] = market_code
        item["market"] = "crypto"
        if market_code:
            item["symbol"] = market_code

        if "change_rate" not in item:
            item["change_rate"] = item.get("signed_change_rate", 0)

        volume_24h = item.get("acc_trade_volume_24h") or item.get("volume") or 0.0
        item["trade_amount_24h"] = item.get("trade_amount_24h") or item.get(
            "acc_trade_price_24h", 0
        )
        item.pop("volume", None)
        if "market_cap" not in item:
            item["market_cap"] = None

        item["rsi"] = item.get("rsi")
        item["volume_24h"] = float(volume_24h)
        item["volume_ratio"] = item.get("volume_ratio")
        item["candle_type"] = item.get("candle_type") or "flat"
        item["adx"] = item.get("adx")
        item["plus_di"] = item.get("plus_di")
        item["minus_di"] = item.get("minus_di")
        item["score"] = calculate_crypto_composite_score(
            rsi=item.get("rsi"),
            volume_24h=item["volume_24h"],
            avg_volume_20d=None,
            candle_coef=0.5,
            adx=item.get("adx"),
            plus_di=item.get("plus_di"),
            minus_di=item.get("minus_di"),
        )

    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    sorted_candidates = _sort_and_limit(
        candidates,
        "trade_amount" if sort_by in {"rsi", "score"} else sort_by,
        sort_order,
        len(candidates),
    )

    rsi_enrichment = _empty_rsi_enrichment_diagnostics()
    rsi_subset_limit = min(max(limit * 3, 30), 60)
    rsi_subset_limit = min(rsi_subset_limit, len(sorted_candidates))
    rsi_subset = sorted_candidates[:rsi_subset_limit]

    if enrich_rsi and rsi_subset:
        rsi_enrichment["attempted"] = len(rsi_subset)
        statuses = ["pending" for _ in rsi_subset]
        errors: list[str | None] = [None for _ in rsi_subset]
        semaphore = asyncio.Semaphore(10)

        async def calculate_rsi_for_coin(item: dict[str, Any], index: int):
            async with semaphore:
                item_copy = item.copy()
                symbol = (
                    item.get("original_market")
                    or item.get("symbol")
                    or item.get("market")
                )
                logger.info("[RSI-Crypto] Starting calculation for symbol: %s", symbol)

                if not symbol:
                    logger.warning(
                        "[RSI-Crypto] No valid symbol found in item keys=%s",
                        sorted(item.keys()),
                    )
                    statuses[index] = "error"
                    errors[index] = "No valid symbol found"
                    return item_copy

                if item_copy.get("rsi") is not None:
                    statuses[index] = "success"
                    return item_copy

                try:
                    logger.debug("[RSI-Crypto] Fetching OHLCV data for %s", symbol)
                    df = await _fetch_ohlcv_for_indicators(symbol, "crypto", count=50)
                    candle_count = len(df) if df is not None else 0
                    logger.info(
                        "[RSI-Crypto] Got %d candles for %s", candle_count, symbol
                    )

                    if df is None or df.empty:
                        logger.warning(
                            "[RSI-Crypto] Missing OHLCV data for %s",
                            symbol,
                        )
                        statuses[index] = "error"
                        errors[index] = "Missing OHLCV data"
                        return item_copy

                    metrics = calculate_crypto_metrics_from_ohlcv(df)
                    item_copy["rsi"] = metrics.get("rsi")
                    item_copy["score"] = metrics.get("score")
                    item_copy["volume_24h"] = metrics.get("volume_24h")
                    item_copy["volume_ratio"] = metrics.get("volume_ratio")
                    item_copy["candle_type"] = metrics.get("candle_type")
                    item_copy["adx"] = metrics.get("adx")
                    item_copy["plus_di"] = metrics.get("plus_di")
                    item_copy["minus_di"] = metrics.get("minus_di")

                    if metrics.get("rsi") is None:
                        logger.warning(
                            "[RSI-Crypto] RSI calculation returned None for %s", symbol
                        )
                        statuses[index] = "error"
                        errors[index] = "RSI calculation returned None"
                    else:
                        statuses[index] = "success"
                        logger.info(
                            "[RSI-Crypto] ✅ Success: %s RSI=%.2f Score=%.2f",
                            symbol,
                            metrics.get("rsi", 0),
                            metrics.get("score", 0),
                        )
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

                return item_copy

        try:
            subset_results = await asyncio.wait_for(
                asyncio.gather(
                    *[
                        calculate_rsi_for_coin(item, i)
                        for i, item in enumerate(rsi_subset)
                    ],
                    return_exceptions=True,
                ),
                timeout=30.0,
            )
            for i, coin in enumerate(subset_results):
                if isinstance(coin, Exception):
                    symbol = (
                        rsi_subset[i].get("original_market")
                        or rsi_subset[i].get("symbol")
                        or rsi_subset[i].get("market")
                    )
                    logger.error(
                        "[RSI-Crypto] gather returned exception for %s: %s: %s",
                        symbol or "UNKNOWN",
                        type(coin).__name__,
                        coin,
                    )
                    statuses[i] = (
                        "rate_limited"
                        if isinstance(coin, RateLimitExceededError)
                        else "error"
                    )
                    errors[i] = f"{type(coin).__name__}: {coin}"
                    continue
                rsi_subset[i].update(coin)
                if coin.get("rsi") is not None and statuses[i] == "pending":
                    statuses[i] = "success"
                elif coin.get("rsi") is None and statuses[i] == "pending":
                    statuses[i] = "error"
                    errors[i] = "RSI calculation returned None"
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

    if sort_by in {"rsi", "score"} and enrich_rsi:
        working_set = _sort_and_limit(rsi_subset, sort_by, sort_order, len(rsi_subset))
    else:
        working_set = rsi_subset if enrich_rsi else sorted_candidates

    if max_rsi is not None:
        filtered = _apply_basic_filters(
            working_set,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=max_rsi,
        )
    else:
        filtered = working_set

    results = filtered[:limit]
    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
        warnings=warnings if warnings else None,
    )


__all__ = [
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
