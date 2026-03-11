"""Analysis and screening MCP tool helper implementations."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.mcp_server.tooling import analysis_analyze
from app.mcp_server.tooling.analysis_rankings import (
    calculate_pearson_correlation as _calculate_pearson_correlation_impl,
)
from app.mcp_server.tooling.analysis_rankings import (
    get_crypto_rankings_impl as _get_crypto_rankings_impl,
)
from app.mcp_server.tooling.analysis_rankings import (
    get_us_rankings_impl as _get_us_rankings_impl,
)
from app.mcp_server.tooling.analysis_recommend import (
    recommend_stocks_impl as _recommend_stocks_impl_core,
)
from app.mcp_server.tooling.analysis_screen_core import (
    _normalize_asset_type,
    _normalize_screen_market,
    _normalize_sort_by,
    _normalize_sort_order,
    _screen_crypto,
    _screen_kr,
    _validate_screen_filters,
    screen_stocks_unified,
)
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload_impl,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input_impl,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type_impl,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_int as _to_int,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)

_normalize_symbol_input = _normalize_symbol_input_impl
_resolve_market_type = _resolve_market_type_impl


def _error_payload(
    source: str,
    message: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return _error_payload_impl(source=source, message=message, **kwargs)


# ---------------------------------------------------------------------------
# Change Rate Normalization
# ---------------------------------------------------------------------------


def _parse_change_rate(value: Any) -> float | None:
    val = _to_optional_float(value)
    if val is None:
        return None
    return val


def _normalize_change_rate_equity(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val


def _normalize_change_rate_crypto(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val * 100


# ---------------------------------------------------------------------------
# Ranking Row Mapping
# ---------------------------------------------------------------------------


def _map_kr_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
    symbol = row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd", "")
    name = row.get("hts_kor_isnm", "")
    price = _to_float(row.get("stck_prpr"))
    change_rate = _normalize_change_rate_equity(row.get("prdy_ctrt"))
    volume = _to_int(row.get("acml_vol") or row.get("frgn_ntby_qty"))
    market_cap = _to_float(row.get("hts_avls"))
    trade_amount = _to_float(row.get("acml_tr_pbmn") or row.get("frgn_ntby_tr_pbmn"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_us_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
    symbol = row.get("symbol", "")
    name = row.get("longName", "") or row.get("shortName", symbol)
    price = _to_float(row.get("regularMarketPrice"))
    prev_close = _to_float(row.get("previousClose"))

    if price is not None and prev_close is not None and prev_close > 0:
        change_rate = ((price - prev_close) / prev_close) * 100
    else:
        change_rate = _to_float(row.get("regularMarketChangePercent", 0))

    volume = _to_int(row.get("regularMarketVolume"))
    market_cap = _to_float(row.get("marketCap"))
    trade_amount = None

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_crypto_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
    symbol = row.get("market", "")
    name = symbol.replace("KRW-", "") if symbol.startswith("KRW-") else symbol
    price = _to_float(row.get("trade_price"))
    change_rate = _normalize_change_rate_crypto(row.get("signed_change_rate"))
    volume = _to_float(row.get("acc_trade_volume_24h"))
    market_cap = None
    trade_amount = _to_float(row.get("acc_trade_price_24h"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


# ---------------------------------------------------------------------------
# Ranking Fetchers
# ---------------------------------------------------------------------------


async def _get_us_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    return await _get_us_rankings_impl(ranking_type, limit, _map_us_row)


async def _get_crypto_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    return await _get_crypto_rankings_impl(ranking_type, limit, _map_crypto_row)


def _calculate_pearson_correlation(x: list[float], y: list[float]) -> float:
    return _calculate_pearson_correlation_impl(x, y)


# ---------------------------------------------------------------------------
# Analyze Stock Helpers
# ---------------------------------------------------------------------------


async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
    """Fetch quote data for any market type."""
    if market_type == "crypto":
        return await _fetch_quote_crypto(symbol)
    if market_type == "equity_kr":
        return await _fetch_quote_equity_kr(symbol)
    if market_type == "equity_us":
        return await _fetch_quote_equity_us(symbol)
    return None


def _build_kr_quote_from_ohlcv(
    symbol: str, ohlcv_df: pd.DataFrame
) -> dict[str, Any] | None:
    if ohlcv_df.empty:
        return None
    last = ohlcv_df.iloc[-1].to_dict()
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": last.get("close"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }


async def _get_indicators_impl(
    symbol: str,
    indicators: list[str],
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import _get_indicators_impl as _impl

    return await _impl(symbol, indicators, market, preloaded_df=preloaded_df)


async def _get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals_handlers import (
        _get_support_resistance_impl as _impl,
    )

    return await _impl(symbol, market, preloaded_df=preloaded_df)


async def _analyze_stock_impl(
    symbol: str,
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    return await analysis_analyze.analyze_stock_impl(
        symbol=symbol,
        market=market,
        include_peers=include_peers,
    )


# ---------------------------------------------------------------------------
# Screen Stocks Helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Recommend Stocks Helpers
# ---------------------------------------------------------------------------


async def _recommend_stocks_impl(
    *,
    budget: float,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None,
    sectors: list[str] | None,
    max_positions: int,
    exclude_held: bool = True,
    top_stocks_fallback: Any,
) -> dict[str, Any]:
    return await _recommend_stocks_impl_core(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        exclude_held=exclude_held,
        top_stocks_fallback=top_stocks_fallback,
        screen_kr_fn=_screen_kr,
        screen_crypto_fn=_screen_crypto,
        top_stocks_override=top_stocks_fallback,
    )


# ---------------------------------------------------------------------------
# Public Helper Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_error_payload",
    "_map_kr_row",
    "_map_us_row",
    "_map_crypto_row",
    "_get_us_rankings",
    "_get_crypto_rankings",
    "_calculate_pearson_correlation",
    "_get_quote_impl",
    "_analyze_stock_impl",
    "_recommend_stocks_impl",
    "_normalize_screen_market",
    "_normalize_asset_type",
    "_normalize_sort_by",
    "_normalize_sort_order",
    "_validate_screen_filters",
    "screen_stocks_unified",
]
