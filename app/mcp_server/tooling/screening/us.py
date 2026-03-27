"""US market screening — _screen_us, _screen_us_via_tvscreener, _screen_us_with_fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import yfinance as yf

from app.mcp_server.tooling.screening.common import (
    _apply_basic_filters,
    _build_screen_response,
    _clean_text,
    _get_first_present,
    _get_tvscreener_attr,
    _normalize_dividend_yield_threshold,
    _sort_and_limit,
    _strip_exchange_prefix,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
)
from app.mcp_server.tooling.screening.enrichment import (
    _compute_target_upside_pct,
    _pick_display_name,
)
from app.mcp_server.tooling.screening.tvscreener_support import (
    _adapt_tvscreener_stock_response,
    _can_use_tvscreener_stock_path,
    _get_tvscreener_stock_capability_snapshot,
)
from app.mcp_server.tooling.shared import error_payload as _error_payload
from app.monitoring import build_yfinance_tracing_session
from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerService,
    _import_tvscreener,
)

logger = logging.getLogger(__name__)


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
) -> dict[str, Any]:
    """Screen US market stocks using yfinance screener."""
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    filters_applied: dict[str, Any] = {
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
                    cast(
                        Any, ["forward_dividend_yield", min_dividend_yield_normalized]
                    ),
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
    """Screen US stocks using TradingView StockScreener API."""
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
        sector_field = _get_tvscreener_attr(StockField, "SECTOR")
        sector_display_field = _get_tvscreener_attr(StockField, "SECTOR_TR")
        recommendation_buy_field = _get_tvscreener_attr(
            StockField,
            "RECOMMENDATION_BUY",
        )
        recommendation_over_field = _get_tvscreener_attr(
            StockField,
            "RECOMMENDATION_OVER",
        )
        recommendation_hold_field = _get_tvscreener_attr(
            StockField,
            "RECOMMENDATION_HOLD",
        )
        recommendation_sell_field = _get_tvscreener_attr(
            StockField,
            "RECOMMENDATION_SELL",
        )
        recommendation_under_field = _get_tvscreener_attr(
            StockField,
            "RECOMMENDATION_UNDER",
        )
        price_target_average_field = _get_tvscreener_attr(
            StockField,
            "PRICE_TARGET_AVERAGE",
        )
        price_target_field = _get_tvscreener_attr(
            StockField,
            "PRICE_TARGET_1Y",
        )
        price_target_delta_field = _get_tvscreener_attr(
            StockField,
            "PRICE_TARGET_1Y_DELTA",
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
        if category is not None and sector_field is None:
            result["error"] = "tvscreener sector field unavailable"
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
        for optional_field in (
            sector_display_field,
            sector_field,
            recommendation_buy_field,
            recommendation_over_field,
            recommendation_hold_field,
            recommendation_sell_field,
            recommendation_under_field,
            price_target_average_field,
            price_target_field,
            price_target_delta_field,
        ):
            if optional_field is not None:
                columns.append(optional_field)

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
        if category is not None and sector_field is not None:
            where_conditions.append(sector_field == category)

        logger.info(
            "[Screen-US-TV] Querying StockScreener for US stocks "
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
                "sector": _get_first_present(row, "sector.tr", "sector"),
                "recommendation_buy": _to_optional_int(
                    _get_first_present(row, "recommendation_buy")
                ),
                "recommendation_over": _to_optional_int(
                    _get_first_present(row, "recommendation_over")
                ),
                "recommendation_hold": _to_optional_int(
                    _get_first_present(row, "recommendation_hold")
                ),
                "recommendation_sell": _to_optional_int(
                    _get_first_present(row, "recommendation_sell")
                ),
                "recommendation_under": _to_optional_int(
                    _get_first_present(row, "recommendation_under")
                ),
                "price_target_average": _to_optional_float(
                    _get_first_present(row, "price_target_average")
                ),
                "price_target_1y": _to_optional_float(
                    _get_first_present(row, "price_target_1y")
                ),
                "price_target_1y_delta": _to_optional_float(
                    _get_first_present(row, "price_target_1y_delta")
                ),
                "market": market,
                "country": str(row.get("country", "")).strip()
                if "country" in row
                else "United States",
            }
            sector = _clean_text(_get_first_present(row, "sector.tr", "sector"))
            if sector:
                stock["sector"] = sector
            recommendation_buy = _to_optional_int(
                _get_first_present(row, "recommendation_buy")
            )
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
            if recommendation_buy is not None or recommendation_over is not None:
                stock["analyst_buy"] = (recommendation_buy or 0) + (
                    recommendation_over or 0
                )
            if recommendation_hold is not None:
                stock["analyst_hold"] = recommendation_hold
            if recommendation_sell is not None or recommendation_under is not None:
                stock["analyst_sell"] = (recommendation_sell or 0) + (
                    recommendation_under or 0
                )
            avg_target = _to_optional_float(
                _get_first_present(row, "price_target_average", "price_target_1y")
            )
            if avg_target is not None:
                stock["avg_target"] = avg_target
            upside_pct = _to_optional_float(
                _get_first_present(row, "price_target_1y_delta")
            )
            if upside_pct is None:
                upside_pct = _compute_target_upside_pct(
                    avg_target=avg_target,
                    current_price=price,
                )
            if upside_pct is not None:
                stock["upside_pct"] = upside_pct
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


async def _screen_us_with_fallback(
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
    """Screen US market with tvscreener fallback to legacy."""
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
            tvscreener_result = await _screen_us_via_tvscreener(
                market=market,
                asset_type=asset_type,
                category=category,
                min_market_cap=min_market_cap,
                max_per=max_per,
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
                    "US stock screening via tvscreener succeeded: %d stocks",
                    tvscreener_result.get("count", 0),
                )
                return _adapt_tvscreener_stock_response(
                    tvscreener_result,
                    market=market,
                )
        except Exception as exc:
            logger.debug(
                "tvscreener US screening failed, falling back to legacy: %s",
                exc,
            )

    # Fallback to legacy implementation
    return await _screen_us(
        market=market,
        asset_type=asset_type,
        category=category,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )
