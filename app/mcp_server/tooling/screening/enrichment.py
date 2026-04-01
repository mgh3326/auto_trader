"""Equity enrichment pipeline and display-name helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_screen_enrichment_kr,
    _fetch_screen_enrichment_us,
)
from app.mcp_server.tooling.screening.common import (
    _clean_text,
    _get_first_present,
    _normalize_sector_compare_key,
    _sort_and_limit,
    _to_optional_float,
    _to_optional_int,
)
from app.monitoring import build_yfinance_tracing_session

logger = logging.getLogger(__name__)

_SCREEN_ENRICHMENT_FIELDS = (
    "sector",
    "analyst_buy",
    "analyst_hold",
    "analyst_sell",
    "avg_target",
    "upside_pct",
)


def _apply_equity_enrichment_defaults(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    for field in _SCREEN_ENRICHMENT_FIELDS:
        normalized.setdefault(field, None)
    sector = _clean_text(normalized.get("sector"))
    normalized["sector"] = sector or None
    for field in ("analyst_buy", "analyst_hold", "analyst_sell"):
        normalized[field] = _to_optional_int(normalized.get(field))
    avg_target = _to_optional_float(normalized.get("avg_target"))
    normalized["avg_target"] = avg_target
    upside_pct = _to_optional_float(normalized.get("upside_pct"))
    if upside_pct is None:
        upside_pct = _compute_target_upside_pct(
            avg_target=avg_target,
            current_price=_to_optional_float(
                _get_first_present(normalized, "close", "price")
            ),
        )
    normalized["upside_pct"] = upside_pct
    return normalized


def _compute_target_upside_pct(
    *, avg_target: float | None, current_price: float | None
) -> float | None:
    if avg_target is None or current_price is None or current_price <= 0:
        return None
    return round((avg_target - current_price) / current_price * 100, 2)


def _row_has_complete_screen_enrichment(row: dict[str, Any]) -> bool:
    if row.get("sector") is None:
        return False
    if any(
        row.get(field) is None
        for field in ("analyst_buy", "analyst_hold", "analyst_sell")
    ):
        return False
    return row.get("avg_target") is not None and row.get("upside_pct") is not None


def _filter_supported_keyword_args(
    func: Any,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return kwargs

    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
        return kwargs

    accepted_names = {
        param.name
        for param in parameters
        if param.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    }
    return {name: value for name, value in kwargs.items() if name in accepted_names}


def _screen_row_symbol(row: dict[str, Any]) -> str | None:
    for key in ("code", "symbol", "short_code"):
        value = row.get(key)
        text = str(value or "").strip().upper()
        if text:
            return text
    return None


def _is_equity_stock_row(row: dict[str, Any]) -> bool:
    market = str(row.get("market") or "").strip().lower()
    if market not in {"kr", "kospi", "kosdaq", "us"}:
        return False
    asset_type = row.get("asset_type")
    if asset_type is None:
        return True
    return str(asset_type).strip().lower() == "stock"


async def _decorate_screen_rows_with_equity_enrichment(
    rows: list[dict[str, Any]],
    *,
    concurrency: int = 5,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows:
        return [], []

    normalized_rows = [_apply_equity_enrichment_defaults(row) for row in rows]
    semaphore = asyncio.Semaphore(max(1, concurrency))
    warnings: list[str] = []
    yfinance_session: Any | None = None
    if any(
        str(row.get("market") or "").strip().lower() == "us"
        and _is_equity_stock_row(row)
        and not _row_has_complete_screen_enrichment(row)
        for row in normalized_rows
    ):
        yfinance_session = build_yfinance_tracing_session()

    async def enrich_row(index: int, row: dict[str, Any]) -> None:
        if not _is_equity_stock_row(row):
            return

        symbol = _screen_row_symbol(row)
        market = str(row.get("market") or "").strip().lower()
        if not symbol:
            warnings.append(f"{market or 'unknown'}:<missing-symbol>: missing symbol")
            return
        if _row_has_complete_screen_enrichment(row):
            return

        async with semaphore:
            try:
                if market == "us":
                    enrichment_kwargs = _filter_supported_keyword_args(
                        _fetch_screen_enrichment_us,
                        {
                            "current_price": _to_optional_float(
                                _get_first_present(row, "close", "price")
                            ),
                            "session": yfinance_session,
                            "include_opinion_history": False,
                        },
                    )
                    enrichment = await _fetch_screen_enrichment_us(
                        symbol,
                        **enrichment_kwargs,
                    )
                else:
                    enrichment = await _fetch_screen_enrichment_kr(symbol)
            except Exception as exc:
                warnings.append(f"{market}:{symbol}: {type(exc).__name__}: {exc}")
                return

        if not isinstance(enrichment, dict):
            warnings.append(f"{market}:{symbol}: invalid enrichment payload")
            return

        for field in _SCREEN_ENRICHMENT_FIELDS:
            current_value = normalized_rows[index].get(field)
            incoming_value = enrichment.get(field)
            if current_value is None and incoming_value is not None:
                normalized_rows[index][field] = incoming_value

    try:
        await asyncio.gather(
            *(enrich_row(index, row) for index, row in enumerate(normalized_rows))
        )
    finally:
        if yfinance_session is not None:
            close = getattr(yfinance_session, "close", None)
            if callable(close):
                close()
    return normalized_rows, warnings


async def _decorate_screen_response_with_equity_enrichment(
    response: dict[str, Any],
    *,
    market: str,
    limit: int,
    sort_by: str,
    sort_order: str,
    sector: str | None,
    min_analyst_buy: float | None,
    min_dividend_yield: float | None,
    apply_post_filters: bool,
) -> dict[str, Any]:
    if market not in {"kr", "kospi", "kosdaq", "us"}:
        return response

    raw_results = response.get("results")
    if not isinstance(raw_results, list):
        return response

    rows = [row for row in raw_results if isinstance(row, dict)]
    if not rows:
        return {**response, "results": [], "returned_count": 0}

    candidate_rows = rows if apply_post_filters else rows[:limit]
    (
        decorated_rows,
        enrichment_warnings,
    ) = await _decorate_screen_rows_with_equity_enrichment(candidate_rows)

    if apply_post_filters:
        decorated_rows = _apply_post_enrichment_filters(
            decorated_rows,
            sector=sector,
            min_analyst_buy=min_analyst_buy,
            min_dividend_yield=min_dividend_yield,
        )
        final_rows = _sort_and_limit(decorated_rows, sort_by, sort_order, limit)
        total_count = len(decorated_rows)
    else:
        final_rows = decorated_rows[:limit]
        total_count = int(response.get("total_count", len(final_rows)) or 0)

    merged_warnings = list(response.get("warnings") or [])
    merged_warnings.extend(enrichment_warnings)
    updated_response = {
        **response,
        "results": final_rows,
        "total_count": total_count,
        "returned_count": len(final_rows),
    }
    if merged_warnings:
        updated_response["warnings"] = merged_warnings
    return updated_response


def _apply_post_enrichment_filters(
    rows: list[dict[str, Any]],
    *,
    sector: str | None,
    min_analyst_buy: float | None,
    min_dividend_yield: float | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    normalized_sector = _normalize_sector_compare_key(sector)

    for row in rows:
        if normalized_sector is not None:
            row_sector = _normalize_sector_compare_key(_clean_text(row.get("sector")))
            if row_sector != normalized_sector:
                continue

        if min_analyst_buy is not None:
            analyst_buy = _to_optional_float(row.get("analyst_buy"))
            if analyst_buy is None or analyst_buy < min_analyst_buy:
                continue

        if min_dividend_yield is not None:
            dividend_yield = _to_optional_float(row.get("dividend_yield"))
            if dividend_yield is None or dividend_yield < min_dividend_yield:
                continue

        filtered.append(row)

    return filtered


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
