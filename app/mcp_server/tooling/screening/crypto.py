"""Crypto screening — indicator enrichment, _screen_crypto, tvscreener path, fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import sentry_sdk
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.brokers.upbit.client as upbit_service
from app.core.async_rate_limiter import RateLimitExceededError
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.analysis_crypto_score import (
    calculate_crypto_metrics_from_ohlcv,
)
from app.mcp_server.tooling.analysis_screen_crypto import finalize_crypto_screen
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
    _normalize_crypto_symbol,
    compute_crypto_realtime_rsi_map,
)
from app.mcp_server.tooling.screening.common import (
    CRYPTO_TOP_BY_VOLUME,
    MarketCapCache,
    _clean_text,
    _compute_rsi_bucket,
    _empty_rsi_enrichment_diagnostics,
    _finalize_rsi_enrichment_diagnostics,
    _get_tvscreener_attr,
    _normalize_dividend_yield_threshold,
    _timeout_seconds,
    _to_optional_float,
    is_safe_drop,
)
from app.mcp_server.tooling.screening.enrichment import (
    _resolve_crypto_display_name,
    _tradingview_symbol_name,
)
from app.services.crypto_trade_cooldown_service import CryptoTradeCooldownService
from app.services.tvscreener_service import (
    TvScreenerError,
    TvScreenerRateLimitError,
    TvScreenerService,
    TvScreenerTimeoutError,
    _import_tvscreener,
)
from app.services.upbit_symbol_universe_service import (
    get_upbit_market_display_names,
    get_upbit_warning_markets,
)
from app.utils.symbol_mapping import (
    SymbolMappingError,
    tradingview_to_upbit,
    upbit_to_tradingview,
)

logger = logging.getLogger(__name__)

_CRYPTO_MARKET_CAP_CACHE = MarketCapCache(ttl=600)

# Crypto trade cooldown service singleton
_cooldown_service: CryptoTradeCooldownService | None = None


def _get_crypto_trade_cooldown_service() -> CryptoTradeCooldownService:
    """Get or create the crypto trade cooldown service."""
    global _cooldown_service
    if _cooldown_service is None:
        _cooldown_service = CryptoTradeCooldownService()
    return _cooldown_service


async def _run_crypto_indicator_enrichment(
    enrichment_items: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    filtered: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    metric_diagnostics = _empty_rsi_enrichment_diagnostics()
    metric_diagnostics["attempted"] = len(enrichment_items)
    _enrich_semaphore = asyncio.Semaphore(5)
    _enrich_succeeded = 0
    _enrich_failed = 0
    _enrich_timeout = 0
    _enrich_error_samples: list[str] = []

    async def _enrich_single_item(item: dict[str, Any]) -> None:
        nonlocal _enrich_succeeded, _enrich_failed, _enrich_timeout
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            return
        async with _enrich_semaphore:
            try:
                df = await asyncio.wait_for(
                    _fetch_ohlcv_for_indicators(symbol, "crypto", count=50),
                    timeout=_timeout_seconds("crypto_enrichment"),
                )
                metrics = calculate_crypto_metrics_from_ohlcv(df)
            except TimeoutError:
                _enrich_timeout += 1
                if len(_enrich_error_samples) < 3:
                    _enrich_error_samples.append(f"TimeoutError: {symbol}")
                return
            except Exception as exc:
                _enrich_failed += 1
                if len(_enrich_error_samples) < 3:
                    _enrich_error_samples.append(f"{type(exc).__name__}: {exc}"[:100])
                return
            _enrich_succeeded += 1
            item["volume_ratio"] = metrics.get("volume_ratio")
            item["candle_type"] = metrics.get("candle_type") or "flat"
            item["plus_di"] = metrics.get("plus_di")
            item["minus_di"] = metrics.get("minus_di")
            if item.get("adx") is None:
                item["adx"] = metrics.get("adx")
            if item.get("rsi") is None:
                item["rsi"] = metrics.get("rsi")
                item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))

    with sentry_sdk.start_span(
        op="crypto.screen.enrichment",
        name="crypto indicator enrichment",
    ) as enrich_span:
        enrich_span.set_data("candidate_count", len(candidates))
        enrich_span.set_data("filtered_count", len(filtered))
        enrich_span.set_data("limit", limit)
        enrich_span.set_data("concurrency", 5)
        await asyncio.gather(
            *[_enrich_single_item(item) for item in enrichment_items],
            return_exceptions=True,
        )

    metric_diagnostics["succeeded"] = _enrich_succeeded
    metric_diagnostics["failed"] = _enrich_failed
    metric_diagnostics["timeout"] = _enrich_timeout
    metric_diagnostics["error_samples"] = _enrich_error_samples[:3]
    return metric_diagnostics


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


async def _enrich_crypto_indicators(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich crypto candidates with RSI, ADX, and volume using CryptoScreener."""
    rsi_enrichment = _empty_rsi_enrichment_diagnostics()
    if not candidates:
        return rsi_enrichment

    rsi_enrichment["attempted"] = len(candidates)
    statuses = ["pending" for _ in candidates]
    errors: list[str | None] = [None for _ in candidates]

    # Map Upbit symbols to TradingView format and track indices
    symbols_by_index: list[str | None] = [None for _ in candidates]
    tv_symbols_by_index: list[str | None] = [None for _ in candidates]
    tv_symbols_to_upbit: dict[str, str] = {}  # TradingView -> Upbit mapping
    unique_tv_symbols: list[str] = []
    seen_tv_symbols: set[str] = set()

    for index, item in enumerate(candidates):
        # Skip if RSI already exists
        if item.get("rsi") is not None:
            statuses[index] = "success"
            continue

        # Extract and normalize Upbit symbol
        symbol = item.get("original_market") or item.get("symbol") or item.get("market")
        normalized_symbol = _normalize_crypto_symbol(str(symbol or ""))
        if not normalized_symbol:
            statuses[index] = "error"
            errors[index] = "No valid symbol found"
            continue

        symbols_by_index[index] = normalized_symbol

        # Convert to TradingView format
        try:
            tv_symbol = upbit_to_tradingview(normalized_symbol)
            tv_symbols_by_index[index] = tv_symbol

            # Track unique TradingView symbols for batch query
            if tv_symbol not in seen_tv_symbols:
                seen_tv_symbols.add(tv_symbol)
                unique_tv_symbols.append(tv_symbol)
                tv_symbols_to_upbit[tv_symbol] = normalized_symbol

        except SymbolMappingError as exc:
            statuses[index] = "error"
            errors[index] = f"Symbol mapping failed: {exc}"
            logger.warning(
                "[Indicators-Crypto] Failed to map symbol %s: %s",
                normalized_symbol,
                exc,
            )
            continue

    # Query CryptoScreener for indicators if we have symbols to query
    if not unique_tv_symbols:
        logger.info("[Indicators-Crypto] No symbols to enrich with CryptoScreener")
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)
        return rsi_enrichment

    try:
        # Use CryptoScreener to bulk query indicators from TradingView
        logger.info(
            "[Indicators-Crypto] Querying CryptoScreener for %d symbols",
            len(unique_tv_symbols),
        )

        tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))

        try:
            tvscreener = _import_tvscreener()
            CryptoField = tvscreener.CryptoField

            columns = [CryptoField.NAME, CryptoField.RELATIVE_STRENGTH_INDEX_14]

            try:
                adx_field = CryptoField.AVERAGE_DIRECTIONAL_INDEX_14
                columns.append(adx_field)
                has_adx = True
                logger.debug(
                    "[Indicators-Crypto] ADX field available for CryptoScreener"
                )
            except AttributeError:
                has_adx = False
                logger.info(
                    "[Indicators-Crypto] ADX field not available for CryptoScreener, skipping"
                )

            try:
                volume_field = CryptoField.VOLUME_24H_IN_USD
                columns.append(volume_field)
                has_volume = True
            except AttributeError:
                has_volume = False
                logger.warning(
                    "[Indicators-Crypto] VOLUME field not available for CryptoScreener"
                )

            requested_tv_names = [
                _tradingview_symbol_name(symbol)
                for symbol in unique_tv_symbols
                if _tradingview_symbol_name(symbol)
            ]
            where_conditions = []
            try:
                where_conditions.append(CryptoField.EXCHANGE == "UPBIT")
            except AttributeError:
                logger.warning(
                    "[Indicators-Crypto] EXCHANGE field not available for CryptoScreener"
                )

            if requested_tv_names:
                try:
                    where_conditions.append(CryptoField.NAME.isin(requested_tv_names))
                except AttributeError:
                    logger.warning(
                        "[Indicators-Crypto] NAME.isin not available for CryptoScreener"
                    )

            df = await tvscreener_service.query_crypto_screener(
                columns=columns,
                where_clause=where_conditions,
                limit=300,
            )

            rsi_map: dict[str, float | None] = {}
            adx_map: dict[str, float | None] = {}
            volume_map: dict[str, float | None] = {}

            if not df.empty:
                for _, row in df.iterrows():
                    tradingview_symbol = str(row.get("symbol", "")).strip().upper()
                    if not tradingview_symbol:
                        continue
                    try:
                        upbit_symbol = tradingview_to_upbit(tradingview_symbol)
                    except SymbolMappingError:
                        continue
                    if upbit_symbol not in tv_symbols_to_upbit.values():
                        continue

                    rsi_value = _to_optional_float(
                        row.get("relative_strength_index_14")
                    )
                    rsi_map[upbit_symbol] = rsi_value

                    if has_adx:
                        adx_value = _to_optional_float(
                            row.get("average_directional_index_14")
                        )
                        adx_map[upbit_symbol] = adx_value

                    if has_volume:
                        volume_value = _to_optional_float(row.get("volume_24h_in_usd"))
                        volume_map[upbit_symbol] = volume_value

            logger.info(
                "[Indicators-Crypto] CryptoScreener returned data for %d/%d symbols "
                "(RSI: %d, ADX: %d, Volume: %d)",
                len(rsi_map),
                len(unique_tv_symbols),
                len(rsi_map),
                len(adx_map) if has_adx else 0,
                len(volume_map) if has_volume else 0,
            )

        except ImportError:
            logger.warning(
                "[Indicators-Crypto] tvscreener not installed, falling back to manual calculation for RSI"
            )
            # Fallback to manual calculation if tvscreener is not available
            batch_symbols: list[str] = [
                symbol for symbol in symbols_by_index if symbol is not None
            ]
            rsi_map_manual = await asyncio.wait_for(
                compute_crypto_realtime_rsi_map(batch_symbols),
                timeout=_timeout_seconds("crypto_enrichment"),
            )
            # Convert manual RSI map (Upbit symbols) to match our data structure
            rsi_map = {}
            adx_map = {}
            volume_map = {}
            for _, upbit_symbol in tv_symbols_to_upbit.items():
                if upbit_symbol in rsi_map_manual:
                    rsi_map[upbit_symbol] = rsi_map_manual[upbit_symbol]

        # Apply indicator values to candidates
        for index, item in enumerate(candidates):
            if statuses[index] != "pending":
                continue

            upbit_symbol = symbols_by_index[index]
            if upbit_symbol is None:
                statuses[index] = "error"
                errors[index] = "No valid Upbit symbol"
                continue

            rsi_value = rsi_map.get(upbit_symbol)
            item["rsi"] = rsi_value
            item["rsi_bucket"] = _compute_rsi_bucket(rsi_value)

            if upbit_symbol in adx_map:
                item["adx"] = adx_map[upbit_symbol]

            if upbit_symbol in volume_map:
                item["volume_24h"] = volume_map[upbit_symbol]

            if rsi_value is None:
                statuses[index] = "error"
                errors[index] = "RSI not found in CryptoScreener results"
            else:
                statuses[index] = "success"

    except TvScreenerTimeoutError as exc:
        logger.warning(
            "[Indicators-Crypto] Indicator enrichment timed out: %s",
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "timeout"
                errors[index] = f"CryptoScreener timeout: {exc}"
    except TvScreenerRateLimitError as exc:
        logger.error(
            "[Indicators-Crypto] CryptoScreener rate limited: %s",
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "rate_limited"
                errors[index] = f"CryptoScreener error: {exc}"
    except TvScreenerError as exc:
        logger.error(
            "[Indicators-Crypto] CryptoScreener query failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = (
                    "rate_limited" if "rate limit" in str(exc).lower() else "error"
                )
                errors[index] = f"CryptoScreener error: {exc}"
    except TimeoutError:
        logger.warning(
            "[Indicators-Crypto] Indicator enrichment timed out after %.2f seconds",
            _timeout_seconds("crypto_enrichment"),
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = "timeout"
                errors[index] = (
                    f"Timed out after {_timeout_seconds('crypto_enrichment'):.2f} seconds"
                )
    except Exception as exc:
        logger.error(
            "[Indicators-Crypto] Indicator enrichment batch failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        for index, status in enumerate(statuses):
            if status == "pending":
                statuses[index] = (
                    "rate_limited"
                    if isinstance(exc, RateLimitExceededError)
                    else "error"
                )
                errors[index] = f"{type(exc).__name__}: {exc}"
    finally:
        _finalize_rsi_enrichment_diagnostics(rsi_enrichment, statuses, errors)

    return rsi_enrichment


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
    enrich_rsi: bool = True,
) -> dict[str, Any]:
    (
        min_dividend_yield_input,
        min_dividend_yield_normalized,
    ) = _normalize_dividend_yield_threshold(min_dividend_yield)

    warnings: list[str] = []
    filters_applied: dict[str, Any] = {
        "market": market,
        "asset_type": asset_type,
        "category": category,
    }

    all_candidates = await upbit_service.fetch_top_traded_coins(fiat="KRW")
    total_markets = len(all_candidates)
    top_candidates = all_candidates[:CRYPTO_TOP_BY_VOLUME]
    top_by_volume = len(top_candidates)

    btc_change_24h = 0.0
    btc_item = next(
        (
            item
            for item in all_candidates
            if str(item.get("market") or "").upper() == "KRW-BTC"
        ),
        None,
    )
    if btc_item is None:
        warnings.append(
            "KRW-BTC ticker not found; crash filter uses btc_change_24h=0.0 fallback."
        )
    else:
        btc_change_24h = _to_optional_float(
            btc_item.get("signed_change_rate") or btc_item.get("change_rate")
        )
        if btc_change_24h is None:
            btc_change_24h = 0.0
            warnings.append(
                "KRW-BTC change rate is missing; crash filter uses btc_change_24h=0.0 fallback."
            )

    warning_markets: set[str] = set()
    try:
        warning_markets = await get_upbit_warning_markets(quote_currency="KRW")
    except Exception as exc:
        warnings.append(
            "market warning details unavailable; warning filter skipped "
            f"({type(exc).__name__}: {exc})"
        )

    filtered_by_warning = 0
    filtered_by_crash = 0
    candidates: list[dict[str, Any]] = []

    for raw_item in top_candidates:
        market_code = str(raw_item.get("market") or "").strip().upper()
        if market_code in warning_markets:
            filtered_by_warning += 1
            continue

        coin_change_24h = raw_item.get("signed_change_rate")
        if coin_change_24h is None:
            coin_change_24h = raw_item.get("change_rate")
        if not is_safe_drop(coin_change_24h, btc_change_24h):
            filtered_by_crash += 1
            continue

        volume_24h = _to_optional_float(
            raw_item.get("acc_trade_volume_24h") or raw_item.get("volume")
        )
        trade_amount_24h = _to_optional_float(
            raw_item.get("trade_amount_24h") or raw_item.get("acc_trade_price_24h")
        )

        item = dict(raw_item)
        item["original_market"] = raw_item.get("market")
        item["market"] = "crypto"
        if market_code:
            item["symbol"] = market_code
        item["name"] = (
            raw_item.get("name")
            or raw_item.get("korean_name")
            or raw_item.get("english_name")
        )
        item["change_rate"] = (
            _to_optional_float(
                raw_item.get("change_rate")
                if raw_item.get("change_rate") is not None
                else raw_item.get("signed_change_rate")
            )
            or 0.0
        )
        item["trade_amount_24h"] = trade_amount_24h or 0.0
        item.pop("volume", None)
        item["market_cap"] = None
        item["market_cap_rank"] = None
        item["market_warning"] = None
        item["rsi"] = _to_optional_float(raw_item.get("rsi"))
        item["volume_24h"] = volume_24h or 0.0
        item["volume_ratio"] = _to_optional_float(raw_item.get("volume_ratio"))
        item["candle_type"] = raw_item.get("candle_type") or "flat"
        item["adx"] = _to_optional_float(raw_item.get("adx"))
        item["plus_di"] = _to_optional_float(raw_item.get("plus_di"))
        item["minus_di"] = _to_optional_float(raw_item.get("minus_di"))
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        item.pop("score", None)
        candidates.append(item)

    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    async def _run_rsi_enrichment() -> dict[str, Any]:
        if not enrich_rsi or not candidates:
            return _empty_rsi_enrichment_diagnostics()
        try:
            return await _enrich_crypto_indicators(candidates)
        except Exception as exc:
            warnings.append(
                f"Crypto RSI enrichment failed: {type(exc).__name__}: {exc}; partial results returned"
            )
            return _empty_rsi_enrichment_diagnostics()

    try:
        parallel_results = await asyncio.gather(
            _run_rsi_enrichment(),
            _CRYPTO_MARKET_CAP_CACHE.get(),
        )
        if len(parallel_results) == 2:
            rsi_enrichment = parallel_results[0]
            coingecko_payload = parallel_results[1]
        else:
            warnings.append(
                "Crypto enrichment parallel execution returned unexpected shape; "
                "partial results returned"
            )
            rsi_enrichment = _empty_rsi_enrichment_diagnostics()
            coingecko_payload = {
                "data": {},
                "cached": False,
                "age_seconds": None,
                "stale": False,
                "error": "parallel_result_shape_error",
            }
    except Exception as exc:
        warnings.append(
            "Crypto enrichment parallel execution failed; partial results returned "
            f"({type(exc).__name__}: {exc})"
        )
        rsi_enrichment = _empty_rsi_enrichment_diagnostics()
        coingecko_payload = {
            "data": {},
            "cached": False,
            "age_seconds": None,
            "stale": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

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

    # Filter out symbols in stop-loss cooldown
    filtered_by_cooldown = 0
    try:
        cooldown_service = _get_crypto_trade_cooldown_service()
        candidates_not_cooldown: list[dict[str, Any]] = []
        for item in candidates:
            symbol = str(item.get("symbol") or "").strip().upper()
            if await cooldown_service.is_in_cooldown(symbol):
                filtered_by_cooldown += 1
                continue
            candidates_not_cooldown.append(item)
        candidates = candidates_not_cooldown
    except Exception as exc:
        warnings.append(
            f"Stop-loss cooldown filter failed; showing all candidates ({type(exc).__name__}: {exc})"
        )

    return await finalize_crypto_screen(
        candidates=candidates,
        filters_applied=filters_applied,
        market=market,
        limit=limit,
        max_rsi=max_rsi,
        rsi_enrichment=rsi_enrichment,
        warnings=warnings,
        coingecko_payload=coingecko_payload,
        total_markets=total_markets,
        top_by_volume=top_by_volume,
        filtered_by_warning=filtered_by_warning,
        filtered_by_crash=filtered_by_crash,
        filtered_by_stop_loss_cooldown=filtered_by_cooldown,
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

    tvscreener_service = TvScreenerService(timeout=_timeout_seconds("tvscreener"))
    df = await tvscreener_service.query_crypto_screener(
        columns=columns,
        where_clause=where_conditions,
        sort_by=sort_field,
        ascending=(dispatch_sort_order == "asc"),
        limit=query_limit,
    )

    warnings: list[str] = []
    if min_market_cap is not None:
        warnings.append(
            "min_market_cap filter is not supported for crypto market; ignored"
        )

    raw_results: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        tradingview_symbol = _clean_text(row.get("symbol")).upper()
        if not tradingview_symbol:
            continue
        try:
            upbit_symbol = tradingview_to_upbit(tradingview_symbol)
        except SymbolMappingError:
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

    ticker_volume_map: dict[str, float] = {}
    if market_codes:
        try:
            ticker_rows = await upbit_service.fetch_multiple_tickers(market_codes)
            ticker_volume_map = {
                str(row.get("market") or "").strip().upper(): (
                    _to_optional_float(row.get("acc_trade_volume_24h")) or 0.0
                )
                for row in ticker_rows
                if str(row.get("market") or "").strip()
            }
        except Exception as exc:
            warnings.append(
                "Upbit 24h volume enrichment failed; volume_24h defaulted to 0.0 "
                f"({type(exc).__name__}: {exc})"
            )

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
        warnings.append(
            "KRW-BTC ticker not found; crash filter uses btc_change_24h=0.0 fallback."
        )
    else:
        btc_change_24h = _to_optional_float(
            btc_item.get("signed_change_rate") or btc_item.get("change_rate")
        )
        if btc_change_24h is None:
            btc_change_24h = 0.0
            warnings.append(
                "KRW-BTC change rate is missing; crash filter uses btc_change_24h=0.0 fallback."
            )

    filtered_by_warning = 0
    filtered_by_crash = 0
    candidates: list[dict[str, Any]] = []
    for raw_item in raw_results:
        market_code = str(raw_item.get("symbol") or "").strip().upper()
        if market_code in warning_markets:
            filtered_by_warning += 1
            continue

        coin_change_24h = raw_item.get("signed_change_rate")
        if coin_change_24h is None:
            coin_change_24h = raw_item.get("change_rate")
        if not is_safe_drop(coin_change_24h, btc_change_24h):
            filtered_by_crash += 1
            continue

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
            "market_warning": None,
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
        filtered = [
            item
            for item in candidates
            if item.get("rsi") is not None and float(item["rsi"]) <= max_rsi
        ]
    else:
        filtered = candidates

    enrichment_items = filtered[:limit]
    async with asyncio.TaskGroup() as tg:
        enrichment_task = tg.create_task(
            _run_crypto_indicator_enrichment(
                enrichment_items,
                candidates,
                filtered,
                limit,
            )
        )
        coingecko_fetch_task = tg.create_task(_run_crypto_coingecko_fetch())
    metric_diagnostics = enrichment_task.result()
    coingecko_payload = coingecko_fetch_task.result()

    # Filter out symbols in stop-loss cooldown
    filtered_by_cooldown = 0
    try:
        cooldown_service = _get_crypto_trade_cooldown_service()
        candidates_not_cooldown: list[dict[str, Any]] = []
        for item in candidates:
            symbol = str(item.get("symbol") or "").strip().upper()
            if await cooldown_service.is_in_cooldown(symbol):
                filtered_by_cooldown += 1
                continue
            candidates_not_cooldown.append(item)
        candidates = candidates_not_cooldown
    except Exception as exc:
        warnings.append(
            f"Stop-loss cooldown filter failed; showing all candidates ({type(exc).__name__}: {exc})"
        )

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
    """Screen crypto market with tvscreener fallback to legacy."""
    try:
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
    except Exception as exc:
        logger.debug(
            "tvscreener crypto screening failed, falling back to legacy: %s",
            exc,
        )
        # Fallback to legacy implementation
        return await _screen_crypto(
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
