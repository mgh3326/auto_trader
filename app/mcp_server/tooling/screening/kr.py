"""KR market screening — _screen_kr, _screen_kr_via_tvscreener, _screen_kr_with_fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling.market_data_indicators import (
    _calculate_rsi,
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.screening.common import (
    _apply_basic_filters,
    _build_screen_response,
    _empty_rsi_enrichment_diagnostics,
    _extract_kr_stock_code,
    _finalize_rsi_enrichment_diagnostics,
    _kr_market_codes,
    _normalize_dividend_yield_threshold,
    _sort_and_limit,
    _timeout_seconds,
    _to_optional_float,
)
from app.mcp_server.tooling.screening.enrichment import _pick_display_name
from app.mcp_server.tooling.screening.tvscreener_support import (
    _adapt_tvscreener_stock_response,
    _can_use_tvscreener_stock_path,
    _get_tvscreener_stock_capability_snapshot,
)
from app.services.krx import (
    classify_etf_category,
    fetch_etf_all_cached,
    fetch_stock_all_cached,
    fetch_valuation_all_cached,
)
from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerService,
    _import_tvscreener,
)

logger = logging.getLogger(__name__)


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

    filters_applied: dict[str, Any] = {
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
                timeout=_timeout_seconds("rsi_enrichment"),
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
                if not isinstance(result, dict):
                    continue
                if result.get("rsi") is not None:
                    rsi_subset[i]["rsi"] = result["rsi"]
                    if statuses[i] == "pending":
                        statuses[i] = "success"
                elif statuses[i] == "pending":
                    statuses[i] = "error"
                    errors[i] = "RSI calculation returned None"
        except TimeoutError:
            logger.warning(
                "[RSI-KR] RSI enrichment timed out after %.2f seconds",
                _timeout_seconds("rsi_enrichment"),
            )
            for i, status in enumerate(statuses):
                if status == "pending":
                    statuses[i] = "timeout"
                    errors[i] = (
                        f"Timed out after {_timeout_seconds('rsi_enrichment'):.2f} seconds"
                    )
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

    # Re-sort by RSI after enrichment (RSI values didn't exist during initial sort)
    if sort_by == "rsi":
        filtered = _sort_and_limit(filtered, sort_by, sort_order, len(filtered))

    results = filtered[:limit]
    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment,
    )


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
    """Screen Korean stocks using TradingView StockScreener API."""
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

        tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))
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


async def _screen_kr_with_fallback(
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
    enrich_rsi: bool,
) -> dict[str, Any]:
    """Screen Korean market with tvscreener fallback to legacy."""
    capability_snapshot = await _get_tvscreener_stock_capability_snapshot(
        market=market,
        asset_type=asset_type,
        category=category,
        sort_by=sort_by,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
    )
    if _can_use_tvscreener_stock_path(
        market=market,
        asset_type=asset_type,
        category=category,
        sort_by=sort_by,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
        capability_snapshot=capability_snapshot,
    ):
        try:
            tvscreener_result = await _screen_kr_via_tvscreener(
                market=market,
                asset_type=asset_type or "stock",
                category=category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                max_pbr=max_pbr,
                min_dividend_yield=min_dividend_yield,
                min_rsi=None,
                max_rsi=max_rsi,
                min_adx=None,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
            )
            if tvscreener_result and not tvscreener_result.get("error"):
                logger.info(
                    "Korean stock screening via tvscreener succeeded: %d stocks",
                    tvscreener_result.get("count", 0),
                )
                return _adapt_tvscreener_stock_response(
                    tvscreener_result,
                    market=market,
                )
        except Exception as exc:
            logger.debug(
                "tvscreener Korean screening failed, falling back to legacy: %s",
                exc,
            )

    # Fallback to legacy implementation
    return await _screen_kr(
        market=market,
        asset_type=asset_type,
        category=category,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        enrich_rsi=enrich_rsi,
    )
