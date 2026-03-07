"""Recommend-stocks helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from app.mcp_server.scoring import calc_composite_score, generate_reason
from app.mcp_server.strategies import (
    StrategyType,
    get_strategy_description,
    get_strategy_scoring_weights,
    get_strategy_screen_params,
    validate_strategy,
)
from app.mcp_server.tooling.shared import (
    MCP_USER_ID as _MCP_USER_ID,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    logger,
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

CRYPTO_PREFILTER_LIMIT = 30


def _build_crypto_rsi_reason(item: dict[str, Any]) -> str:
    rsi = item.get("rsi")
    candle_type = item.get("candle_type", "")
    volume_ratio = item.get("volume_ratio")
    rsi_bucket = item.get("rsi_bucket")

    parts: list[str] = []

    if rsi is not None:
        if rsi < 30:
            rsi_label = "과매도"
        elif rsi < 40:
            rsi_label = "저평가"
        elif rsi > 70:
            rsi_label = "과매수"
        elif rsi > 60:
            rsi_label = "고평가"
        else:
            rsi_label = "중립"
        parts.append(f"RSI {rsi:.1f}({rsi_label})")

    if rsi_bucket is not None:
        parts.append(f"RSI bucket {rsi_bucket}")

    if candle_type and candle_type != "flat":
        parts.append(f"캔들 {candle_type}")

    if volume_ratio is not None and volume_ratio > 1.0:
        parts.append(f"거래량 {volume_ratio:.1f}배")

    if not parts:
        return "RSI 기반 정렬"
    return " | ".join(parts)


async def _enrich_crypto_composite_metrics(
    candidates: list[dict[str, Any]],
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    from app.core.async_rate_limiter import RateLimitExceededError
    from app.mcp_server.tooling.analysis_crypto_score import (
        calculate_crypto_metrics_from_ohlcv,
    )
    from app.mcp_server.tooling.market_data_indicators import (
        _fetch_ohlcv_for_indicators,
    )

    if not candidates:
        return candidates

    semaphore = asyncio.Semaphore(5)
    results = list(candidates)
    enrich_warnings: list[str] = []
    failed_count = 0
    rate_limited_count = 0

    async def fetch_metrics(item: dict[str, Any], index: int) -> None:
        nonlocal failed_count, rate_limited_count
        async with semaphore:
            symbol = (
                item.get("symbol") or item.get("original_market") or item.get("market")
            )
            if not symbol:
                failed_count += 1
                return
            try:
                df = await _fetch_ohlcv_for_indicators(symbol, "crypto", count=50)
                if df is not None and not df.empty:
                    metrics = calculate_crypto_metrics_from_ohlcv(df)
                    results[index]["rsi"] = metrics.get("rsi")
                    results[index]["score"] = metrics.get("score")
                    results[index]["volume_24h"] = metrics.get("volume_24h")
                    results[index]["volume_ratio"] = metrics.get("volume_ratio")
                    results[index]["candle_type"] = metrics.get("candle_type")
                    results[index]["adx"] = metrics.get("adx")
                    results[index]["plus_di"] = metrics.get("plus_di")
                    results[index]["minus_di"] = metrics.get("minus_di")
                else:
                    failed_count += 1
            except RateLimitExceededError as exc:
                rate_limited_count += 1
                logger.debug(
                    "Crypto metrics rate-limited symbol=%s error=%s", symbol, exc
                )
            except Exception as exc:
                failed_count += 1
                logger.debug(
                    "Crypto metrics fetch failed symbol=%s error=%s", symbol, exc
                )

    try:
        await asyncio.wait_for(
            asyncio.gather(
                *[fetch_metrics(item, i) for i, item in enumerate(candidates)],
                return_exceptions=True,
            ),
            timeout=30.0,
        )
    except TimeoutError:
        enrich_warnings.append(
            "Crypto metrics enrichment timed out after 30 seconds; partial results returned"
        )
        logger.warning("Crypto composite metrics enrichment timed out")
    except Exception as exc:
        enrich_warnings.append(f"Crypto metrics enrichment failed: {exc}")
        logger.warning("Crypto composite metrics enrichment failed: %s", exc)

    if rate_limited_count > 0:
        enrich_warnings.append(
            f"Crypto metrics enrichment hit rate limits for {rate_limited_count} symbols; partial results returned"
        )
    if failed_count > 0:
        enrich_warnings.append(
            f"Crypto metrics enrichment failed for {failed_count} symbols; partial results returned"
        )

    if warnings is not None:
        warnings.extend(enrich_warnings)

    return results


def _allocate_budget_equal(
    candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
) -> tuple[list[dict[str, Any]], float]:
    if not candidates or budget <= 0:
        return [], round(budget, 2)

    deduped_candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for item in candidates:
        symbol = str(item.get("symbol", "")).strip().upper()
        price = _to_float(item.get("price"), default=0.0)
        if not symbol or symbol in seen_symbols:
            continue
        if price <= 0:
            continue
        seen_symbols.add(symbol)
        deduped_candidates.append(item)

    top_candidates = deduped_candidates[:max_positions]
    if not top_candidates:
        return [], round(budget, 2)

    equal_ratio = 1.0 / len(top_candidates)
    target_per_position = budget * equal_ratio

    allocated: list[dict[str, Any]] = []
    remaining = float(budget)

    for item in top_candidates:
        price = item.get("price", 0)
        target_amount = target_per_position
        quantity = int(target_amount / price) if price > 0 else 0
        if quantity <= 0:
            continue

        amount = price * quantity
        if amount > remaining:
            quantity = int(remaining / price)
            if quantity <= 0:
                continue
            amount = price * quantity

        remaining -= amount
        allocated.append(
            {
                **item,
                "quantity": quantity,
                "amount": round(amount, 2),
            }
        )

    if allocated and remaining > 0:
        sorted_allocated = sorted(
            allocated,
            key=lambda x: _to_float(x.get("rsi") or 999, default=999),
        )
        for rec in sorted_allocated:
            price = _to_float(rec.get("price"), default=0.0)
            if price <= 0 or remaining < price:
                continue
            extra_qty = int(remaining / price)
            if extra_qty <= 0:
                continue
            extra_amount = price * extra_qty
            rec["quantity"] += extra_qty
            rec["amount"] = round(rec.get("amount", 0) + extra_amount, 2)
            remaining -= extra_amount
            break

    return allocated, round(remaining, 2)


def _normalize_recommend_market(market: str | None) -> str:
    if market is None:
        return "kr"
    m = market.lower().strip()
    if not m:
        return "kr"

    aliases = {
        "kr": "kr",
        "kis": "kr",
        "krx": "kr",
        "korea": "kr",
        "kospi": "kr",
        "kosdaq": "kr",
        "us": "us",
        "usa": "us",
        "nyse": "us",
        "nasdaq": "us",
        "yahoo": "us",
        "crypto": "crypto",
        "upbit": "crypto",
        "krw": "crypto",
    }

    normalized = aliases.get(m)
    if normalized is None:
        raise ValueError("market must be one of: kr, us, crypto")
    return normalized


def _build_recommend_reason(item: dict[str, Any], strategy: str, score: float) -> str:
    return generate_reason(item, strategy, score)


def _normalize_candidate(item: dict[str, Any], market: str) -> dict[str, Any]:
    symbol = (
        item.get("symbol")
        or item.get("code")
        or item.get("original_market")
        or item.get("market", "")
    )
    is_crypto_market = market == "crypto"
    market_cap = (
        _to_optional_float(item.get("market_cap"))
        if is_crypto_market
        else _to_float(item.get("market_cap") or 0)
    )
    volume_24h = _to_optional_float(
        item.get("volume_24h") or item.get("acc_trade_volume_24h") or item.get("volume")
    )
    parsed_rsi_bucket = _to_int(item.get("rsi_bucket"))
    rsi_bucket = parsed_rsi_bucket if parsed_rsi_bucket is not None else 999

    return {
        "symbol": symbol,
        "name": item.get("name")
        or item.get("shortName")
        or item.get("shortname")
        or "",
        "price": _to_float(
            item.get("close") or item.get("price") or item.get("trade_price")
        ),
        "change_rate": _to_float(item.get("change_rate") or 0),
        "volume": _to_int(item.get("volume") or 0),
        "market_cap": market_cap,
        "trade_amount_24h": _to_float(
            item.get("trade_amount_24h") or item.get("acc_trade_price_24h") or 0
        ),
        "volume_24h": volume_24h,
        "volume_ratio": _to_optional_float(item.get("volume_ratio")),
        "candle_type": item.get("candle_type"),
        "adx": _to_optional_float(item.get("adx")),
        "plus_di": _to_optional_float(item.get("plus_di")),
        "minus_di": _to_optional_float(item.get("minus_di")),
        "per": _to_optional_float(item.get("per")),
        "pbr": _to_optional_float(item.get("pbr")),
        "dividend_yield": _to_optional_float(item.get("dividend_yield")),
        "rsi": _to_optional_float(item.get("rsi")),
        "rsi_bucket": rsi_bucket,
        "market_warning": item.get("market_warning"),
        "market_cap_rank": _to_int(item.get("market_cap_rank")),
        "market": market,
    }


def _allocate_budget(
    candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
) -> tuple[list[dict[str, Any]], float]:
    if not candidates or budget <= 0:
        return [], round(budget, 2)

    deduped_candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for item in candidates:
        symbol = str(item.get("symbol", "")).strip().upper()
        price = _to_float(item.get("price"), default=0.0)
        if not symbol or symbol in seen_symbols:
            continue
        if price <= 0:
            continue
        seen_symbols.add(symbol)
        deduped_candidates.append(item)

    top_candidates = deduped_candidates[:max_positions]
    if not top_candidates:
        return [], round(budget, 2)

    total_score = sum(
        max(_to_float(item.get("score"), default=0.0), 0.0) for item in top_candidates
    )
    equal_ratio = 1.0 / len(top_candidates)

    allocated: list[dict[str, Any]] = []
    remaining = float(budget)

    for item in top_candidates:
        price = item.get("price", 0)
        score = max(_to_float(item.get("score"), default=0.0), 0.0)
        alloc_ratio = (score / total_score) if total_score > 0 else equal_ratio
        target_amount = budget * alloc_ratio
        quantity = int(target_amount / price) if price > 0 else 0
        if quantity <= 0:
            continue

        amount = price * quantity
        if amount > remaining:
            quantity = int(remaining / price)
            if quantity <= 0:
                continue
            amount = price * quantity

        remaining -= amount
        allocated.append(
            {
                **item,
                "quantity": quantity,
                "amount": round(amount, 2),
            }
        )

    if allocated and remaining > 0:
        sorted_allocated = sorted(
            allocated,
            key=lambda item: _to_float(item.get("score"), default=0.0),
            reverse=True,
        )
        for rec in sorted_allocated:
            price = _to_float(rec.get("price"), default=0.0)
            if price <= 0 or remaining < price:
                continue
            extra_qty = int(remaining / price)
            if extra_qty <= 0:
                continue
            extra_amount = price * extra_qty
            rec["quantity"] += extra_qty
            rec["amount"] = round(rec.get("amount", 0) + extra_amount, 2)
            remaining -= extra_amount
            break

    return allocated, round(remaining, 2)


# =============================================================================
# Stage Functions for Recommendation Pipeline
# =============================================================================


async def _stage_screen_candidates(
    *,
    market: str,
    strategy: str,
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]],
    screen_us_fn: Callable[..., Awaitable[dict[str, Any]]],
    screen_crypto_fn: Callable[..., Awaitable[dict[str, Any]]],
    top_stocks_fallback: Callable[..., Awaitable[dict[str, Any]]],
    top_stocks_override: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    candidate_limit: int = 100,
    sectors: list[str] | None = None,
    strategy_screen_params: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 1: Screen candidates from the market.

    Returns:
        Tuple of (raw_candidates, diagnostics) where diagnostics contains
        raw_candidates count and any screening warnings.
    """
    normalized_market = _normalize_recommend_market(market)
    validated_strategy = validate_strategy(strategy)

    if strategy_screen_params is None:
        strategy_screen_params = get_strategy_screen_params(validated_strategy)

    sort_by = strategy_screen_params.get("sort_by", "volume")
    sort_order = strategy_screen_params.get("sort_order", "desc")
    min_market_cap = strategy_screen_params.get("min_market_cap")
    max_per = strategy_screen_params.get("max_per")
    max_pbr = strategy_screen_params.get("max_pbr")
    min_dividend_yield = strategy_screen_params.get("min_dividend_yield")
    max_rsi = strategy_screen_params.get("max_rsi")

    diagnostics: dict[str, Any] = {
        "raw_candidates": 0,
        "market": normalized_market,
    }

    screen_asset_type = None
    screen_category = sectors[0] if sectors else None

    if normalized_market == "crypto":
        if screen_category is not None:
            if warnings is not None:
                warnings.append(
                    "crypto market does not support sectors/category filter; ignored."
                )
            screen_category = None
        if validated_strategy != "oversold":
            if warnings is not None:
                warnings.append(
                    f"crypto market에서 strategy='{validated_strategy}'는 무시됩니다. "
                    "RSI ascending 정렬 고정."
                )
        sort_by = "rsi"
        sort_order = "asc"
        min_market_cap = None
        max_per = None
        max_pbr = None
        min_dividend_yield = None
        max_rsi = None

    if normalized_market == "us" and max_pbr is not None:
        if warnings is not None:
            warnings.append(
                "us market screener does not support max_pbr filter; ignored."
            )
        max_pbr = None

    logger.info(
        "_stage_screen_candidates market=%s strategy=%s limit=%d sort_by=%s",
        normalized_market,
        validated_strategy,
        candidate_limit,
        sort_by,
    )

    raw_candidates: list[dict[str, Any]] = []

    if normalized_market == "kr":
        screen_result = await screen_kr_fn(
            market="kr",
            asset_type=screen_asset_type,
            category=screen_category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=candidate_limit,
            enrich_rsi=False,
        )
        screen_error = screen_result.get("error")
        if screen_error is not None:
            error_msg = str(screen_error).strip() or "unknown error"
            logger.warning(
                "_stage_screen_candidates KR screening failed: %s", error_msg
            )
            diagnostics["screen_error"] = error_msg
            return [], diagnostics
        raw_candidates = screen_result.get("results", [])

    elif normalized_market == "us":
        us_limit = min(candidate_limit, 50)

        fallback_sort_by = sort_by
        fallback_sort_order = sort_order
        fallback_ranking_type = "volume"

        if fallback_sort_by == "market_cap":
            fallback_ranking_type = "market_cap"
        elif fallback_sort_by == "change_rate":
            fallback_ranking_type = (
                "losers" if fallback_sort_order == "asc" else "gainers"
            )
        elif fallback_sort_by == "dividend_yield":
            if warnings is not None:
                warnings.append(
                    "US top-stocks fallback does not support dividend_yield ranking; volume ranking used instead."
                )

        top_stocks_fn = top_stocks_override or top_stocks_fallback

        try:
            screen_result = await screen_us_fn(
                market="us",
                asset_type=None,
                category=screen_category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                min_dividend_yield=min_dividend_yield,
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=us_limit,
                enrich_rsi=False,
            )

            if screen_result.get("error"):
                error_msg = str(screen_result.get("error")).strip() or "unknown error"
                logger.warning(
                    "_stage_screen_candidates US screening failed: %s", error_msg
                )
                if warnings is not None:
                    warnings.append(
                        f"US screening unavailable: {error_msg}; top-stocks fallback used."
                    )
            else:
                screen_warnings = screen_result.get("warnings")
                if isinstance(screen_warnings, list) and warnings is not None:
                    warnings.extend(str(w) for w in screen_warnings if w)
                raw_candidates = screen_result.get("results", [])
        except Exception as exc:
            error_msg = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "_stage_screen_candidates US screening exception: %s",
                error_msg,
                exc_info=True,
            )
            if warnings is not None:
                warnings.append(
                    f"US screening unavailable: {error_msg}; top-stocks fallback used."
                )

        if not raw_candidates:
            try:
                top_result = await top_stocks_fn(
                    market="us",
                    ranking_type=fallback_ranking_type,
                    limit=us_limit,
                )
                if top_result.get("error"):
                    error_msg = str(top_result.get("error")).strip() or "unknown error"
                    logger.warning(
                        "_stage_screen_candidates US get_top_stocks failed: %s",
                        error_msg,
                    )
                    if warnings is not None:
                        warnings.append(f"US 후보 수집 실패: {error_msg}")
                    diagnostics["screen_error"] = error_msg
                else:
                    raw_candidates = top_result.get("rankings", [])
            except Exception as exc:
                error_msg = str(exc).strip() or exc.__class__.__name__
                logger.warning(
                    "_stage_screen_candidates US get_top_stocks exception: %s",
                    error_msg,
                    exc_info=True,
                )
                if warnings is not None:
                    warnings.append(f"US 후보 수집 실패: {error_msg}")
                diagnostics["screen_error"] = error_msg

    else:  # crypto
        screen_result = await screen_crypto_fn(
            market="crypto",
            asset_type=screen_asset_type,
            category=screen_category,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="asc",
            limit=CRYPTO_PREFILTER_LIMIT,
            enrich_rsi=True,
        )
        screen_error = screen_result.get("error")
        if screen_error is not None:
            error_msg = str(screen_error).strip() or "unknown error"
            logger.warning(
                "_stage_screen_candidates crypto screening failed: %s", error_msg
            )
            diagnostics["screen_error"] = error_msg
            return [], diagnostics

        screen_warnings = screen_result.get("warnings")
        if isinstance(screen_warnings, list) and warnings is not None:
            warnings.extend(str(w) for w in screen_warnings if w)

        raw_candidates = screen_result.get("results", [])

    diagnostics["raw_candidates"] = len(raw_candidates)
    logger.info(
        "_stage_screen_candidates done market=%s raw_candidates=%d",
        normalized_market,
        len(raw_candidates),
    )
    return raw_candidates, diagnostics


