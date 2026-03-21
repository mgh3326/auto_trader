"""TvScreener capability checks, row mapping, and response adaptation."""

from __future__ import annotations

import logging
from typing import Any

from app.mcp_server.tooling.screening.common import (
    _build_screen_response,
    _clean_text,
    _get_first_present,
    _timeout_seconds,
    _to_optional_float,
    _to_optional_int,
)
from app.mcp_server.tooling.screening.enrichment import (
    _compute_target_upside_pct,
)
from app.services.tvscreener_service import (
    TvScreenerCapabilitySnapshot,
    TvScreenerService,
)

logger = logging.getLogger(__name__)


def _required_tvscreener_stock_capabilities(
    *,
    market: str,
    asset_type: str | None,
    category: str | None,
    sort_by: str,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
) -> set[str]:
    if market in {"kr", "kospi", "kosdaq"}:
        if asset_type not in {None, "stock"} or category is not None:
            return set()
        return {"volume", "change_rate", "rsi", "adx"}

    if market == "us":
        if asset_type not in {None, "stock"}:
            return set()

        required = {"volume", "change_rate", "rsi", "adx"}
        if sort_by == "market_cap" or min_market_cap is not None:
            required.add("market_cap")
        if max_per is not None:
            required.add("pe")
        if sort_by == "dividend_yield" or min_dividend_yield is not None:
            required.add("dividend_yield")
        if category is not None:
            required.add("sector")
        return required

    return set()


async def _get_tvscreener_stock_capability_snapshot(
    *,
    market: str,
    asset_type: str | None,
    category: str | None,
    sort_by: str,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
) -> TvScreenerCapabilitySnapshot | None:
    required_capabilities = _required_tvscreener_stock_capabilities(
        market=market,
        asset_type=asset_type,
        category=category,
        sort_by=sort_by,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
    )
    if not required_capabilities:
        return None

    try:
        tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))
        return await tvscreener_service.get_stock_capabilities(
            market=market,
            capability_names=required_capabilities,
        )
    except Exception as exc:
        logger.debug(
            "tvscreener stock capability snapshot failed for %s: %s: %s",
            market,
            type(exc).__name__,
            exc,
        )
        return None


def _can_use_tvscreener_stock_path(
    *,
    market: str,
    asset_type: str | None,
    category: str | None,
    sort_by: str,
    min_market_cap: float | None,
    max_per: float | None,
    min_dividend_yield: float | None,
    capability_snapshot: TvScreenerCapabilitySnapshot | None,
) -> bool:
    if capability_snapshot is None:
        return False

    required_capabilities = _required_tvscreener_stock_capabilities(
        market=market,
        asset_type=asset_type,
        category=category,
        sort_by=sort_by,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
    )
    if not required_capabilities:
        return False

    return all(
        capability_snapshot.is_usable(capability_name)
        for capability_name in required_capabilities
    )


def _map_tvscreener_stock_row(
    row: dict[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Map a tvscreener result row to standardized candidate format."""
    mapped: dict[str, Any] = {
        "code": row.get("symbol") or "",
        "name": row.get("name") or "",
        "close": row.get("price"),
        "change_rate": row.get("change_percent"),
        "volume": row.get("volume"),
        "market_cap": row.get("market_cap"),
        "per": row.get("per"),
        "dividend_yield": row.get("dividend_yield"),
        "rsi": row.get("rsi"),
        "adx": row.get("adx"),
        "market": row.get("market") or market,
    }
    if row.get("pbr") is not None or market in {"kr", "kospi", "kosdaq"}:
        mapped["pbr"] = row.get("pbr")
    if market == "us":
        sector = _clean_text(_get_first_present(row, "sector.tr", "sector"))
        if sector:
            mapped["sector"] = sector
        recommendation_buy = _to_optional_int(
            _get_first_present(row, "recommendation_buy", "analyst_buy")
        )
        recommendation_over = _to_optional_int(
            _get_first_present(row, "recommendation_over")
        )
        recommendation_hold = _to_optional_int(
            _get_first_present(row, "recommendation_hold", "analyst_hold")
        )
        recommendation_sell = _to_optional_int(
            _get_first_present(row, "recommendation_sell", "analyst_sell")
        )
        recommendation_under = _to_optional_int(
            _get_first_present(row, "recommendation_under")
        )
        if recommendation_buy is not None or recommendation_over is not None:
            mapped["analyst_buy"] = (recommendation_buy or 0) + (
                recommendation_over or 0
            )
        if recommendation_hold is not None:
            mapped["analyst_hold"] = recommendation_hold
        if recommendation_sell is not None or recommendation_under is not None:
            mapped["analyst_sell"] = (recommendation_sell or 0) + (
                recommendation_under or 0
            )
        avg_target = _to_optional_float(
            _get_first_present(
                row,
                "price_target_average",
                "price_target_1y",
                "avg_target",
            )
        )
        if avg_target is not None:
            mapped["avg_target"] = avg_target
        upside_pct = _to_optional_float(
            _get_first_present(row, "price_target_1y_delta", "upside_pct")
        )
        if upside_pct is None:
            upside_pct = _compute_target_upside_pct(
                avg_target=avg_target,
                current_price=_to_optional_float(mapped.get("close")),
            )
        if upside_pct is not None:
            mapped["upside_pct"] = upside_pct
    return mapped


def _adapt_tvscreener_stock_response(
    tvscreener_result: dict[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Adapt tvscreener response to standard screen response format."""
    raw_rows = tvscreener_result.get("stocks", [])
    rows = [
        _map_tvscreener_stock_row(row, market=market)
        for row in raw_rows
        if isinstance(row, dict)
    ]
    filters_applied = tvscreener_result.get("filters_applied")
    normalized_filters = (
        dict(filters_applied) if isinstance(filters_applied, dict) else {}
    )
    normalized_filters.setdefault("market", market)
    normalized_filters.setdefault("asset_type", "stock")
    normalized_filters.setdefault("category", None)
    normalized_filters.setdefault("sort_by", None)
    normalized_filters.setdefault("sort_order", "desc")
    total_count = int(tvscreener_result.get("count", len(rows)) or 0)
    return _build_screen_response(
        rows,
        total_count,
        normalized_filters,
        market,
        meta_fields={"source": "tvscreener"},
    )
