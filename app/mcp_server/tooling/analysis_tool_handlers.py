"""MCP analysis tool handlers.

This module contains the MCP callable implementations for analysis and screening tools.
Tool registration itself is handled separately in ``analysis_registration.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any, Literal

import httpx
import yfinance as yf

from app.mcp_server.tooling import analysis_screening
from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_session import (
    DATA_STATE_FRESH,
    kr_market_data_state,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.monitoring import yfinance_tracing_session
from app.services.brokers.kis.client import KISClient

logger = logging.getLogger(__name__)

_CORRELATION_COMPANY_NAME_ERROR = (
    "get_correlation does not support company-name inputs because it has no "
    "market parameter. Use ticker/code inputs directly."
)


def _looks_like_correlation_company_name(symbol: str) -> bool:
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, None)
    if not normalized_symbol:
        return False
    if _is_crypto_market(normalized_symbol):
        return False
    if _is_korean_equity_code(normalized_symbol):
        return False
    stripped_symbol = normalized_symbol.strip()
    if any(ch.isspace() for ch in stripped_symbol):
        return True
    if not stripped_symbol.isascii():
        return True
    return False


def _resolve_correlation_symbol_input(symbol: str | int) -> tuple[str, str]:
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, None)
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if _looks_like_correlation_company_name(normalized_symbol):
        raise ValueError(_CORRELATION_COMPANY_NAME_ERROR)
    return analysis_screening._resolve_market_type(normalized_symbol, None)


try:
    from app.services.disclosures.dart import list_filings
except ImportError:
    list_filings = None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def get_top_stocks_impl(
    market: str = "kr",
    ranking_type: str = "volume",
    limit: int = 20,
) -> dict[str, Any]:
    market = (market or "").strip().lower()
    ranking_type = (ranking_type or "").strip().lower()
    limit_clamped = max(1, min(limit, 50))

    supported_combinations = {
        ("kr", "volume"),
        ("kr", "market_cap"),
        ("kr", "gainers"),
        ("kr", "losers"),
        ("kr", "foreigners"),
        ("us", "volume"),
        ("us", "market_cap"),
        ("us", "gainers"),
        ("us", "losers"),
        ("crypto", "volume"),
        ("crypto", "gainers"),
        ("crypto", "losers"),
        ("crypto", "relative_strength"),
    }

    key = (market, ranking_type)
    if key not in supported_combinations:
        return analysis_screening._error_payload(
            source="validation",
            message=f"Unsupported combination: market={market}, ranking_type={ranking_type}",
            query=f"market={market}, ranking_type={ranking_type}",
        )

    fetch_limit = limit_clamped
    rankings: list[dict[str, Any]] = []
    source = {"kr": "kis", "us": "yfinance", "crypto": "upbit"}.get(
        market,
        "",
    )

    try:
        if market == "kr":
            kis = KISClient()

            if ranking_type == "volume":
                data = await kis.volume_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type == "market_cap":
                data = await kis.market_cap_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type in ("gainers", "losers"):
                direction = "up" if ranking_type == "gainers" else "down"
                data = await kis.fluctuation_rank(
                    market="J", direction=direction, limit=fetch_limit
                )
                source = "kis"
            elif ranking_type == "foreigners":
                data = await kis.foreign_buying_rank(market="J", limit=fetch_limit)
                source = "kis"
            else:
                data = []

            filtered_rank = 1
            for row in data[:fetch_limit]:
                if ranking_type == "losers":
                    change_rate = analysis_screening._to_float(row.get("prdy_ctrt"))
                    if change_rate is None or change_rate >= 0:
                        continue

                mapped = analysis_screening._map_kr_row(row, filtered_rank)
                rankings.append(mapped)
                filtered_rank += 1
                if len(rankings) >= limit_clamped:
                    break

        elif market == "us":
            rankings, source = await analysis_screening._get_us_rankings(
                ranking_type, limit_clamped
            )

        elif market == "crypto":
            rankings, source = await analysis_screening._get_crypto_rankings(
                ranking_type, limit_clamped
            )

        else:
            return analysis_screening._error_payload(
                source="validation",
                message=f"Unsupported market: {market}",
                query=f"market={market}",
            )

    except Exception as exc:
        return analysis_screening._error_payload(
            source=source,
            message=str(exc),
        )

    kst_tz = datetime.timezone(datetime.timedelta(hours=9))

    # ROB-464: outside the KRX regular session the gainers/losers rankings come
    # back with every change rate at 0, alphabetically ordered — not a real
    # ranking. Suppress that fake-0 garbage and tag the session instead of
    # presenting it as live data.
    data_state: str | None = None
    if market == "kr":
        data_state = kr_market_data_state()
        if ranking_type in ("gainers", "losers"):
            has_real_move = any(r.get("change_rate") for r in rankings)
            if data_state != DATA_STATE_FRESH and not has_real_move:
                return {
                    "rankings": [],
                    "total_count": 0,
                    "market": market,
                    "ranking_type": ranking_type,
                    "timestamp": datetime.datetime.now(kst_tz).isoformat(),
                    "source": source,
                    "data_state": data_state,
                    "note": (
                        "KRX is not in regular session; gainers/losers come back "
                        "with all change rates at 0 (not a real ranking). Returning "
                        "no rows instead of fake-0 entries — retry during market "
                        "hours (09:00–15:30 KST)."
                    ),
                }

    if len(rankings) == 0 and market == "kr" and ranking_type == "losers":
        return analysis_screening._error_payload(
            source="kis",
            message=(
                "No losing stocks found. "
                "Market may be entirely bullish or KIS API limitation."
            ),
            query="market=kr, ranking_type=losers",
            suggestion=(
                "This could indicate no stocks are declining, "
                "or the KIS API may have limited data for this ranking type."
            ),
        )

    response: dict[str, Any] = {
        "rankings": rankings,
        "total_count": len(rankings),
        "market": market,
        "ranking_type": ranking_type,
        "timestamp": datetime.datetime.now(kst_tz).isoformat(),
        "source": source,
    }
    if data_state is not None:
        response["data_state"] = data_state
    return response


async def get_crypto_top_movers_impl(
    ranking_type: str = "relative_strength",
    limit: int = 20,
) -> dict[str, Any]:
    normalized = (ranking_type or "relative_strength").strip().lower()
    aliases = {
        "relative": "relative_strength",
        "relative_strength_vs_btc": "relative_strength",
        "rs": "relative_strength",
        "value": "volume",
        "trade_amount": "volume",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"relative_strength", "volume", "gainers", "losers"}:
        return analysis_screening._error_payload(
            source="validation",
            message=(
                "Unsupported crypto ranking_type: "
                f"{ranking_type}; allowed: relative_strength, volume, gainers, losers"
            ),
            query=f"ranking_type={ranking_type}",
        )
    return await get_top_stocks_impl(
        market="crypto",
        ranking_type=normalized,
        limit=limit,
    )


async def get_disclosures_impl(
    symbol: str,
    days: int = 30,
    limit: int = 20,
    report_type: str | None = None,
) -> dict[str, Any]:
    if list_filings is None:
        return {
            "success": False,
            "error": "DART functionality not available",
            "filings": [],
            "symbol": symbol,
        }

    try:
        return await list_filings(symbol, days, limit, report_type)
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "filings": [],
            "symbol": symbol,
        }


async def get_correlation_impl(
    symbols: list[str],
    period: int = 60,
) -> dict[str, Any]:
    if not symbols or len(symbols) < 2:
        raise ValueError("symbols must contain at least 2 assets")

    if len(symbols) > 10:
        raise ValueError("Maximum 10 symbols supported for correlation calculation")

    period = max(period, 30)
    if period > 365:
        raise ValueError("period must be between 30 and 365 days")

    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    errors: list[str] = []
    company_name_validation_errors: list[str] = []
    price_data: dict[str, list[float]] = {}
    market_types: dict[str, str] = {}

    async def fetch_prices(
        symbol: str,
    ) -> tuple[str | None, str | None, list[float] | None, str | None, bool]:
        try:
            market_type, normalized_symbol = _resolve_correlation_symbol_input(symbol)
        except Exception as exc:
            return (
                None,
                None,
                None,
                f"{symbol}: {str(exc)}",
                str(exc) == _CORRELATION_COMPANY_NAME_ERROR,
            )

        try:
            df = await _fetch_ohlcv_for_indicators(
                normalized_symbol,
                market_type,
                count=period,
            )
            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")
            if "close" not in df.columns:
                raise ValueError(f"Missing close price data for symbol '{symbol}'")

            prices = df["close"].tolist()
            return normalized_symbol, market_type, prices, None, False
        except Exception as exc:
            return None, None, None, f"{symbol}: {str(exc)}", False

    results = await asyncio.gather(*[fetch_prices(sym) for sym in symbols])

    for normalized_symbol, market_type, prices, error, is_validation_error in results:
        if error is not None:
            errors.append(error)
            if is_validation_error:
                company_name_validation_errors.append(error)
            continue
        if normalized_symbol is None or market_type is None or prices is None:
            continue
        market_types[normalized_symbol] = market_type
        price_data[normalized_symbol] = prices

    if len(price_data) < 2:
        if company_name_validation_errors:
            return {
                "success": False,
                "error": _CORRELATION_COMPANY_NAME_ERROR,
                "errors": errors,
            }
        return {
            "success": False,
            "error": (
                "Insufficient data to calculate correlation (need at least 2 symbols)"
            ),
        }

    correlation_matrix: list[list[float]] = []
    sorted_symbols = sorted(price_data.keys())

    for i, sym_a in enumerate(sorted_symbols):
        row: list[float] = []
        prices_a = price_data[sym_a]
        min_len = len(prices_a)

        for j, sym_b in enumerate(sorted_symbols):
            prices_b = price_data[sym_b]
            actual_len = min(len(prices_b), min_len)

            corr = 0.0
            if i <= j:
                truncated_a = prices_a[-actual_len:]
                truncated_b = prices_b[-actual_len:]
                corr = (
                    analysis_screening._calculate_pearson_correlation(
                        truncated_a, truncated_b
                    )
                    if len(truncated_a) >= 2
                    else 0.0
                )
            else:
                corr = correlation_matrix[j][i]

            row.append(corr)
        correlation_matrix.append(row)

    metadata = {
        "period_days": period,
        "symbols": sorted_symbols,
        "market_types": {
            sym: market_types.get(sym, "unknown") for sym in sorted_symbols
        },
        "sources": {
            sym: source_map.get(market_types.get(sym, "equity_us"), "unknown")
            for sym in sorted_symbols
        },
    }

    if errors:
        return {
            "success": True,
            "correlation_matrix": correlation_matrix,
            "symbols": sorted_symbols,
            "metadata": metadata,
            "errors": errors,
        }

    return {
        "success": True,
        "correlation_matrix": correlation_matrix,
        "symbols": sorted_symbols,
        "metadata": metadata,
    }


async def analyze_stock_impl(
    symbol: str | int,
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, market)
    result = analysis_screening._analyze_stock_impl(
        normalized_symbol, market, include_peers
    )
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _run_batch_analysis(
    symbols: list[str | int],
    *,
    market: str | None,
    include_peers: bool,
    formatter: Callable[..., dict[str, Any]],
    include_position: bool = False,
) -> dict[str, Any]:
    """Shared batch analysis executor for portfolio and stock batch analysis.

    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        formatter: Callable that receives (normalized_symbol, analysis_result)
            and an optional ``position_index`` keyword, returning the formatted
            result.
        include_position: When True (ROB-541), make ONE batched holdings fetch
            for the whole batch and pass the in-memory position index to the
            formatter. Never fans out per symbol.

    Returns:
        Dict with 'results' (symbol -> formatted_result) and 'summary' keys

    Raises:
        ValueError: If symbols list is empty or exceeds 10 entries
    """
    if not symbols:
        raise ValueError("symbols must contain at least one entry")

    if len(symbols) > 10:
        raise ValueError("symbols must contain at most 10 entries")

    normalized_symbols = [
        analysis_screening._normalize_symbol_input(s, market) for s in symbols
    ]
    results: dict[str, Any] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(5)

    position_index: dict[str, list[dict[str, Any]]] | None = None
    if include_position:
        # ONE batched holdings fetch for the WHOLE batch — never per symbol.
        position_index, position_error = await _build_batch_position_index(market)
        if position_error:
            errors.append(position_error)

    async def _analyze_one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                result = analysis_screening._analyze_stock_impl(
                    sym, market, include_peers
                )
                if asyncio.iscoroutine(result):
                    return await result
                return result
            except Exception as exc:
                errors.append(f"{sym}: {str(exc)}")
                return {"symbol": sym, "error": str(exc)}

    analyze_results = await asyncio.gather(
        *[_analyze_one(s) for s in normalized_symbols]
    )

    # Group successful results by market_type to batch resolve names
    market_to_symbols = {}
    for sym, res in zip(normalized_symbols, analyze_results, strict=True):
        if "error" not in res:
            mtype = res.get("market_type")
            if mtype:
                market_to_symbols.setdefault(mtype, []).append(sym)

    # Call resolve_names for each market type
    resolved_info = {}
    if market_to_symbols:
        from app.mcp_server.tooling.name_resolution import resolve_names

        resolution_tasks = []
        mtypes = list(market_to_symbols.keys())
        for mtype in mtypes:
            resolution_tasks.append(resolve_names(market_to_symbols[mtype], mtype))
        resolution_results = await asyncio.gather(*resolution_tasks)
        for res_dict in resolution_results:
            resolved_info.update(res_dict)

    # Inject name and name_resolved into each result
    for sym, res in zip(normalized_symbols, analyze_results, strict=True):
        if "error" not in res:
            info = resolved_info.get(sym) or {"name": sym, "name_resolved": False}
            res["name"] = info["name"]
            res["name_resolved"] = info["name_resolved"]

    success_count = 0
    fail_count = 0
    for sym, result in zip(normalized_symbols, analyze_results, strict=True):
        if position_index is not None:
            formatted_result = formatter(sym, result, position_index=position_index)
        else:
            formatted_result = formatter(sym, result)
        results[sym] = formatted_result
        if "error" not in result:
            success_count += 1
        else:
            fail_count += 1

    return {
        "results": results,
        "summary": {
            "total_symbols": len(normalized_symbols),
            "successful": success_count,
            "failed": fail_count,
            "errors": errors,
        },
    }


def _position_index_key(symbol: str, instrument_type: str) -> str:
    """Normalized lookup key for the in-memory position index (ROB-541)."""
    from app.mcp_server.tooling.shared import normalize_position_symbol

    inst = instrument_type or ""
    if inst in {"crypto", "equity_us", "equity_kr"}:
        return normalize_position_symbol(symbol, inst).upper()
    return symbol.strip().upper()


async def _build_batch_position_index(
    market: str | None,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Fetch holdings ONCE for the batch and index them by normalized symbol.

    ROB-541: fail-open — any holdings failure returns an empty index plus a
    warning string so analysis never breaks on a holdings outage. ``position``
    is then ``null`` for every symbol (not-held semantics), never fabricated.
    """
    from app.mcp_server.tooling.portfolio_holdings import (
        _account_order_routable,
        _collect_portfolio_positions,
        _provenance_account_mode,
    )

    index: dict[str, list[dict[str, Any]]] = {}
    try:
        positions, _errors, _market, _account = await _collect_portfolio_positions(
            account=None,
            market=market,
            include_current_price=False,
        )
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        logger.warning("analyze_stock_batch holdings lookup failed: %s", detail)
        return index, f"보유 종목 조회 실패: {detail}"

    for position in positions:
        symbol = str(position.get("symbol") or "")
        instrument_type = str(position.get("instrument_type") or "")
        if not symbol:
            continue
        source = position.get("source")
        entry = {
            "account": position.get("account"),
            "account_mode": _provenance_account_mode(
                broker=position.get("broker"),
                source=source,
                routing_mode="kis_live",
            ),
            "qty": position.get("quantity"),
            "avg_buy_price": position.get("avg_buy_price"),
            "pnl_pct": position.get("profit_rate"),
            "order_routable": _account_order_routable(
                source=source, broker=position.get("broker")
            ),
            # Internal-only fields for symbol matching — stripped before output.
            "_symbol": symbol,
            "_instrument_type": instrument_type,
        }
        index.setdefault(_position_index_key(symbol, instrument_type), []).append(entry)
    return index, None


