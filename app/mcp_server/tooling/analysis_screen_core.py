"""Stock screening helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import yfinance as yf

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
        if sort_by == "dividend_yield":
            raise ValueError(
                "Crypto market does not support sorting by 'dividend_yield'"
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
        "market_cap": "market_cap",
        "change_rate": "change_rate",
        "dividend_yield": "dividend_yield",
    }
    field = sort_field_map.get(sort_by, "volume")
    reverse = sort_order == "desc"
    results.sort(key=lambda x: x.get(field, 0) or 0, reverse=reverse)
    return results[:limit]


def _build_screen_response(
    results: list[dict[str, Any]],
    total_count: int,
    filters_applied: dict[str, Any],
    market: str,
) -> dict[str, Any]:
    """Build the final screening response."""
    return {
        "results": results,
        "total_count": total_count,
        "returned_count": len(results),
        "filters_applied": filters_applied,
        "market": market,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


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
    min_dividend_yield_input, min_dividend_yield_normalized = (
        _normalize_dividend_yield_threshold(min_dividend_yield)
    )

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

    if max_rsi is not None:
        subset_limit = min(len(candidates), limit * 3, 150)
        subset = candidates[:subset_limit]
        semaphore = asyncio.Semaphore(10)

        async def fetch_advanced_data(item: dict[str, Any]):
            async with semaphore:
                item_copy = item.copy()
                code = item["code"]

                if max_rsi is not None and item_copy.get("rsi") is None:
                    try:
                        df = await _fetch_ohlcv_for_indicators(
                            code, "equity_kr", count=50
                        )
                        if not df.empty and "close" in df.columns:
                            rsi_result = _calculate_rsi(df["close"])
                            if rsi_result and "14" in rsi_result:
                                item_copy["rsi"] = rsi_result["14"]
                    except Exception:
                        pass

                return item_copy

        try:
            subset_results = await asyncio.wait_for(
                asyncio.gather(
                    *[fetch_advanced_data(item) for item in subset],
                    return_exceptions=True,
                ),
                timeout=30.0,
            )
            for i, result in enumerate(subset_results):
                if not isinstance(result, Exception) and result.get("rsi") is not None:
                    candidates[i]["rsi"] = result["rsi"]
        except TimeoutError:
            pass
        except Exception:
            pass

    filters_applied.update(advanced_filters_applied)
    filters_applied["sort_by"] = sort_by
    filters_applied["sort_order"] = sort_order
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    filtered = _apply_basic_filters(
        candidates,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield_normalized,
        max_rsi=max_rsi,
    )
    results = _sort_and_limit(filtered, sort_by, sort_order, limit)
    return _build_screen_response(results, len(filtered), filters_applied, market)


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
    min_dividend_yield_input, min_dividend_yield_normalized = (
        _normalize_dividend_yield_threshold(min_dividend_yield)
    )

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

        quotes = screen_result.get("quotes", []) if isinstance(screen_result, dict) else []
        if not quotes:
            _complete_filters_applied()
            return _build_screen_response([], 0, filters_applied, market)

        results = []
        for quote in quotes:
            results.append(
                {
                    "code": quote.get("symbol"),
                    "name": quote.get("shortname") or quote.get("longname"),
                    "close": quote.get("lastprice"),
                    "change_rate": quote.get("percentchange", 0),
                    "volume": quote.get("dayvolume", 0),
                    "market_cap": quote.get("intradaymarketcap", 0),
                    "per": quote.get("peratio"),
                    "dividend_yield": quote.get("forward_dividend_yield"),
                    "market": "us",
                }
            )

        if max_rsi is not None:
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

            try:
                results_with_rsi = await asyncio.gather(
                    *[calculate_rsi_for_stock(item) for item in results],
                    return_exceptions=True,
                )
                results = [r for r in results_with_rsi if not isinstance(r, Exception)]
            except Exception:
                pass

            results = _apply_basic_filters(
                results,
                min_market_cap=None,
                max_per=None,
                max_pbr=None,
                min_dividend_yield=None,
                max_rsi=max_rsi,
            )

            pre_limit_count = len(results)
            results = _sort_and_limit(results, sort_by, sort_order, limit)
            _complete_filters_applied()
            return _build_screen_response(
                results, pre_limit_count, filters_applied, market
            )

        _complete_filters_applied()
        pre_limit_count = (
            screen_result.get("total", len(results))
            if isinstance(screen_result, dict)
            else len(results)
        )
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
) -> dict[str, Any]:
    """Screen crypto market coins using Upbit."""
    min_dividend_yield_input, min_dividend_yield_normalized = (
        _normalize_dividend_yield_threshold(min_dividend_yield)
    )

    filters_applied = {
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

        if "market_cap" not in item:
            item["market_cap"] = item.get("acc_trade_price_24h", 0)

    if max_rsi is not None:
        subset_limit = min(len(candidates), limit * 3, 150)
        subset = candidates[:subset_limit]
        semaphore = asyncio.Semaphore(10)

        async def calculate_rsi_for_coin(item: dict[str, Any]):
            async with semaphore:
                item_copy = item.copy()
                market_code = item["original_market"]

                try:
                    df = await _fetch_ohlcv_for_indicators(
                        market_code, "crypto", count=50
                    )
                    if not df.empty and "close" in df.columns:
                        rsi_result = _calculate_rsi(df["close"])
                        if rsi_result and "14" in rsi_result:
                            item_copy["rsi"] = rsi_result["14"]
                except Exception:
                    pass

                return item_copy

        try:
            subset = await asyncio.wait_for(
                asyncio.gather(*[calculate_rsi_for_coin(item) for item in subset]),
                timeout=30.0,
            )
            for i, coin in enumerate(subset[: len(candidates)]):
                candidates[i].update(coin)
        except TimeoutError:
            pass
        except Exception:
            pass

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

    filtered = _apply_basic_filters(
        candidates,
        min_market_cap=min_market_cap,
        max_per=None,
        max_pbr=None,
        min_dividend_yield=None,
        max_rsi=max_rsi,
    )
    results = _sort_and_limit(filtered, sort_by, sort_order, limit)
    return _build_screen_response(results, len(filtered), filters_applied, market)


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
