"""US market screening — _screen_us, _screen_us_via_tvscreener, _screen_us_with_fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import yfinance as yf

from app.mcp_server.tooling.screening.common import (
    _aggregate_analyst_recommendations,
    _apply_basic_filters,
    _build_rsi_adx_conditions,
    _build_screen_response,
    _clean_text,
    _compute_avg_target_and_upside,
    _filter_by_min_analyst_buy,
    _get_first_present,
    _get_tvscreener_attr,
    _init_tvscreener_result,
    _normalize_dividend_yield_threshold,
    _sort_and_limit,
    _strip_exchange_prefix,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
)
from app.mcp_server.tooling.screening.enrichment import (
    _pick_display_name,
)
from app.mcp_server.tooling.screening.instrument_type import classify_us_instrument
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
    adv_krw_min: int | None = None,
    market_cap_min_krw: int | None = None,
    market_cap_max_krw: int | None = None,
    instrument_types: list[str] | None = None,
    exclude_sectors: list[str] | None = None,
    exclude_sector_keys: set[str] | None = None,
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
                "adv_krw_min": adv_krw_min,
                "market_cap_min_krw": market_cap_min_krw,
                "market_cap_max_krw": market_cap_max_krw,
                "instrument_types": instrument_types,
                "exclude_sectors": exclude_sectors,
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
            sector = _first_value(quote, "sector", "sectorDisp")
            if sector:
                mapped["sector"] = sector
            mapped["instrument_type"] = classify_us_instrument(
                mapped.get("code"),
                mapped.get("name"),
                _first_value(quote, "quoteType", "type"),
                _first_value(quote, "typeDisp", "subtype"),
            )
            market_cap = _to_optional_float(mapped.get("market_cap"))
            if market_cap is not None:
                mapped["market_cap_krw"] = market_cap
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
                adv_krw_min=None,
                market_cap_min_krw=market_cap_min_krw,
                market_cap_max_krw=market_cap_max_krw,
                instrument_types=instrument_types,
                exclude_sector_keys=exclude_sector_keys,
            )
        else:
            results = _apply_basic_filters(
                results,
                min_market_cap=None,
                max_per=None,
                max_pbr=None,
                min_dividend_yield=None,
                max_rsi=None,
                adv_krw_min=None,
                market_cap_min_krw=market_cap_min_krw,
                market_cap_max_krw=market_cap_max_krw,
                instrument_types=instrument_types,
                exclude_sector_keys=exclude_sector_keys,
            )

        _complete_filters_applied()
        pre_limit_count = len(results)
        results = _sort_and_limit(results, sort_by, sort_order, limit)
        warnings = []
        if adv_krw_min is not None:
            warnings.append(
                "adv_krw_min requires 30-day average-volume data and is not "
                "available on the US yfinance fallback path; filter was skipped."
            )
        return _build_screen_response(
            results,
            pre_limit_count,
            filters_applied,
            market,
            warnings=warnings or None,
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


def _build_us_filters(
    *,
    market: str,
    category: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield_normalized: float | None,
    min_rsi: float | None,
    max_rsi: float | None,
    min_adx: float | None,
    sort_by: str,
) -> dict[str, Any] | str:
    """Build tvscreener columns and where-conditions for US stock screening.

    Returns a dict with keys: StockField, Market, columns, where_conditions,
    and resolved field references. Returns an error string on failure.
    """
    try:
        tvscreener = _import_tvscreener()
        StockField = tvscreener.StockField
        Market = tvscreener.Market
    except ImportError:
        return "tvscreener library not installed, cannot use StockScreener"

    market_cap_field = _get_tvscreener_attr(
        StockField, "MARKET_CAPITALIZATION", "MARKET_CAP_BASIC"
    )
    pe_field = _get_tvscreener_attr(
        StockField, "PRICE_TO_EARNINGS_RATIO_TTM", "PRICE_TO_EARNINGS_TTM"
    )
    dividend_field = _get_tvscreener_attr(
        StockField,
        "DIVIDEND_YIELD_FORWARD",
        "DIVIDEND_YIELD_RECENT",
        "DIVIDEND_YIELD_CURRENT",
    )
    sector_field = _get_tvscreener_attr(StockField, "SECTOR")
    sector_display_field = _get_tvscreener_attr(StockField, "SECTOR_TR")
    recommendation_buy_field = _get_tvscreener_attr(StockField, "RECOMMENDATION_BUY")
    recommendation_over_field = _get_tvscreener_attr(StockField, "RECOMMENDATION_OVER")
    recommendation_hold_field = _get_tvscreener_attr(StockField, "RECOMMENDATION_HOLD")
    recommendation_sell_field = _get_tvscreener_attr(StockField, "RECOMMENDATION_SELL")
    recommendation_under_field = _get_tvscreener_attr(
        StockField, "RECOMMENDATION_UNDER"
    )
    price_target_average_field = _get_tvscreener_attr(
        StockField, "PRICE_TARGET_AVERAGE"
    )
    price_target_field = _get_tvscreener_attr(StockField, "PRICE_TARGET_1Y")
    price_target_delta_field = _get_tvscreener_attr(StockField, "PRICE_TARGET_1Y_DELTA")
    average_volume_30_day_field = _get_tvscreener_attr(
        StockField, "AVERAGE_VOLUME_30_DAY", "AVERAGE_VOLUME_30D", "AVERAGE_VOLUME_30"
    )
    type_field = _get_tvscreener_attr(StockField, "TYPE")
    subtype_field = _get_tvscreener_attr(StockField, "SUBTYPE")

    # Validate that required fields are available
    if sort_by == "market_cap" and market_cap_field is None:
        return "tvscreener market-cap field unavailable"
    if sort_by == "dividend_yield" and dividend_field is None:
        return "tvscreener dividend-yield field unavailable"
    if min_market_cap is not None and market_cap_field is None:
        return "tvscreener market-cap field unavailable"
    if max_per is not None and pe_field is None:
        return "tvscreener PE field unavailable"
    if min_dividend_yield_normalized is not None and dividend_field is None:
        return "tvscreener dividend-yield field unavailable"
    if category is not None and sector_field is None:
        return "tvscreener sector field unavailable"

    # Build columns
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
        average_volume_30_day_field,
        type_field,
        subtype_field,
    ):
        if optional_field is not None:
            columns.append(optional_field)
    try:
        columns.append(StockField.COUNTRY)
    except AttributeError:
        logger.warning("[Screen-US-TV] COUNTRY field not available in StockField")

    # Build where conditions
    where_conditions = _build_rsi_adx_conditions(
        min_rsi=min_rsi,
        max_rsi=max_rsi,
        min_adx=min_adx,
        rsi_field=StockField.RELATIVE_STRENGTH_INDEX_14,
        adx_field=StockField.AVERAGE_DIRECTIONAL_INDEX_14,
    )
    if min_market_cap is not None and market_cap_field is not None:
        where_conditions.append(market_cap_field >= min_market_cap)
    if max_per is not None and pe_field is not None:
        where_conditions.append(pe_field <= max_per)
    if min_dividend_yield_normalized is not None and dividend_field is not None:
        where_conditions.append(dividend_field >= min_dividend_yield_normalized)
    if category is not None and sector_field is not None:
        where_conditions.append(sector_field == category)

    return {
        "Market": Market,
        "columns": columns,
        "where_conditions": where_conditions,
    }


async def _execute_us_query(
    *,
    columns: list[Any],
    where_conditions: list[Any],
    Market: Any,
    min_rsi: float | None,
    max_rsi: float | None,
    min_adx: float | None,
    limit: int,
) -> Any:
    """Execute the tvscreener StockScreener query for US stocks.

    Returns a pandas DataFrame. Raises TvScreenerError or TimeoutError on failure.
    """
    logger.info(
        "[Screen-US-TV] Querying StockScreener for US stocks "
        "(filters: min_rsi=%s, max_rsi=%s, min_adx=%s, limit=%d)",
        min_rsi,
        max_rsi,
        min_adx,
        limit,
    )

    tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))
    return await tvscreener_service.query_stock_screener(
        columns=columns,
        where_clause=where_conditions,
        country="United States",
        markets=[Market.AMERICA],
        limit=None,
    )


def _normalize_us_results(
    df: Any,
    *,
    market: str,
) -> list[dict[str, Any]]:
    """Map tvscreener DataFrame rows to normalized US stock dicts."""
    stocks: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        price = _to_optional_float(row.get("price"))
        if price is None or price <= 0:
            continue

        stock: dict[str, Any] = {
            "symbol": _strip_exchange_prefix(row.get("symbol")),
            "name": _pick_display_name(row),
            "price": price,
            "rsi": _to_optional_float(row.get("relative_strength_index_14")),
            "adx": _to_optional_float(row.get("average_directional_index_14")),
            "volume": _to_optional_float(row.get("volume")),
            "average_volume_30_day": _to_optional_float(
                _get_first_present(
                    row,
                    "average_volume_30_day",
                    "average_volume_30d",
                    "average_volume_30",
                )
            ),
            "change_percent": _to_optional_float(row.get("change_percent")),
            "market_cap": _to_optional_float(
                _get_first_present(row, "market_capitalization", "market_cap_basic")
            ),
            "per": _to_optional_float(
                _get_first_present(
                    row, "price_to_earnings_ratio_ttm", "price_to_earnings_ttm"
                )
            ),
            "dividend_yield": _to_optional_float(
                _get_first_present(
                    row,
                    "dividend_yield_forward",
                    "dividend_yield_recent",
                    "dividend_yield_current",
                )
            ),
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
                _get_first_present(row, "price_target_average", "target_price_average")
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
        avg_volume = _to_optional_float(stock.get("average_volume_30_day"))
        if avg_volume is not None:
            stock["adv_krw"] = avg_volume * price
        market_cap = _to_optional_float(stock.get("market_cap"))
        if market_cap is not None:
            stock["market_cap_krw"] = market_cap
        stock["instrument_type"] = classify_us_instrument(
            stock.get("symbol"),
            stock.get("name"),
            _get_first_present(row, "type"),
            _get_first_present(row, "subtype"),
        )

        stock.update(_aggregate_analyst_recommendations(row))

        avg_target, upside_pct = _compute_avg_target_and_upside(
            row, current_price=price
        )
        if avg_target is not None:
            stock["avg_target"] = avg_target
        if upside_pct is not None:
            stock["upside_pct"] = upside_pct

        stock["change_rate"] = stock["change_percent"]
        stocks.append(stock)

    return stocks


async def _screen_us_via_tvscreener(
    market: str = "us",
    asset_type: str | None = None,
    category: str | None = None,
    min_market_cap: float | None = None,
    max_per: float | None = None,
    min_dividend_yield: float | None = None,
    min_analyst_buy: float | None = None,
    min_rsi: float | None = None,
    max_rsi: float | None = None,
    min_adx: float | None = None,
    sort_by: str = "rsi",
    sort_order: str = "desc",
    limit: int = 50,
    adv_krw_min: int | None = None,
    market_cap_min_krw: int | None = None,
    market_cap_max_krw: int | None = None,
    instrument_types: list[str] | None = None,
    exclude_sectors: list[str] | None = None,
    exclude_sector_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Screen US stocks using TradingView StockScreener API."""
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
        "min_analyst_buy": min_analyst_buy,
        "min_rsi": min_rsi,
        "max_rsi": max_rsi,
        "min_adx": min_adx,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "limit": limit,
        "adv_krw_min": adv_krw_min,
        "market_cap_min_krw": market_cap_min_krw,
        "market_cap_max_krw": market_cap_max_krw,
        "instrument_types": instrument_types,
        "exclude_sectors": exclude_sectors,
    }
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    result = _init_tvscreener_result(filters_applied)

    try:
        # Phase 1: Build filters
        build_result = _build_us_filters(
            market=market,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            min_dividend_yield_normalized=min_dividend_yield_normalized,
            min_rsi=min_rsi,
            max_rsi=max_rsi,
            min_adx=min_adx,
            sort_by=sort_by,
        )
        if isinstance(build_result, str):
            logger.warning("[Screen-US-TV] %s", build_result)
            result["error"] = build_result
            return result

        # Phase 2: Execute query
        df = await _execute_us_query(
            columns=build_result["columns"],
            where_conditions=build_result["where_conditions"],
            Market=build_result["Market"],
            min_rsi=min_rsi,
            max_rsi=max_rsi,
            min_adx=min_adx,
            limit=limit,
        )

        if df.empty:
            logger.info("[Screen-US-TV] StockScreener returned no results")
            return result

        logger.info("[Screen-US-TV] StockScreener returned %d US stocks", len(df))

        # Phase 3: Normalize results
        stocks = _normalize_us_results(df, market=market)

        stocks = _filter_by_min_analyst_buy(stocks, min_analyst_buy)

        filtered = _apply_basic_filters(
            stocks,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=None,
            min_dividend_yield=min_dividend_yield_normalized,
            max_rsi=max_rsi,
            adv_krw_min=adv_krw_min,
            market_cap_min_krw=market_cap_min_krw,
            market_cap_max_krw=market_cap_max_krw,
            instrument_types=instrument_types,
            exclude_sector_keys=exclude_sector_keys,
        )
        ordered = _sort_and_limit(filtered, sort_by, sort_order, limit)

        result["count"] = len(filtered)
        result["stocks"] = ordered
        if adv_krw_min is not None:
            result["meta_fields"] = {"adv_window_days": 30}

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
    min_analyst_buy: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
    adv_krw_min: int | None = None,
    market_cap_min_krw: int | None = None,
    market_cap_max_krw: int | None = None,
    instrument_types: list[str] | None = None,
    exclude_sectors: list[str] | None = None,
    exclude_sector_keys: set[str] | None = None,
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
                min_analyst_buy=min_analyst_buy,
                min_rsi=None,
                max_rsi=max_rsi,
                min_adx=None,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                adv_krw_min=adv_krw_min,
                market_cap_min_krw=market_cap_min_krw,
                market_cap_max_krw=market_cap_max_krw,
                instrument_types=instrument_types,
                exclude_sectors=exclude_sectors,
                exclude_sector_keys=exclude_sector_keys,
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
        adv_krw_min=adv_krw_min,
        market_cap_min_krw=market_cap_min_krw,
        market_cap_max_krw=market_cap_max_krw,
        instrument_types=instrument_types,
        exclude_sectors=exclude_sectors,
        exclude_sector_keys=exclude_sector_keys,
    )