def _lookup_position_for_symbol(
    *,
    symbol: str,
    market_type: str,
    position_index: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]] | None:
    """Resolve held positions for ``symbol`` from the prebuilt index (ROB-541).

    Uses ``is_position_symbol_match`` (equity_us dot/slash + crypto-base aware)
    rather than naive upper() so we never join the wrong position. Returns a
    LIST (one entry per holding account) or ``None`` when not held; never
    OR-collapses routability across accounts.
    """
    from app.mcp_server.tooling.portfolio_helpers import is_position_symbol_match

    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Fast path: direct normalized-key hit.
    candidates = list(position_index.get(_position_index_key(symbol, market_type), []))
    # Fallback: scan all entries so equity_us dot/slash variants still match even
    # when the index key normalization differs from the query normalization.
    if not candidates:
        for entries in position_index.values():
            candidates.extend(entries)
    for entry in candidates:
        pos_symbol = str(entry.get("_symbol") or "")
        pos_inst = str(entry.get("_instrument_type") or market_type)
        if not pos_symbol:
            continue
        dedupe_key = f"{entry.get('account')}|{pos_symbol}"
        if dedupe_key in seen:
            continue
        if is_position_symbol_match(
            position_symbol=pos_symbol,
            query_symbol=symbol,
            instrument_type=pos_inst or market_type,
        ):
            seen.add(dedupe_key)
            matched.append({k: v for k, v in entry.items() if not k.startswith("_")})
    return matched or None