async def _stage_score_candidates(
    candidates: list[dict[str, Any]],
    *,
    strategy: str,
    strategy_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Stage 2: Score candidates using strategy weights.

    Args:
        candidates: List of candidate dictionaries
        strategy: Strategy name for weight lookup
        strategy_weights: Optional pre-computed weights (defaults to strategy weights)

    Returns:
        List of candidates with 'score' field added, sorted by score descending.
    """
    if not candidates:
        return []

    validated_strategy = validate_strategy(strategy)

    if strategy_weights is None:
        strategy_weights = get_strategy_scoring_weights(validated_strategy)

    logger.debug(
        "_stage_score_candidates start strategy=%s weights=%s count=%d",
        validated_strategy,
        strategy_weights,
        len(candidates),
    )

    scored_candidates: list[dict[str, Any]] = []
    for item in candidates:
        score = calc_composite_score(
            item,
            rsi_weight=strategy_weights.get("rsi_weight", 0.20),
            valuation_weight=strategy_weights.get("valuation_weight", 0.25),
            momentum_weight=strategy_weights.get("momentum_weight", 0.25),
            volume_weight=strategy_weights.get("volume_weight", 0.15),
            dividend_weight=strategy_weights.get("dividend_weight", 0.15),
        )
        fallback_penalty = item.pop("_fallback_penalty", 0)
        final_score = max(0.0, score + fallback_penalty)
        scored_candidates.append({**item, "score": round(final_score, 2)})

    scored_candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

    logger.info(
        "_stage_score_candidates done strategy=%s scored=%d",
        validated_strategy,
        len(scored_candidates),
    )
    return scored_candidates


async def _stage_filter_by_strategy(
    candidates: list[dict[str, Any]],
    *,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None = None,
    exclude_held: bool = True,
    max_positions: int,
    strategy_screen_params: dict[str, Any] | None = None,
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    sectors: list[str] | None = None,
    warnings: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Stage 3: Filter candidates by strategy-specific criteria.

    This stage:
    - Normalizes candidates
    - Excludes specified symbols
    - Optionally excludes held positions
    - Deduplicates candidates
    - Applies fallback for value/dividend strategies if needed
    - Enriches RSI for missing values

    Args:
        candidates: Raw candidates from screening
        market: Market type (kr, us, crypto)
        strategy: Strategy name
        exclude_symbols: Symbols to exclude
        exclude_held: Whether to exclude held positions
        max_positions: Maximum positions for fallback logic
        strategy_screen_params: Strategy parameters
        screen_kr_fn: KR screening function for fallback
        sectors: Sector filters
        warnings: List to append warnings
        diagnostics: Dict to update with diagnostics

    Returns:
        Tuple of (filtered_candidates, updated_diagnostics)
    """
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
        _get_indicators_impl,
    )

    normalized_market = _normalize_recommend_market(market)
    validated_strategy = validate_strategy(strategy)

    if diagnostics is None:
        diagnostics = {}
    if warnings is None:
        warnings = []

    if strategy_screen_params is None:
        strategy_screen_params = get_strategy_screen_params(validated_strategy)

    min_market_cap = strategy_screen_params.get("min_market_cap")
    max_per = strategy_screen_params.get("max_per")
    max_pbr = strategy_screen_params.get("max_pbr")
    min_dividend_yield = strategy_screen_params.get("min_dividend_yield")

    # Normalize candidates
    normalized_candidates = [
        _normalize_candidate(c, normalized_market) for c in candidates
    ]
    if not normalized_candidates:
        warnings.append("스크리닝 결과가 없어 추천 가능한 종목이 없습니다.")

    # Build exclude set
    exclude_set: set[str] = set()
    if exclude_symbols:
        manual_excludes = [
            str(symbol).strip().upper()
            for symbol in exclude_symbols
            if symbol is not None and str(symbol).strip()
        ]
        exclude_set.update(manual_excludes)
        logger.debug(
            "_stage_filter_by_strategy manual exclusions=%d", len(manual_excludes)
        )

    # Exclude held positions
    if exclude_held:
        logger.debug("_stage_filter_by_strategy holdings exclusion lookup start")
        try:
            (
                holdings_positions,
                holdings_errors,
                _,
                _,
            ) = await _collect_portfolio_positions(
                account=None,
                market=normalized_market,
                include_current_price=False,
                user_id=_MCP_USER_ID,
            )
            holdings_exclusions = 0
            for pos in holdings_positions:
                symbol = pos.get("symbol", "")
                if symbol:
                    holdings_exclusions += 1
                    exclude_set.add(str(symbol).upper())
            if holdings_errors:
                warnings.append(
                    f"보유 종목 조회 중 일부 오류: {len(holdings_errors)}건"
                )
            logger.debug(
                "_stage_filter_by_strategy holdings exclusions=%d total_exclusions=%d",
                holdings_exclusions,
                len(exclude_set),
            )
        except Exception as exc:
            holdings_error = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "_stage_filter_by_strategy holdings lookup failed: %s",
                holdings_error,
                exc_info=True,
            )
            warnings.append(f"보유 종목 조회 실패: {holdings_error}")
    else:
        warnings.append("exclude_held=False: 보유 종목도 추천 대상에 포함됩니다.")

    # Filter by exclude set
    filtered_candidates = [
        c
        for c in normalized_candidates
        if c.get("symbol", "").upper() not in exclude_set
    ]
    if normalized_candidates and not filtered_candidates:
        warnings.append("제외 조건 적용 후 추천 가능한 종목이 없습니다.")

    # Deduplicate
    deduped_candidates: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    duplicate_count = 0
    for candidate in filtered_candidates:
        symbol = str(candidate.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        if symbol in seen_symbols:
            duplicate_count += 1
            continue
        seen_symbols.add(symbol)
        candidate["symbol"] = symbol
        deduped_candidates.append(candidate)
    if duplicate_count > 0:
        warnings.append(f"중복 심볼 {duplicate_count}건을 제거했습니다.")

    logger.debug(
        "_stage_filter_by_strategy normalized=%d filtered=%d deduped=%d",
        len(normalized_candidates),
        len(filtered_candidates),
        len(deduped_candidates),
    )

    # Update diagnostics
    diagnostics["post_filter_candidates"] = len(normalized_candidates)
    diagnostics["strict_candidates"] = len(deduped_candidates)
    diagnostics["active_thresholds"] = {
        "min_market_cap": min_market_cap,
        "max_per": max_per,
        "max_pbr": max_pbr,
        "min_dividend_yield": min_dividend_yield,
    }

    # Count missing data
    for c in deduped_candidates:
        if c.get("per") is None:
            diagnostics["per_none_count"] = diagnostics.get("per_none_count", 0) + 1
        if c.get("pbr") is None:
            diagnostics["pbr_none_count"] = diagnostics.get("pbr_none_count", 0) + 1
        dy = c.get("dividend_yield")
        if dy is None:
            diagnostics["dividend_none_count"] = (
                diagnostics.get("dividend_none_count", 0) + 1
            )
        elif dy <= 0:
            diagnostics["dividend_zero_count"] = (
                diagnostics.get("dividend_zero_count", 0) + 1
            )

    # Fallback for value/dividend strategies
    needs_fallback = (
        validated_strategy in ("value", "dividend")
        and normalized_market == "kr"
        and len(deduped_candidates) < max_positions
        and screen_kr_fn is not None
    )

    if needs_fallback:
        logger.info(
            "_stage_filter_by_strategy 2-stage relaxation triggered strategy=%s strict=%d max_positions=%d",
            validated_strategy,
            len(deduped_candidates),
            max_positions,
        )
        fallback_params: dict[str, Any] = {}
        if validated_strategy == "value":
            fallback_params = {
                "max_per": 25.0,
                "max_pbr": 2.0,
                "min_market_cap": 200,
            }
        elif validated_strategy == "dividend":
            fallback_params = {
                "min_dividend_yield": 1.0,
                "min_market_cap": 200,
            }

        screen_asset_type = None
        screen_category = sectors[0] if sectors else None
        sort_by = strategy_screen_params.get("sort_by", "volume")
        sort_order = strategy_screen_params.get("sort_order", "desc")
        max_rsi = strategy_screen_params.get("max_rsi")
        candidate_limit = min(100, max(50, max_positions * 20))

        fallback_screen_kr_fn = screen_kr_fn
        if fallback_screen_kr_fn is None:
            raise RuntimeError("screen_kr_fn is required for KR fallback screening")
        try:
            fallback_result = await fallback_screen_kr_fn(
                market="kr",
                asset_type=screen_asset_type,
                category=screen_category,
                min_market_cap=fallback_params.get("min_market_cap"),
                max_per=fallback_params.get("max_per"),
                max_pbr=fallback_params.get("max_pbr"),
                min_dividend_yield=fallback_params.get("min_dividend_yield"),
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=candidate_limit,
                enrich_rsi=False,
            )
            if not fallback_result.get("error"):
                fallback_raw = fallback_result.get("results", [])
                fallback_normalized = [
                    _normalize_candidate(c, normalized_market) for c in fallback_raw
                ]
                added_count = 0
                for fc in fallback_normalized:
                    fsymbol = str(fc.get("symbol", "")).strip().upper()
                    if not fsymbol or fsymbol in seen_symbols:
                        continue
                    if fsymbol in exclude_set:
                        continue
                    if validated_strategy == "dividend":
                        fdy = fc.get("dividend_yield")
                        if fdy is None or fdy <= 0:
                            continue
                    if validated_strategy == "value":
                        penalty = 0
                        if fc.get("per") is None:
                            penalty -= 12
                        if fc.get("pbr") is None:
                            penalty -= 8
                        fc["_fallback_penalty"] = penalty
                    fc["symbol"] = fsymbol
                    seen_symbols.add(fsymbol)
                    deduped_candidates.append(fc)
                    added_count += 1
                if added_count > 0:
                    diagnostics["fallback_applied"] = True
                    diagnostics["fallback_candidates_added"] = added_count
                    diagnostics["fallback_thresholds"] = {
                        "min_market_cap": fallback_params.get("min_market_cap"),
                        "max_per": fallback_params.get("max_per"),
                        "max_pbr": fallback_params.get("max_pbr"),
                        "min_dividend_yield": fallback_params.get("min_dividend_yield"),
                    }
                    warnings.append(
                        f"{validated_strategy} strict 단계에서 후보가 부족해 fallback을 적용했습니다 (추가 {added_count}건)"
                    )
                    logger.info(
                        "_stage_filter_by_strategy fallback applied strategy=%s added=%d total=%d",
                        validated_strategy,
                        added_count,
                        len(deduped_candidates),
                    )
        except Exception as exc:
            fallback_error = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "_stage_filter_by_strategy fallback screening failed: %s",
                fallback_error,
            )
            warnings.append(f"Fallback 스크리닝 실패: {fallback_error}")

    # RSI enrichment for missing values
    rsi_missing_candidates = [
        c for c in deduped_candidates[:20] if c.get("rsi") is None
    ]
    if rsi_missing_candidates:
        logger.debug(
            "_stage_filter_by_strategy rsi enrichment start count=%d",
            len(rsi_missing_candidates),
        )
        rsi_semaphore = asyncio.Semaphore(5)

        async def _fetch_rsi_for_candidate(
            candidate: dict[str, Any],
        ) -> dict[str, Any]:
            async with rsi_semaphore:
                symbol = candidate.get("symbol", "")
                if not symbol:
                    return candidate
                try:
                    indicators = await _get_indicators_impl(
                        symbol, ["rsi"], normalized_market
                    )
                    if indicators.get("error"):
                        logger.debug(
                            "_stage_filter_by_strategy RSI fetch failed symbol=%s error=%s",
                            symbol,
                            indicators.get("error"),
                        )
                        return candidate
                    rsi_data = indicators.get("indicators", {}).get("rsi", {})
                    rsi_value = rsi_data.get("14")
                    if rsi_value is not None:
                        candidate["rsi"] = _to_optional_float(rsi_value)
                except Exception as exc:
                    logger.debug(
                        "_stage_filter_by_strategy RSI fetch exception symbol=%s error=%s",
                        symbol,
                        exc,
                    )
                return candidate

        try:
            updated_candidates = await asyncio.gather(
                *[_fetch_rsi_for_candidate(c) for c in rsi_missing_candidates],
                return_exceptions=True,
            )
            for i, updated in enumerate(updated_candidates):
                if isinstance(updated, dict):
                    payload = updated
                    rsi_missing_candidates[i].update(payload)
        except Exception as exc:
            logger.debug("_stage_filter_by_strategy RSI batch fetch failed: %s", exc)

    logger.info(
        "_stage_filter_by_strategy done market=%s strategy=%s candidates=%d",
        normalized_market,
        validated_strategy,
        len(deduped_candidates),
    )
    return deduped_candidates, diagnostics


