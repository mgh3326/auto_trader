"""KR market screening — _screen_kr, _screen_kr_via_tvscreener, _screen_kr_with_fallback."""

from __future__ import annotations

import logging
from typing import Any

from app.mcp_server.tooling.screening.common import (
    _aggregate_analyst_recommendations,
    _apply_basic_filters,
    _build_rsi_adx_conditions,
    _build_screen_response,
    _clean_text,
    _compute_avg_target_and_upside,
    _extract_kr_stock_code,
    _filter_by_min_analyst_buy,
    _get_first_present,
    _get_tvscreener_attr,
    _init_tvscreener_result,
    _kr_market_codes,
    _normalize_dividend_yield_threshold,
    _sort_and_limit,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
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

    filtered = _apply_basic_filters(
        candidates,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield_normalized,
        max_rsi=max_rsi,
    )
    results = _sort_and_limit(
        filtered,
        sort_by,
        sort_order,
        limit,
    )

    filters_applied.update(advanced_filters_applied)
    filters_applied["sort_by"] = sort_by
    filters_applied["sort_order"] = sort_order
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
    )


def _build_kr_filters(
    *,
    min_rsi: float | None,
    max_rsi: float | None,
    min_adx: float | None,
) -> dict[str, Any] | str:
    """Build tvscreener columns and where-conditions for KR stock screening.

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
    pbr_field = _get_tvscreener_attr(
        StockField, "PRICE_TO_BOOK_FQ", "PRICE_TO_BOOK_MRQ", "PRICE_BOOK_CURRENT"
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
    price_target_field = _get_tvscreener_attr(StockField, "PRICE_TARGET_1Y")
    price_target_delta_field = _get_tvscreener_attr(
        StockField, "PRICE_TARGET_1Y_DELTA"
    )

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
    for optional_field in (
        market_cap_field,
        pe_field,
        pbr_field,
        dividend_field,
        sector_display_field,
        sector_field,
        recommendation_buy_field,
        recommendation_over_field,
        recommendation_hold_field,
        recommendation_sell_field,
        recommendation_under_field,
        price_target_field,
        price_target_delta_field,
    ):
        if optional_field is not None:
            columns.append(optional_field)
    try:
        columns.append(StockField.COUNTRY)
    except AttributeError:
        logger.warning("[Screen-KR-TV] COUNTRY field not available in StockField")

    where_conditions = _build_rsi_adx_conditions(
        min_rsi=min_rsi,
        max_rsi=max_rsi,
        min_adx=min_adx,
        rsi_field=StockField.RELATIVE_STRENGTH_INDEX_14,
        adx_field=StockField.AVERAGE_DIRECTIONAL_INDEX_14,
    )

    return {
        "Market": Market,
        "columns": columns,
        "where_conditions": where_conditions,
    }


async def _execute_kr_query(
    *,
    columns: list[Any],
    where_conditions: list[Any],
    Market: Any,
    min_rsi: float | None,
    max_rsi: float | None,
    min_adx: float | None,
    limit: int,
) -> Any:
    """Execute the tvscreener StockScreener query for KR stocks.

    Returns a pandas DataFrame. Raises TvScreenerError or TimeoutError on failure.
    """
    logger.info(
        "[Screen-KR-TV] Querying StockScreener for Korean stocks "
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
        country=None,
        markets=[Market.KOREA],
        limit=None,
    )


async def _normalize_kr_results(
    df: Any,
    *,
    market: str,
) -> list[dict[str, Any]]:
    """Map tvscreener DataFrame rows to normalized KR stock dicts.

    Cross-references with KRX stock universe and enriches with valuation data.
    """
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

    stocks: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        code = _extract_kr_stock_code(row.get("symbol"))
        if not code or code not in allowed_by_code:
            continue

        base = allowed_by_code[code]
        valuation = valuations.get(code, {})
        sector = _clean_text(_get_first_present(row, "sector.tr", "sector"))

        avg_target, upside_pct = _compute_avg_target_and_upside(
            row, current_price=_to_optional_float(row.get("price"))
        )

        stock: dict[str, Any] = {
            "symbol": code,
            "short_code": code,
            "code": base.get("code") or code,
            "name": _pick_display_name(row),
            "price": _to_optional_float(row.get("price")),
            "rsi": _to_optional_float(row.get("relative_strength_index_14")),
            "adx": _to_optional_float(row.get("average_directional_index_14")),
            "volume": _to_optional_float(row.get("volume")),
            "change_percent": _to_optional_float(row.get("change_percent")),
            "market_cap": _to_optional_float(
                _get_first_present(
                    row, "market_capitalization", "market_cap_basic", "market_cap"
                )
            ),
            "per": _to_optional_float(
                _get_first_present(
                    row, "price_to_earnings_ratio_ttm", "price_to_earnings_ttm"
                )
            ),
            "pbr": _to_optional_float(
                _get_first_present(
                    row, "price_to_book_fq", "price_to_book_mrq", "price_book_current"
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
            "market": base.get("market") or market,
            "country": str(row.get("country", "")).strip()
            if "country" in row
            else "South Korea",
        }
        stock["change_rate"] = stock["change_percent"]

        # Fallback to KRX data for missing fields
        if stock["market_cap"] is None:
            stock["market_cap"] = _to_optional_float(base.get("market_cap"))
        if stock["per"] is None:
            stock["per"] = _to_optional_float(valuation.get("per"))
        if stock["pbr"] is None:
            stock["pbr"] = _to_optional_float(valuation.get("pbr"))
        if stock["dividend_yield"] is None:
            stock["dividend_yield"] = _to_optional_float(
                valuation.get("dividend_yield")
            )
        if not stock["name"]:
            stock["name"] = str(base.get("name") or "").strip()

        if sector:
            stock["sector"] = sector

        stock.update(_aggregate_analyst_recommendations(row))

        if avg_target is not None:
            stock["avg_target"] = avg_target
        if upside_pct is not None:
            stock["upside_pct"] = upside_pct

        stocks.append(stock)

    return stocks


async def _screen_kr_via_tvscreener(
    market: str = "kr",
    asset_type: str | None = "stock",
    category: str | None = None,
    sector: str | None = None,
    min_market_cap: float | None = None,
    max_per: float | None = None,
    max_pbr: float | None = None,
    min_dividend_yield: float | None = None,
    min_analyst_buy: float | None = None,
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

    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
        "sector": sector,
        "min_market_cap": min_market_cap,
        "max_per": max_per,
        "max_pbr": max_pbr,
        "min_dividend_yield": min_dividend_yield_normalized,
        "min_analyst_buy": min_analyst_buy,
        "min_rsi": min_rsi,
        "max_rsi": max_rsi,
        "min_adx": min_adx,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "limit": limit,
    }
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = (
            min_dividend_yield_normalized
        )

    result = _init_tvscreener_result(filters_applied)

    try:
        # Phase 1: Build filters
        build_result = _build_kr_filters(
            min_rsi=min_rsi,
            max_rsi=max_rsi,
            min_adx=min_adx,
        )
        if isinstance(build_result, str):
            logger.warning("[Screen-KR-TV] %s", build_result)
            result["error"] = build_result
            return result

        # Phase 2: Execute query
        df = await _execute_kr_query(
            columns=build_result["columns"],
            where_conditions=build_result["where_conditions"],
            Market=build_result["Market"],
            min_rsi=min_rsi,
            max_rsi=max_rsi,
            min_adx=min_adx,
            limit=limit,
        )

        if df.empty:
            logger.info("[Screen-KR-TV] StockScreener returned no results")
            return result

        logger.info("[Screen-KR-TV] StockScreener returned %d Korean stocks", len(df))

        # Phase 3: Normalize results
        stocks = await _normalize_kr_results(df, market=market)

        stocks = _filter_by_min_analyst_buy(stocks, min_analyst_buy)

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
    sector: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    min_analyst_buy: float | None,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
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
                sector=sector,
                min_market_cap=min_market_cap,
                max_per=max_per,
                max_pbr=max_pbr,
                min_dividend_yield=min_dividend_yield,
                min_analyst_buy=min_analyst_buy,
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
    )
