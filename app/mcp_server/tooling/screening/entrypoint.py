"""Unified screening entry-point — dispatches to per-market implementations."""

from __future__ import annotations

import logging
from typing import Any

from app.mcp_server.tooling.screening.common import (
    _build_screen_response,
    _validate_screen_filters,
    normalize_screen_request,
)
from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
from app.mcp_server.tooling.screening.enrichment import (
    _decorate_screen_response_with_equity_enrichment,
)
from app.mcp_server.tooling.screening.kr import _screen_kr_with_fallback
from app.mcp_server.tooling.screening.us import _screen_us_with_fallback

logger = logging.getLogger(__name__)


async def _dispatch_kr_screen(
    normalized_request: dict[str, Any],
    *,
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    normalized_min_dividend_yield: float | None,
    max_rsi: float | None,
    query_limit: int,
) -> dict[str, Any]:
    """Dispatch screening to the KR market implementation."""
    return await _screen_kr_with_fallback(
        market=normalized_request["market"],
        asset_type=normalized_request["asset_type"],
        category=normalized_request["effective_category"],
        sector=normalized_request["sector"],
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=normalized_min_dividend_yield,
        min_analyst_buy=normalized_request["min_analyst_buy"],
        max_rsi=max_rsi,
        sort_by=normalized_request["sort_by"],
        sort_order=normalized_request["sort_order"],
        limit=query_limit,
        adv_krw_min=normalized_request["adv_krw_min"],
        market_cap_min_krw=normalized_request["market_cap_min_krw"],
        market_cap_max_krw=normalized_request["market_cap_max_krw"],
        instrument_types=normalized_request["instrument_types"],
        exclude_sectors=normalized_request["exclude_sectors"],
        exclude_sector_keys=normalized_request["exclude_sector_keys"],
    )


async def _dispatch_us_screen(
    normalized_request: dict[str, Any],
    *,
    min_market_cap: float | None,
    max_per: float | None,
    normalized_min_dividend_yield: float | None,
    max_rsi: float | None,
    query_limit: int,
) -> dict[str, Any]:
    """Dispatch screening to the US market implementation."""
    return await _screen_us_with_fallback(
        market=normalized_request["market"],
        asset_type=normalized_request["asset_type"],
        category=normalized_request["effective_category"],
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=normalized_min_dividend_yield,
        min_analyst_buy=normalized_request["min_analyst_buy"],
        max_rsi=max_rsi,
        sort_by=normalized_request["sort_by"],
        sort_order=normalized_request["sort_order"],
        limit=query_limit,
        adv_krw_min=normalized_request["adv_krw_min"],
        market_cap_min_krw=normalized_request["market_cap_min_krw"],
        market_cap_max_krw=normalized_request["market_cap_max_krw"],
        instrument_types=normalized_request["instrument_types"],
        exclude_sectors=normalized_request["exclude_sectors"],
        exclude_sector_keys=normalized_request["exclude_sector_keys"],
    )


async def _dispatch_crypto_screen(
    normalized_request: dict[str, Any],
    *,
    min_market_cap: float | None,
    max_per: float | None,
    normalized_min_dividend_yield: float | None,
    max_rsi: float | None,
    limit: int,
) -> dict[str, Any]:
    """Dispatch screening to the crypto market implementation."""
    return await _screen_crypto_with_fallback(
        market=normalized_request["market"],
        asset_type=normalized_request["asset_type"],
        category=normalized_request["effective_category"],
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=normalized_min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=normalized_request["sort_by"],
        sort_order=normalized_request["sort_order"],
        limit=limit,
    )