async def _stage_allocate_budget(
    candidates: list[dict[str, Any]],
    *,
    budget: float,
    max_positions: int,
    strategy: str,
) -> tuple[list[dict[str, Any]], float]:
    """Stage 4: Allocate budget to top candidates.

    Args:
        candidates: Scored and filtered candidates
        budget: Total budget to allocate
        max_positions: Maximum number of positions
        strategy: Strategy name for reason generation

    Returns:
        Tuple of (allocated_positions, remaining_budget)
    """
    validated_strategy = validate_strategy(strategy)

    logger.info(
        "_stage_allocate_budget start budget=%.2f candidates=%d max_positions=%d",
        budget,
        len(candidates),
        max_positions,
    )

    allocated, remaining_budget = _allocate_budget(candidates, budget, max_positions)

    # Handle case where no allocation was possible
    if not allocated and candidates:
        valid_prices = [
            _to_float(item.get("price"), default=0.0)
            for item in candidates
            if _to_float(item.get("price"), default=0.0) > 0
        ]
        if valid_prices:
            min_price = min(valid_prices)
            if budget < min_price:
                logger.warning(
                    "_stage_allocate_budget budget %.2f < min_price %.2f",
                    budget,
                    min_price,
                )

    # Build recommendations with reasons
    recommendations = []
    for item in allocated:
        reason = _build_recommend_reason(item, validated_strategy, item.get("score", 0))
        recommendations.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "price": item.get("price"),
                "quantity": item.get("quantity"),
                "amount": item.get("amount"),
                "score": item.get("score"),
                "reason": reason,
                "rsi": item.get("rsi"),
                "per": item.get("per"),
                "change_rate": item.get("change_rate"),
            }
        )

    total_amount = sum(r.get("amount", 0) for r in recommendations)
    logger.info(
        "_stage_allocate_budget done recommendations=%d total_amount=%.2f remaining_budget=%.2f",
        len(recommendations),
        total_amount,
        remaining_budget,
    )

    return recommendations, remaining_budget