def _summarize_analysis_result(
    symbol: str,
    analysis: dict[str, Any],
    *,
    position_index: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Convert full analysis into compact summary for batch responses.

    ROB-541: when ``position_index`` is provided (include_position=True), the
    compact summary carries a ``position`` field — a LIST (one entry per holding
    account, because a symbol may be held in multiple accounts e.g. toss+samsung)
    or ``None`` when not held.
    """
    # If result is an error, pass through unchanged
    if "error" in analysis:
        return analysis

    quote = analysis.get("quote") or {}
    # ROB-451: production stores a FLAT indicator map ({"rsi": {"14": ...}}) — the unwrap
    # already happens in analysis_analyze.py. The old `.get("indicators", {})` assumed the
    # pre-unwrap provider shape, so it found nothing → rsi_14 was ALWAYS null (all markets).
    # Defensively accept both: nested provider payload OR the already-flat map.
    raw_indicators = analysis.get("indicators") or {}
    inner = raw_indicators.get("indicators")
    indicators = inner if isinstance(inner, dict) else raw_indicators
    rsi = (indicators.get("rsi") or {}).get("14")
    sr = analysis.get("support_resistance") or {}

    summary: dict[str, Any] = {
        "symbol": symbol,
        "name": analysis.get("name"),
        "name_resolved": analysis.get("name_resolved", False),
        "market_type": analysis.get("market_type"),
        "source": analysis.get("source"),
        "current_price": quote.get("price") or quote.get("current_price"),
        "rsi_14": rsi,
        "consensus": ((analysis.get("opinions") or {}).get("consensus")),
        "recommendation": analysis.get("recommendation"),
        # NOSONAR python:S6466 — list slicing never raises IndexError
        "supports": (sr.get("supports") or [])[:3],  # NOSONAR
        "resistances": (sr.get("resistances") or [])[:3],  # NOSONAR
    }
    if position_index is not None:
        summary["position"] = _lookup_position_for_symbol(
            symbol=symbol,
            market_type=str(analysis.get("market_type") or ""),
            position_index=position_index,
        )
    return summary


async def analyze_stock_batch_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    quick: bool = True,
    include_position: bool = True,
) -> dict[str, Any]:
    """Analyze multiple symbols and return compact per-symbol summaries.
    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        quick: If True, return compact summary; if False, return full analysis
        include_position: ROB-541 — when True (default) and quick=True, attach a
            per-account holdings 'position' array (or null) to each compact
            summary via a SINGLE batched holdings fetch.
    Returns:
        Dict with 'results' (symbol -> summary) and 'summary' keys
    """
    # Position attach only applies to the compact summary contract. The full
    # payload (quick=False) is returned verbatim and never carries 'position'.
    attach_position = include_position and quick

    if quick:

        def formatter(
            _sym: str,
            result: dict[str, Any],
            *,
            position_index: dict[str, list[dict[str, Any]]] | None = None,
        ) -> dict[str, Any]:
            return _summarize_analysis_result(
                _sym, result, position_index=position_index
            )
    else:

        def formatter(
            _sym: str,
            result: dict[str, Any],
            *,
            position_index: dict[str, list[dict[str, Any]]] | None = None,
        ) -> dict[str, Any]:
            return result

    return await _run_batch_analysis(
        symbols,
        market=market,
        include_peers=include_peers,
        formatter=formatter,
        include_position=attach_position,
    )


async def analyze_portfolio_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    include_rotation_plan: bool = False,
) -> dict[str, Any]:
    """Analyze a portfolio of symbols.

    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        include_rotation_plan: Whether to append rotation plan for crypto

    Returns:
        Dict with 'results' (symbol -> analysis_result) and 'summary' keys
    """
    result = await _run_batch_analysis(
        symbols,
        market=market,
        include_peers=include_peers,
        formatter=lambda _sym, result: result,
    )

    if include_rotation_plan:
        from app.services.portfolio_rotation_service import PortfolioRotationService

        rotation_service = PortfolioRotationService()
        result["rotation_plan"] = await rotation_service.build_rotation_plan(
            market=market or "crypto",
        )

    return result


async def screen_stocks_impl(
    market: Literal["kr", "kospi", "kosdaq", "konex", "all", "us", "crypto"] = "kr",
    asset_type: Literal["stock", "etf", "etn"] | None = None,
    category: str | None = None,
    sector: str | None = None,
    strategy: str | None = None,
    sort_by: Literal[
        "volume",
        "trade_amount",
        "market_cap",
        "change_rate",
        "dividend_yield",
        "rsi",
    ]
    | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    min_market_cap: float | None = None,
    max_per: float | None = None,
    max_pbr: float | None = None,
    min_dividend_yield: float | None = None,
    min_dividend: float | None = None,
    min_analyst_buy: float | None = None,
    max_rsi: float | None = None,
    exclude_sectors: list[str] | None = None,
    instrument_types: list[str] | None = None,
    adv_krw_min: int | None = None,
    market_cap_min_krw: int | None = None,
    market_cap_max_krw: int | None = None,
    min_consecutive_up_days: int | None = None,
    min_week_change_rate: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    sort_by_specified = sort_by is not None
    strategy_applied = False

    strategy_presets: dict[str, dict[str, Any]] = {
        "oversold": {"max_rsi": 30.0, "sort_by": "volume", "sort_order": "desc"},
        "momentum": {"sort_by": "change_rate", "sort_order": "desc"},
        "high_volume": {"sort_by": "volume", "sort_order": "desc"},
    }
    if strategy:
        strategy_key = strategy.strip().lower()
        if strategy_key not in strategy_presets:
            valid = ", ".join(sorted(strategy_presets.keys()))
            raise ValueError(f"screen strategy must be one of: {valid}")
        preset = strategy_presets[strategy_key]
        strategy_applied = True
        if not (sort_by_specified and str(market).strip().lower() == "crypto"):
            sort_by = preset.get("sort_by", sort_by)
        sort_order = preset.get("sort_order", sort_order)
        if max_rsi is None and "max_rsi" in preset:
            max_rsi = preset["max_rsi"]

    normalized_request = normalize_screen_request(
        market=market,
        asset_type=asset_type,
        category=category,
        sector=sector,
        strategy=strategy,
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
        min_consecutive_up_days=min_consecutive_up_days,
        min_week_change_rate=min_week_change_rate,
    )

    normalized_market = analysis_screening._normalize_screen_market(market)
    normalized_asset_type = analysis_screening._normalize_asset_type(asset_type)
    normalized_sort_by = analysis_screening._normalize_sort_by(sort_by)
    normalized_sort_order = analysis_screening._normalize_sort_order(sort_order)

    if normalized_market == "crypto" and not sort_by_specified:
        if strategy_applied:
            if normalized_sort_by == "volume":
                normalized_sort_by = "trade_amount"
        else:
            normalized_sort_by = "rsi"
            normalized_sort_order = "asc"

    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > 100:
        limit = 100

    analysis_screening._validate_screen_filters(
        market=normalized_market,
        asset_type=normalized_asset_type,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=normalized_request["min_dividend_yield"],
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
        adv_krw_min=normalized_request["adv_krw_min"],
        market_cap_min_krw=normalized_request["market_cap_min_krw"],
        market_cap_max_krw=normalized_request["market_cap_max_krw"],
        instrument_types=normalized_request["instrument_types"],
        exclude_sectors=normalized_request["exclude_sectors"],
    )
    # Use unified screening with automatic data source selection.  Streak filtering
    # happens after OHLCV enrichment, so fetch a wider candidate pool first;
    # otherwise the preset can return 0 simply because the first page had no
    # qualifying streak rows.
    query_limit = limit
    if (
        (min_consecutive_up_days is not None or min_week_change_rate is not None)
        and normalized_market in {"kr", "kospi", "kosdaq", "konex", "all", "us"}
        and normalized_asset_type in {None, "stock"}
    ):
        query_limit = min(limit * 5, 100)

    result = await analysis_screening.screen_stocks_unified(
        market=normalized_market,
        asset_type=normalized_asset_type,
        category=category,
        sector=sector,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        min_dividend=min_dividend,
        min_analyst_buy=min_analyst_buy,
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
        sort_order=normalized_sort_order,
        limit=query_limit,
        exclude_sectors=exclude_sectors,
        instrument_types=instrument_types,
        adv_krw_min=adv_krw_min,
        market_cap_min_krw=market_cap_min_krw,
        market_cap_max_krw=market_cap_max_krw,
    )
    if min_consecutive_up_days is not None or min_week_change_rate is not None:
        from app.mcp_server.tooling.screening.common import (
            _apply_min_consecutive_up_days,
            _apply_min_week_change_rate,
        )
        from app.mcp_server.tooling.screening.enrichment import (
            _enrich_consecutive_up_days,
        )

        rows: list[dict[str, Any]] = list(result.get("results") or [])
        await _enrich_consecutive_up_days(rows, market=normalized_market)
        if min_consecutive_up_days is not None:
            rows = _apply_min_consecutive_up_days(
                rows, threshold=min_consecutive_up_days
            )
        if min_week_change_rate is not None:
            rows = _apply_min_week_change_rate(
                rows, threshold=float(min_week_change_rate)
            )
        filters_applied = dict(result.get("filters_applied") or {})
        if filters_applied:
            filters_applied["limit"] = limit
        result = {
            **result,
            "filters_applied": filters_applied or result.get("filters_applied"),
            "results": rows[:limit],
            "total_count": len(rows),
        }
    return result


async def recommend_stocks_impl(
    budget: float,
    market: str = "kr",
    strategy: str = "balanced",
    exclude_symbols: list[str] | None = None,
    sectors: list[str] | None = None,
    max_positions: int = 5,
    exclude_held: bool = True,
) -> dict[str, Any]:
    return await analysis_screening._recommend_stocks_impl(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        exclude_held=exclude_held,
        top_stocks_fallback=get_top_stocks_impl,
    )


async def get_dividends_impl(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    def fetch_sync(ticker: yf.Ticker) -> dict[str, Any]:
        try:
            info = ticker.info or {}

            dividend_yield = info.get("dividendYield")
            dividend_rate = info.get("dividendRate")
            ex_date = info.get("exDividendDate")

            divs = ticker.dividends
            last_div = None
            if divs is not None and not divs.empty:
                last_date = divs.index[-1]
                last_div = {
                    "date": last_date.strftime("%Y-%m-%d"),
                    "amount": float(divs.iloc[-1]),
                }

            return {
                "success": True,
                "symbol": symbol.upper(),
                "dividend_yield": round(dividend_yield, 4) if dividend_yield else None,
                "dividend_rate": float(dividend_rate) if dividend_rate else None,
                "ex_dividend_date": (
                    datetime.datetime.fromtimestamp(ex_date).strftime("%Y-%m-%d")
                    if ex_date
                    else None
                ),
                "last_dividend": last_div,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "symbol": symbol.upper(),
            }

    with yfinance_tracing_session() as session:
        ticker = yf.Ticker(symbol.upper(), session=session)
        return await asyncio.to_thread(fetch_sync, ticker)


async def get_fear_greed_index_impl(days: int = 7) -> dict[str, Any]:
    capped_days = min(max(days, 1), 365)
    url = "https://api.alternative.me/fng/"
    params = {"limit": capped_days}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        if not data or "data" not in data:
            return analysis_screening._error_payload(
                source="alternative.me",
                message="No data received from Fear & Greed API",
            )

        history_data = data["data"]
        if not history_data:
            return analysis_screening._error_payload(
                source="alternative.me",
                message="Empty data array received from Fear & Greed API",
            )

        current = history_data[0]
        current_value = int(current["value"])
        current_classification = current["value_classification"]
        current_timestamp = current["timestamp"]
        current_date = (
            datetime.datetime.fromtimestamp(int(current_timestamp)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if current_timestamp
            else "Unknown"
        )

        history = []
        for item in history_data:
            value = int(item["value"])
            classification = item["value_classification"]
            timestamp = item["timestamp"]
            date = (
                datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
                if timestamp
                else "Unknown"
            )
            history.append(
                {"date": date, "value": value, "classification": classification}
            )

        return {
            "success": True,
            "source": "alternative.me",
            "current": {
                "value": current_value,
                "classification": current_classification,
                "date": current_date,
            },
            "history": history,
        }

    except httpx.HTTPStatusError as exc:
        return analysis_screening._error_payload(
            source="alternative.me",
            message=f"HTTP error: {exc}",
        )
    except Exception as exc:
        return analysis_screening._error_payload(
            source="alternative.me",
            message=str(exc),
        )


__all__ = [
    "analyze_portfolio_impl",
    "analyze_stock_impl",
    "get_correlation_impl",
    "get_disclosures_impl",
    "get_dividends_impl",
    "get_fear_greed_index_impl",
    "get_top_stocks_impl",
    "recommend_stocks_impl",
    "screen_stocks_impl",
]