def _dispatch_unsupported_market(
    normalized_request: dict[str, Any],
) -> dict[str, Any]:
    """Return an empty response for unsupported markets."""
    return _build_screen_response(
        results=[],
        total_count=0,
        filters_applied={
            "market": normalized_request["market"],
            "asset_type": normalized_request["asset_type"],
            "category": normalized_request["category_for_filters"],
            "sector": normalized_request["sector"],
            "exclude_sectors": normalized_request["exclude_sectors"],
            "instrument_types": normalized_request["instrument_types"],
            "adv_krw_min": normalized_request["adv_krw_min"],
            "market_cap_min_krw": normalized_request["market_cap_min_krw"],
            "market_cap_max_krw": normalized_request["market_cap_max_krw"],
        },
        market=normalized_request["market"],
        warnings=[f"Unsupported market: {normalized_request['market']}"],
    )


async def screen_stocks_unified(
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
    sector: str | None = None,
    min_dividend: float | None = None,
    min_analyst_buy: float | None = None,
    exclude_sectors: list[str] | None = None,
    instrument_types: list[str] | None = None,
    adv_krw_min: int | None = None,
    market_cap_min_krw: int | None = None,
    market_cap_max_krw: int | None = None,
) -> dict[str, Any]:
    """Unified stock screening entry point with automatic data source selection.

    This function encapsulates the selector logic to choose between tvscreener
    and legacy implementations based on query parameters. It provides a single
    entry point for all screening operations while maintaining backward
    compatibility with existing response contracts.

    Args:
        market: Target market ("kr", "kospi", "kosdaq", "us", "crypto")
        asset_type: Asset type filter ("stock", "etf", "etn") or None
        category: Category/sector filter or None
        min_market_cap: Minimum market cap filter
        max_per: Maximum P/E ratio filter
        max_pbr: Maximum P/B ratio filter
        min_dividend_yield: Minimum dividend yield filter (decimal or percent)
        max_rsi: Maximum RSI filter (0-100)
        sort_by: Sort field ("volume", "trade_amount", "market_cap", etc.)
        sort_order: Sort order ("asc" or "desc")
        limit: Maximum results to return

    Returns:
        Standardized screening response dict with results, filters_applied,
        meta (including source), and timestamp.
    """
    normalized_request = normalize_screen_request(
        market=market,
        asset_type=asset_type,
        category=category,
        sector=sector,
        strategy=None,
        sort_by=sort_by,
        sort_order=sort_order,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        min_dividend=min_dividend,
        min_analyst_buy=min_analyst_buy,
        max_rsi=max_rsi,
        limit=limit,
        exclude_sectors=exclude_sectors,
        instrument_types=instrument_types,
        adv_krw_min=adv_krw_min,
        market_cap_min_krw=market_cap_min_krw,
        market_cap_max_krw=market_cap_max_krw,
    )
    normalized_market = normalized_request["market"]
    normalized_asset_type = normalized_request["asset_type"]
    normalized_sort_by = normalized_request["sort_by"]
    normalized_sort_order = normalized_request["sort_order"]
    normalized_min_dividend_yield = normalized_request["min_dividend_yield"]
    apply_post_enrichment_filters = (
        normalized_market in {"kr", "kospi", "kosdaq", "konex", "all", "us"}
        and normalized_asset_type in {None, "stock"}
        and (
            normalized_request["sector"] is not None
            or normalized_request["min_analyst_buy"] is not None
        )
    )
    can_avoid_overfetch = normalized_asset_type in {None, "stock"} and (
        normalized_market == "us"
        or (
            normalized_market in {"kr", "kospi", "kosdaq", "konex", "all"}
            and normalized_request["sector"] is None
            and normalized_request["min_analyst_buy"] is not None
        )
    )
    query_limit = (
        limit
        if not apply_post_enrichment_filters or can_avoid_overfetch
        else min(limit * 5, 100)
    )

    # Validate filters before processing
    _validate_screen_filters(
        market=normalized_market,
        asset_type=normalized_asset_type,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=normalized_min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
        adv_krw_min=normalized_request["adv_krw_min"],
        market_cap_min_krw=normalized_request["market_cap_min_krw"],
        market_cap_max_krw=normalized_request["market_cap_max_krw"],
        instrument_types=normalized_request["instrument_types"],
        exclude_sectors=normalized_request["exclude_sectors"],
    )

    # Route to appropriate implementation based on market
    if normalized_market in ("kr", "kospi", "kosdaq", "konex", "all"):
        response = await _dispatch_kr_screen(
            normalized_request,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            normalized_min_dividend_yield=normalized_min_dividend_yield,
            max_rsi=max_rsi,
            query_limit=query_limit,
        )
    elif normalized_market == "us":
        response = await _dispatch_us_screen(
            normalized_request,
            min_market_cap=min_market_cap,
            max_per=max_per,
            normalized_min_dividend_yield=normalized_min_dividend_yield,
            max_rsi=max_rsi,
            query_limit=query_limit,
        )
    elif normalized_market == "crypto":
        response = await _dispatch_crypto_screen(
            normalized_request,
            min_market_cap=min_market_cap,
            max_per=max_per,
            normalized_min_dividend_yield=normalized_min_dividend_yield,
            max_rsi=max_rsi,
            limit=limit,
        )
    else:
        response = _dispatch_unsupported_market(normalized_request)

    response = await _decorate_screen_response_with_equity_enrichment(
        response,
        market=normalized_market,
        limit=limit,
        sort_by=normalized_sort_by,
        sort_order=normalized_sort_order,
        sector=normalized_request["sector"],
        min_analyst_buy=normalized_request["min_analyst_buy"],
        min_dividend_yield=normalized_min_dividend_yield,
        apply_post_filters=apply_post_enrichment_filters,
    )

    filters_applied = response.get("filters_applied")
    normalized_filters_applied = (
        dict(filters_applied) if isinstance(filters_applied, dict) else {}
    )
    normalized_filters_applied.setdefault("market", normalized_market)
    normalized_filters_applied.setdefault("asset_type", normalized_asset_type)
    normalized_filters_applied.setdefault(
        "category", normalized_request["category_for_filters"]
    )
    normalized_filters_applied.setdefault("sector", normalized_request["sector"])
    normalized_filters_applied.setdefault("sort_by", normalized_sort_by)
    normalized_filters_applied.setdefault("sort_order", normalized_sort_order)
    normalized_filters_applied.setdefault("min_market_cap", min_market_cap)
    normalized_filters_applied.setdefault("max_per", max_per)
    normalized_filters_applied.setdefault("max_pbr", max_pbr)
    normalized_filters_applied.setdefault(
        "min_dividend_yield", normalized_min_dividend_yield
    )
    normalized_filters_applied.setdefault(
        "min_analyst_buy", normalized_request["min_analyst_buy"]
    )
    normalized_filters_applied.setdefault("max_rsi", max_rsi)
    normalized_filters_applied.setdefault(
        "exclude_sectors", normalized_request["exclude_sectors"]
    )
    normalized_filters_applied.setdefault(
        "instrument_types", normalized_request["instrument_types"]
    )
    normalized_filters_applied.setdefault(
        "adv_krw_min", normalized_request["adv_krw_min"]
    )
    normalized_filters_applied.setdefault(
        "market_cap_min_krw", normalized_request["market_cap_min_krw"]
    )
    normalized_filters_applied.setdefault(
        "market_cap_max_krw", normalized_request["market_cap_max_krw"]
    )
    if normalized_request["adv_krw_min"] is not None:
        meta = response.get("meta")
        if isinstance(meta, dict):
            meta.setdefault("adv_window_days", 30)
    if normalized_request["min_dividend_input"] is not None:
        normalized_filters_applied["min_dividend_input"] = normalized_request[
            "min_dividend_input"
        ]
        normalized_filters_applied["min_dividend_normalized"] = (
            normalized_min_dividend_yield
        )
        normalized_filters_applied["min_dividend_yield_input"] = normalized_request[
            "min_dividend_input"
        ]
        normalized_filters_applied["min_dividend_yield_normalized"] = (
            normalized_min_dividend_yield
        )
    return {**response, "filters_applied": normalized_filters_applied}