async def recommend_stocks_impl(
    *,
    budget: float,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None,
    sectors: list[str] | None,
    max_positions: int,
    top_stocks_fallback: Callable[..., Awaitable[dict[str, Any]]],
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]],
    screen_us_fn: Callable[..., Awaitable[dict[str, Any]]],
    screen_crypto_fn: Callable[..., Awaitable[dict[str, Any]]],
    top_stocks_override: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    exclude_held: bool = True,
) -> dict[str, Any]:
    """Orchestrate recommendation pipeline through staged functions.

    This function coordinates the recommendation flow:
    1. _stage_screen_candidates: Screen candidates from the market
    2. _stage_filter_by_strategy: Filter and enrich candidates (KR/US only)
    3. _stage_score_candidates: Score candidates using strategy weights (KR/US only)
    4. _stage_allocate_budget: Allocate budget to top candidates (KR/US only)

    Crypto market has a simplified flow due to its unique requirements.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")
    if max_positions < 1 or max_positions > 20:
        raise ValueError("max_positions must be between 1 and 20")

    validated_strategy = validate_strategy(strategy)
    normalized_market = _normalize_recommend_market(market)
    logger.info(
        "recommend_stocks start market=%s strategy=%s budget=%.2f max_positions=%d exclude_held=%s",
        normalized_market,
        validated_strategy,
        budget,
        max_positions,
        exclude_held,
    )

    try:
        candidate_limit = min(100, max(50, max_positions * 20))
        warnings: list[str] = []
        diagnostics: dict[str, Any] = {
            "raw_candidates": 0,
            "post_filter_candidates": 0,
            "per_none_count": 0,
            "pbr_none_count": 0,
            "dividend_none_count": 0,
            "dividend_zero_count": 0,
            "strict_candidates": 0,
            "fallback_candidates_added": 0,
            "fallback_applied": False,
            "active_thresholds": {},
        }

        # Stage 1: Screen candidates
        raw_candidates, screen_diagnostics = await _stage_screen_candidates(
            market=normalized_market,
            strategy=validated_strategy,
            screen_kr_fn=screen_kr_fn,
            screen_us_fn=screen_us_fn,
            screen_crypto_fn=screen_crypto_fn,
            top_stocks_fallback=top_stocks_fallback,
            top_stocks_override=top_stocks_override,
            candidate_limit=candidate_limit,
            sectors=sectors,
            warnings=warnings,
        )

        # Handle screening errors
        if screen_diagnostics.get("screen_error"):
            error_msg = screen_diagnostics["screen_error"]
            return {
                "recommendations": [],
                "total_amount": 0,
                "remaining_budget": budget,
                "strategy": validated_strategy,
                "strategy_description": get_strategy_description(validated_strategy),
                "candidates_screened": 0,
                "diagnostics": diagnostics,
                "fallback_applied": False,
                "warnings": [
                    *warnings,
                    f"{normalized_market.upper()} 후보 스크리닝 실패: {error_msg}",
                ],
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }

        diagnostics["raw_candidates"] = screen_diagnostics.get("raw_candidates", 0)

        # Crypto has a special simplified flow
        if normalized_market == "crypto":
            return await _handle_crypto_recommendation(
                raw_candidates=raw_candidates,
                budget=budget,
                max_positions=max_positions,
                exclude_symbols=exclude_symbols,
                exclude_held=exclude_held,
                validated_strategy=validated_strategy,
                diagnostics=diagnostics,
                warnings=warnings,
            )

        # Stage 2: Filter by strategy (normalize, exclude, dedupe, fallback, RSI enrichment)
        filtered_candidates, filter_diagnostics = await _stage_filter_by_strategy(
            raw_candidates,
            market=normalized_market,
            strategy=validated_strategy,
            exclude_symbols=exclude_symbols,
            exclude_held=exclude_held,
            max_positions=max_positions,
            screen_kr_fn=screen_kr_fn,
            sectors=sectors,
            warnings=warnings,
            diagnostics=diagnostics,
        )

        # Merge diagnostics from filter stage
        diagnostics.update(filter_diagnostics)

        if not filtered_candidates:
            warnings.append("필터링 후 추천 가능한 종목이 없습니다.")
            return {
                "recommendations": [],
                "total_amount": 0,
                "remaining_budget": round(budget, 2),
                "strategy": validated_strategy,
                "strategy_description": get_strategy_description(validated_strategy),
                "candidates_screened": diagnostics.get("raw_candidates", 0),
                "diagnostics": diagnostics,
                "fallback_applied": diagnostics.get("fallback_applied", False),
                "warnings": warnings,
                "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            }

        # Stage 3: Score candidates
        scored_candidates = await _stage_score_candidates(
            filtered_candidates,
            strategy=validated_strategy,
        )

        # Stage 4: Allocate budget
        recommendations, remaining_budget = await _stage_allocate_budget(
            scored_candidates,
            budget=budget,
            max_positions=max_positions,
            strategy=validated_strategy,
        )

        # Handle case where no allocation was possible
        if not recommendations and scored_candidates:
            valid_prices = [
                _to_float(item.get("price"), default=0.0)
                for item in scored_candidates
                if _to_float(item.get("price"), default=0.0) > 0
            ]
            if valid_prices:
                min_price = min(valid_prices)
                if budget < min_price:
                    warnings.append(
                        "예산이 최소 구매 금액보다 작아 종목을 배분하지 못했습니다. "
                        f"(budget={budget:.2f}, min_price={min_price:.2f})"
                    )

        total_amount = sum(r.get("amount", 0) for r in recommendations)
        logger.info(
            "recommend_stocks done recommendations=%d total_amount=%.2f remaining_budget=%.2f",
            len(recommendations),
            total_amount,
            remaining_budget,
        )

        return {
            "recommendations": recommendations,
            "total_amount": round(total_amount, 2),
            "remaining_budget": remaining_budget,
            "strategy": validated_strategy,
            "strategy_description": get_strategy_description(validated_strategy),
            "candidates_screened": diagnostics.get("raw_candidates", 0),
            "diagnostics": diagnostics,
            "fallback_applied": diagnostics.get("fallback_applied", False),
            "warnings": warnings,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
    except Exception as exc:
        error_message = str(exc).strip() or exc.__class__.__name__
        error_traceback = traceback.format_exc()
        logger.exception(
            "recommend_stocks failed market=%s strategy=%s budget=%.2f max_positions=%d",
            normalized_market,
            validated_strategy,
            budget,
            max_positions,
        )
        return _error_payload(
            source="recommend_stocks",
            message=f"recommend_stocks failed: {error_message}",
            query=(
                f"market={normalized_market},strategy={validated_strategy},"
                f"budget={budget},max_positions={max_positions}"
            ),
            details=error_traceback,
        )


async def _handle_crypto_recommendation(
    *,
    raw_candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
    exclude_symbols: list[str] | None,
    exclude_held: bool,
    validated_strategy: StrategyType,
    diagnostics: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Handle crypto recommendation with crypto-specific logic.

    Crypto has a simplified flow:
    - Normalize candidates
    - Exclude symbols and held positions
    - Take top N by RSI (already sorted)
    - Allocate equal budget
    - Return with crypto-specific fields
    """
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
    )

    # Normalize candidates
    crypto_candidates = [_normalize_candidate(c, "crypto") for c in raw_candidates]
    diagnostics["raw_candidates"] = len(crypto_candidates)

    # Build exclude set
    crypto_exclude_set: set[str] = set()
    if exclude_symbols:
        crypto_exclude_set.update(
            str(s).strip().upper() for s in exclude_symbols if s and str(s).strip()
        )

    # Exclude held positions
    if exclude_held:
        try:
            (
                held_positions,
                held_errors,
                _,
                _,
            ) = await _collect_portfolio_positions(
                account=None,
                market="crypto",
                include_current_price=False,
                user_id=_MCP_USER_ID,
            )
            for pos in held_positions:
                sym = pos.get("symbol", "")
                if sym:
                    crypto_exclude_set.add(str(sym).upper())
            if held_errors:
                warnings.append(f"보유 종목 조회 중 일부 오류: {len(held_errors)}건")
        except Exception as exc:
            warnings.append(f"보유 종목 조회 실패: {exc}")
    else:
        warnings.append("exclude_held=False: 보유 종목도 추천 대상에 포함됩니다.")

    # Filter by exclude set
    crypto_candidates = [
        c
        for c in crypto_candidates
        if c.get("symbol", "").upper() not in crypto_exclude_set
    ]

    # Filter for valid prices
    eligible_candidates = [
        item
        for item in crypto_candidates
        if _to_float(item.get("price"), default=0.0) > 0
    ]
    diagnostics["post_filter_candidates"] = len(eligible_candidates)
    diagnostics["strict_candidates"] = len(eligible_candidates)

    # Take top N
    picks = eligible_candidates[:max_positions]
    if not picks:
        warnings.append("스크리닝 결과가 없어 추천 가능한 종목이 없습니다.")
        return {
            "recommendations": [],
            "total_amount": 0,
            "remaining_budget": round(budget, 2),
            "strategy": validated_strategy,
            "strategy_description": get_strategy_description(validated_strategy),
            "candidates_screened": diagnostics["raw_candidates"],
            "diagnostics": diagnostics,
            "fallback_applied": False,
            "warnings": warnings,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }

    # Equal allocation
    per_coin_budget = budget / len(picks)

    recommendations = []
    for item in picks:
        price = _to_float(item.get("price"), default=0.0)
        if price <= 0:
            continue
        quantity = per_coin_budget / price
        reason = _build_crypto_rsi_reason(item)
        recommendations.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "price": price,
                "quantity": round(quantity, 12),
                "budget": round(per_coin_budget, 2),
                "amount": round(per_coin_budget, 2),
                "reason": reason,
                "rsi": item.get("rsi"),
                "rsi_bucket": item.get("rsi_bucket"),
                "per": None,
                "change_rate": item.get("change_rate"),
                "trade_amount_24h": item.get("trade_amount_24h"),
                "market_warning": item.get("market_warning"),
                "market_cap": item.get("market_cap"),
                "market_cap_rank": item.get("market_cap_rank"),
                "volume_24h": item.get("volume_24h"),
                "volume_ratio": item.get("volume_ratio"),
                "candle_type": item.get("candle_type"),
                "adx": item.get("adx"),
                "plus_di": item.get("plus_di"),
                "minus_di": item.get("minus_di"),
            }
        )

    total_amount = sum(r.get("amount", 0) for r in recommendations)
    remaining_budget = round(max(0.0, budget - total_amount), 2)

    return {
        "recommendations": recommendations,
        "total_amount": round(total_amount, 2),
        "remaining_budget": remaining_budget,
        "strategy": validated_strategy,
        "strategy_description": get_strategy_description(validated_strategy),
        "candidates_screened": diagnostics["raw_candidates"],
        "diagnostics": diagnostics,
        "fallback_applied": False,
        "warnings": warnings,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


normalize_recommend_market = _normalize_recommend_market
build_recommend_reason = _build_recommend_reason
normalize_candidate = _normalize_candidate
allocate_budget = _allocate_budget

__all__ = [
    "_normalize_recommend_market",
    "_build_recommend_reason",
    "_normalize_candidate",
    "_allocate_budget",
    "_stage_screen_candidates",
    "_stage_score_candidates",
    "_stage_filter_by_strategy",
    "_stage_allocate_budget",
    "recommend_stocks_impl",
    "normalize_recommend_market",
    "build_recommend_reason",
    "normalize_candidate",
    "allocate_budget",
]
