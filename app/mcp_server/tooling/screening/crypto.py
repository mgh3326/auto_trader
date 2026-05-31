"""Crypto screening — tvscreener path."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, cast

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.brokers.upbit.client as upbit_service
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.analysis_screen_crypto import finalize_crypto_screen
from app.mcp_server.tooling.screening.common import (
    DROP_THRESHOLD,
    MarketCapCache,
    _clean_text,
    _compute_rsi_bucket,
    _empty_rsi_enrichment_diagnostics,
    _get_tvscreener_attr,
    _normalize_dividend_yield_threshold,
    _timeout_seconds,
    _to_optional_float,
    is_safe_drop,
)
from app.mcp_server.tooling.screening.enrichment import (
    _resolve_crypto_display_name,
)
from app.services.crypto_trade_cooldown_service import CryptoTradeCooldownService
from app.services.crypto_voting_signals import CryptoVotingSignals
from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerService,
    _import_tvscreener,
)
from app.services.upbit_symbol_universe_service import (
    get_upbit_market_display_names,
    get_upbit_warning_markets,
)
from app.utils.symbol_mapping import (
    SymbolMappingError,
    tradingview_to_upbit,
)

logger = logging.getLogger(__name__)

_CRYPTO_MARKET_CAP_CACHE = MarketCapCache(ttl=600)


def _is_upbit_krw_market_code(symbol: Any) -> bool:
    """Return True for Upbit KRW quote-market symbols only.

    TradingView's UPBIT crypto screener includes KRW, BTC, and USDT quote
    markets.  `/invest` crypto surfaces model Upbit's Korean/KRW market, so
    fallback/live screener rows must not leak BTC-* or USDT-* pairs into the
    read model.
    """
    return str(symbol or "").strip().upper().startswith("KRW-")


def _resolve_upbit_market_code_from_row(row: Any) -> str | None:
    """Resolve an Upbit market code from tvscreener or Upbit-shaped rows."""
    raw_symbol = _clean_text(row.get("symbol")).upper()
    raw_market = _clean_text(row.get("market")).upper()

    for candidate in (raw_symbol, raw_market):
        if "-" in candidate:
            return candidate

    if not raw_symbol:
        return None
    try:
        return tradingview_to_upbit(raw_symbol)
    except SymbolMappingError:
        return None


# Crypto trade cooldown service singleton
_cooldown_service: CryptoTradeCooldownService | None = None

# Crypto voting signals evaluator singleton
_voting_evaluator: CryptoVotingSignals | None = None


def _get_crypto_trade_cooldown_service() -> CryptoTradeCooldownService:
    """Get or create the crypto trade cooldown service."""
    global _cooldown_service
    if _cooldown_service is None:
        _cooldown_service = CryptoTradeCooldownService()
    return _cooldown_service


def _get_voting_evaluator() -> CryptoVotingSignals:
    """Get or create the crypto voting signals evaluator."""
    global _voting_evaluator
    if _voting_evaluator is None:
        _voting_evaluator = CryptoVotingSignals()
    return _voting_evaluator


async def _run_crypto_coingecko_fetch() -> dict[str, Any]:
    with sentry_sdk.start_span(
        op="crypto.screen.coingecko",
        name="crypto coingecko fetch",
    ) as cg_span:
        coingecko_payload = await _CRYPTO_MARKET_CAP_CACHE.get()
        cg_span.set_data("coingecko_cached", coingecko_payload.get("cached", False))
        if "stale" in coingecko_payload:
            cg_span.set_data("coingecko_stale", coingecko_payload.get("stale", False))
        if "error" in coingecko_payload:
            cg_span.set_data(
                "coingecko_error_present", bool(coingecko_payload.get("error"))
            )
        return coingecko_payload


def _build_crypto_filters(
    *,
    max_rsi: float | None,
    sort_by: str,
    sort_order: str,
    limit: int,
) -> dict[str, Any]:
    """Build tvscreener columns, where-conditions, and sort config for crypto screening.

    Returns a dict with keys: columns, where_conditions, sort_field,
    dispatch_sort_order, query_limit.
    """
    tvscreener = _import_tvscreener()
    CryptoField = tvscreener.CryptoField

    value_traded_field = _get_tvscreener_attr(CryptoField, "VALUE_TRADED")
    if value_traded_field is None:
        raise TvScreenerError("CryptoScreener VALUE_TRADED field unavailable")
    description_field = _get_tvscreener_attr(CryptoField, "DESCRIPTION")
    market_cap_field = _get_tvscreener_attr(CryptoField, "MARKET_CAP")

    columns = [
        CryptoField.NAME,
        *([description_field] if description_field is not None else []),
        CryptoField.PRICE,
        CryptoField.CHANGE_PERCENT,
        value_traded_field,
        *([market_cap_field] if market_cap_field is not None else []),
        CryptoField.RELATIVE_STRENGTH_INDEX_14,
        CryptoField.AVERAGE_DIRECTIONAL_INDEX_14,
    ]
    volume_usd_field = _get_tvscreener_attr(CryptoField, "VOLUME_24H_IN_USD")
    if volume_usd_field is not None:
        columns.append(volume_usd_field)

    where_conditions = [CryptoField.EXCHANGE == "UPBIT"]
    if max_rsi is not None:
        where_conditions.append(CryptoField.RELATIVE_STRENGTH_INDEX_14 <= max_rsi)

    sort_field_map = {
        "trade_amount": value_traded_field,
        "market_cap": market_cap_field or value_traded_field,
        "rsi": CryptoField.RELATIVE_STRENGTH_INDEX_14,
        "change_rate": CryptoField.CHANGE_PERCENT,
    }
    sort_field = sort_field_map.get(sort_by, value_traded_field)
    dispatch_sort_order = "asc" if sort_by == "rsi" else sort_order
    query_limit = max(limit * 5, 50)

    return {
        "columns": columns,
        "where_conditions": where_conditions,
        "sort_field": sort_field,
        "dispatch_sort_order": dispatch_sort_order,
        "query_limit": query_limit,
    }


async def _execute_crypto_query(
    *,
    columns: list[Any],
    where_conditions: list[Any],
    sort_field: Any,
    dispatch_sort_order: str,
    query_limit: int,
) -> Any:
    """Execute the tvscreener CryptoScreener query.

    Returns a pandas DataFrame. Raises TvScreenerError or TimeoutError on failure.
    """
    tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))
    return await tvscreener_service.query_crypto_screener(
        columns=columns,
        where_clause=where_conditions,
        sort_by=sort_field,
        ascending=(dispatch_sort_order == "asc"),
        limit=query_limit,
    )


async def _normalize_crypto_results(
    df: Any,
    *,
    market: str,
    max_rsi: float | None,
    min_market_cap: float | None,
    filters_applied: dict[str, Any],
    limit: int,
    pure_market_snapshot: bool = False,
) -> dict[str, Any]:
    """Map tvscreener crypto DataFrame to candidate rows and finalize.

    The normal MCP screening path applies warning-market, crash, and
    stop-loss-cooldown filters.  Snapshot builds pass ``pure_market_snapshot``
    so persisted rows remain provider/account-state agnostic market data.
    """
    warnings: list[str] = []
    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    # Map raw rows
    raw_results: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        upbit_symbol = _resolve_upbit_market_code_from_row(row)
        if not upbit_symbol:
            continue

        raw_results.append(
            {
                "symbol": upbit_symbol,
                "market": upbit_symbol,
                "name": _clean_text(row.get("name")),
                "description": _clean_text(row.get("description")),
                "trade_price": _to_optional_float(row.get("price")),
                "signed_change_rate": _to_optional_float(row.get("change_percent")),
                "change_rate": _to_optional_float(row.get("change_percent")),
                "acc_trade_price_24h": _to_optional_float(row.get("value_traded")),
                "tv_market_cap": _to_optional_float(row.get("market_cap")),
                "rsi": _to_optional_float(row.get("relative_strength_index_14")),
                "adx": _to_optional_float(row.get("average_directional_index_14")),
                "tv_volume_24h_in_usd": _to_optional_float(
                    row.get("volume_24h_in_usd")
                ),
            }
        )

    pre_krw_filter_total = len(raw_results)
    raw_results = [
        item for item in raw_results if _is_upbit_krw_market_code(item.get("symbol"))
    ]
    filtered_by_quote_market = pre_krw_filter_total - len(raw_results)
    if filtered_by_quote_market:
        logger.info(
            "filtered %s non-KRW Upbit crypto screener rows from %s live rows",
            filtered_by_quote_market,
            pre_krw_filter_total,
        )

    # Fetch Upbit display names and warning markets
    market_codes = [
        str(item.get("symbol") or "").strip().upper() for item in raw_results
    ]
    _db: AsyncSession = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        try:
            display_names = await get_upbit_market_display_names(market_codes, db=_db)
        except Exception as exc:
            display_names = {}
            warnings.append(
                "Upbit symbol-universe names unavailable; TradingView description/name fallback used "
                f"({type(exc).__name__}: {exc})"
            )

        warning_markets: set[str] = set()
        try:
            warning_markets = await get_upbit_warning_markets(
                quote_currency="KRW", db=_db
            )
        except Exception as exc:
            warnings.append(
                "market warning details unavailable; warning filter skipped "
                f"({type(exc).__name__}: {exc})"
            )
    finally:
        await _db.close()

    # Fetch Upbit 24h volume + trade amount (거래대금). ROB-369 B4: tvscreener
    # VALUE_TRADED is null in production, so the live Upbit ``acc_trade_price_24h``
    # (already returned by this same ticker call) is the real trade-amount source.
    ticker_volume_map: dict[str, float] = {}
    ticker_trade_amount_map: dict[str, float] = {}
    if market_codes:
        try:
            ticker_rows = await upbit_service.fetch_multiple_tickers(market_codes)
            for row in ticker_rows:
                code = str(row.get("market") or "").strip().upper()
                if not code:
                    continue
                ticker_volume_map[code] = (
                    _to_optional_float(row.get("acc_trade_volume_24h")) or 0.0
                )
                amount = _to_optional_float(row.get("acc_trade_price_24h"))
                if amount is not None:
                    ticker_trade_amount_map[code] = amount
        except Exception as exc:
            warnings.append(
                "Upbit 24h volume enrichment failed; volume_24h defaulted to 0.0 "
                f"({type(exc).__name__}: {exc})"
            )

    # BTC reference for crash filter
    btc_item = next(
        (
            item
            for item in raw_results
            if str(item.get("symbol") or "").upper() == "KRW-BTC"
        ),
        None,
    )
    btc_change_24h: float | None = None
    if btc_item is None:
        # ROB-369 B4 — KRW-BTC can be absent from the tvscreener rows, leaving
        # the crash filter with a btc_change_24h=0.0 fallback that mistakes a
        # market-wide crash for an isolated drop (wrongly filtering deep losers).
        # Recover the reference with a direct Upbit ticker fetch — but only when
        # it can change the outcome: a crash candidate (≤ DROP_THRESHOLD) exists
        # and we're in interactive screening (the filter is skipped for snapshot
        # builds). This avoids an extra Upbit call on calm screens.
        has_crash_candidate = not pure_market_snapshot and any(
            (
                _to_optional_float(
                    item.get("signed_change_rate")
                    if item.get("signed_change_rate") is not None
                    else item.get("change_rate")
                )
                or 0.0
            )
            <= DROP_THRESHOLD
            for item in raw_results
        )
        if has_crash_candidate:
            try:
                btc_rows = await upbit_service.fetch_multiple_tickers(["KRW-BTC"])
            except Exception as exc:  # noqa: BLE001 — best-effort, degrade to 0.0
                logger.warning(
                    "KRW-BTC crash-filter fallback fetch failed (%s: %s)",
                    type(exc).__name__,
                    exc,
                )
                btc_rows = []
            if btc_rows:
                btc_change_24h = _to_optional_float(
                    btc_rows[0].get("signed_change_rate")
                    or btc_rows[0].get("change_rate")
                )
            if btc_change_24h is None:
                logger.warning(
                    "KRW-BTC ticker not found in Upbit KRW crypto screener rows "
                    "and fallback fetch yielded no change rate; crash filter uses "
                    "btc_change_24h=0.0 fallback"
                )
                warnings.append(
                    "BTC 기준 데이터가 없어 급락 방어 필터를 기본값으로 적용했습니다."
                )
    else:
        btc_change_24h = _to_optional_float(
            btc_item.get("signed_change_rate") or btc_item.get("change_rate")
        )
        if btc_change_24h is None:
            btc_change_24h = 0.0
            if not pure_market_snapshot:
                logger.warning(
                    "KRW-BTC change rate is missing; crash filter uses "
                    "btc_change_24h=0.0 fallback"
                )
                warnings.append(
                    "BTC 기준 등락률이 없어 급락 방어 필터를 기본값으로 적용했습니다."
                )

    # Apply warning + crash filters for interactive screening; snapshot builds keep
    # every provider row and only carry warning metadata forward.
    filtered_by_warning = 0
    filtered_by_crash = 0
    candidates: list[dict[str, Any]] = []
    for raw_item in raw_results:
        market_code = str(raw_item.get("symbol") or "").strip().upper()
        is_warning_market = market_code in warning_markets
        if is_warning_market and not pure_market_snapshot:
            filtered_by_warning += 1
            continue

        coin_change_24h = raw_item.get("signed_change_rate")
        if coin_change_24h is None:
            coin_change_24h = raw_item.get("change_rate")
        if not pure_market_snapshot and not is_safe_drop(
            coin_change_24h, btc_change_24h
        ):
            filtered_by_crash += 1
            continue

        # Prefer the live Upbit 거래대금; fall back to tvscreener VALUE_TRADED
        # only when Upbit did not return a trade amount for this market.
        trade_amount_24h = ticker_trade_amount_map.get(market_code)
        if trade_amount_24h is None:
            trade_amount_24h = _to_optional_float(
                raw_item.get("acc_trade_price_24h") or raw_item.get("trade_amount_24h")
            )
        item = {
            "symbol": market_code,
            "original_market": market_code,
            "market": market,
            "name": _resolve_crypto_display_name(market_code, raw_item, display_names),
            "close": _to_optional_float(raw_item.get("trade_price")),
            "change_rate": _to_optional_float(
                raw_item.get("change_rate")
                if raw_item.get("change_rate") is not None
                else raw_item.get("signed_change_rate")
            )
            or 0.0,
            "trade_amount_24h": trade_amount_24h or 0.0,
            "volume_24h": ticker_volume_map.get(market_code, 0.0),
            "market_cap": _to_optional_float(raw_item.get("tv_market_cap")),
            "market_cap_rank": None,
            "market_warning": is_warning_market if pure_market_snapshot else None,
            "rsi": _to_optional_float(raw_item.get("rsi")),
            "volume_ratio": None,
            "candle_type": "flat",
            "adx": _to_optional_float(raw_item.get("adx")),
            "plus_di": None,
            "minus_di": None,
        }
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        candidates.append(item)

    if max_rsi is not None:
        candidates = [
            item
            for item in candidates
            if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
        ]

    # Cooldown filter + coingecko fetch.  Snapshot builds must not consult
    # account/trade-state cooldowns and should not blend in non-Upbit/TV market
    # data before persistence.
    coingecko_fetch_task = (
        None
        if pure_market_snapshot
        else asyncio.create_task(_run_crypto_coingecko_fetch())
    )
    try:
        filtered_by_cooldown = 0
        if not pure_market_snapshot:
            try:
                cooldown_service = _get_crypto_trade_cooldown_service()
                blocked_symbols = await cooldown_service.filter_symbols_in_cooldown(
                    str(item.get("symbol") or "") for item in candidates
                )
                if blocked_symbols:
                    candidates = [
                        item
                        for item in candidates
                        if str(item.get("symbol") or "").strip().upper()
                        not in blocked_symbols
                    ]
                    filtered_by_cooldown = len(blocked_symbols)
            except Exception as exc:
                warnings.append(
                    f"Stop-loss cooldown filter failed; showing all candidates ({type(exc).__name__}: {exc})"
                )

        if max_rsi is not None:
            candidates = [
                item
                for item in candidates
                if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
            ]

        metric_diagnostics = _empty_rsi_enrichment_diagnostics()
        coingecko_payload = (
            {
                "data": {},
                "cached": False,
                "age_seconds": None,
                "stale": False,
                "error": None,
            }
            if coingecko_fetch_task is None
            else await coingecko_fetch_task
        )
    finally:
        if coingecko_fetch_task is not None and not coingecko_fetch_task.done():
            coingecko_fetch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await coingecko_fetch_task

    return await finalize_crypto_screen(
        candidates=candidates,
        filters_applied=filters_applied,
        market=market,
        limit=limit,
        max_rsi=max_rsi,
        rsi_enrichment=metric_diagnostics,
        warnings=warnings,
        coingecko_payload=coingecko_payload,
        total_markets=len(raw_results),
        top_by_volume=len(raw_results),
        filtered_by_warning=filtered_by_warning,
        filtered_by_crash=filtered_by_crash,
        filtered_by_stop_loss_cooldown=filtered_by_cooldown,
        source="tvscreener",
    )


async def _screen_crypto_via_tvscreener(
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
    pure_market_snapshot: bool = False,
) -> dict[str, Any]:
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
        "max_rsi": max_rsi,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    if min_dividend_yield_input is not None:
        filters_applied["min_dividend_yield_input"] = min_dividend_yield_input
    if min_dividend_yield_normalized is not None:
        filters_applied["min_dividend_yield_normalized"] = min_dividend_yield_normalized

    # Phase 1: Build filters
    filter_config = _build_crypto_filters(
        max_rsi=max_rsi,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )

    # Phase 2: Execute query
    df = await _execute_crypto_query(
        columns=filter_config["columns"],
        where_conditions=filter_config["where_conditions"],
        sort_field=filter_config["sort_field"],
        dispatch_sort_order=filter_config["dispatch_sort_order"],
        query_limit=filter_config["query_limit"],
    )

    # Phase 3: Normalize results
    return await _normalize_crypto_results(
        df,
        market=market,
        max_rsi=max_rsi,
        min_market_cap=min_market_cap,
        filters_applied=filters_applied,
        limit=limit,
        pure_market_snapshot=pure_market_snapshot,
    )


async def _screen_crypto_with_fallback(
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
    """Screen crypto market using tvscreener (fallback removed)."""
    # Silent fallback 제거 - 에러를 명시적으로 전파
    return await _screen_crypto_via_tvscreener(
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
