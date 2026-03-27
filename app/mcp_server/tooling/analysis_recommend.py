"""Recommend-stocks helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from app.mcp_server.scoring import calc_composite_score, generate_reason
from app.mcp_server.strategies import (
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


@dataclass(frozen=True, slots=True)
class RecommendRequestContext:
    budget: float
    exclude_held: bool
    exclude_symbols: list[str] | None
    candidate_limit: int
    diagnostics: dict[str, Any] = field(default_factory=dict)
    max_positions: int = 0
    normalized_market: str = "kr"
    screen_asset_type: str | None = None
    screen_category: str | None = None
    sort_by: str = "volume"
    sort_order: str = "desc"
    min_market_cap: float | None = None
    max_per: float | None = None
    max_pbr: float | None = None
    min_dividend_yield: float | None = None
    max_rsi: float | None = None
    strategy_description: str = ""
    validated_strategy: str = "balanced"
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


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


def _dedupe_allocatable_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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

    return deduped_candidates


def allocate_budget(
    candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
    *,
    mode: str = "weighted",
) -> tuple[list[dict[str, Any]], float]:
    if not candidates or budget <= 0:
        return [], round(budget, 2)

    if mode not in {"weighted", "equal"}:
        raise ValueError("mode must be one of: weighted, equal")

    deduped_candidates = _dedupe_allocatable_candidates(candidates)

    top_candidates = deduped_candidates[:max_positions]
    if not top_candidates:
        return [], round(budget, 2)

    equal_ratio = 1.0 / len(top_candidates)
    total_score = sum(
        max(_to_float(item.get("score"), default=0.0), 0.0) for item in top_candidates
    )

    allocated: list[dict[str, Any]] = []
    remaining = float(budget)

    for item in top_candidates:
        price = item.get("price", 0)
        if mode == "equal":
            alloc_ratio = equal_ratio
        else:
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
        if mode == "equal":
            sorted_allocated = sorted(
                allocated,
                key=lambda item: _to_float(item.get("rsi") or 999, default=999),
            )
        else:
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


def _allocate_budget_equal(
    candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
) -> tuple[list[dict[str, Any]], float]:
    return allocate_budget(candidates, budget, max_positions, mode="equal")


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
    return allocate_budget(candidates, budget, max_positions, mode="weighted")


def _prepare_recommend_request(
    *,
    budget: float,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None,
    sectors: list[str] | None,
    max_positions: int,
    exclude_held: bool,
) -> RecommendRequestContext:
    if budget <= 0:
        raise ValueError("budget must be positive")
    if max_positions < 1 or max_positions > 20:
        raise ValueError("max_positions must be between 1 and 20")

    validated_strategy = validate_strategy(strategy)
    normalized_market = _normalize_recommend_market(market)
    strategy_description = get_strategy_description(validated_strategy)
    strategy_screen_params = get_strategy_screen_params(validated_strategy)
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
    candidate_limit = min(100, max(50, max_positions * 20))
    screen_asset_type = None
    screen_category = sectors[0] if sectors else None
    sort_by = strategy_screen_params.get("sort_by", "volume")
    sort_order = strategy_screen_params.get("sort_order", "desc")
    min_market_cap = strategy_screen_params.get("min_market_cap")
    max_per = strategy_screen_params.get("max_per")
    max_pbr = strategy_screen_params.get("max_pbr")
    min_dividend_yield = strategy_screen_params.get("min_dividend_yield")
    max_rsi = strategy_screen_params.get("max_rsi")

    if normalized_market == "crypto":
        if screen_category is not None:
            warnings.append(
                "crypto market does not support sectors/category filter; ignored."
            )
        if validated_strategy != "oversold":
            warnings.append(
                f"crypto market에서 strategy='{validated_strategy}'는 무시됩니다. "
                "RSI ascending 정렬 고정."
            )
        screen_category = None
        sort_by = "rsi"
        sort_order = "asc"
        min_market_cap = None
        max_per = None
        max_pbr = None
        min_dividend_yield = None
        max_rsi = None

    if normalized_market == "us" and max_pbr is not None:
        warnings.append("us market screener does not support max_pbr filter; ignored.")
        max_pbr = None

    logger.debug(
        "recommend_stocks strategy params strategy=%s params=%s",
        validated_strategy,
        strategy_screen_params,
    )
    return RecommendRequestContext(
        budget=budget,
        exclude_held=exclude_held,
        exclude_symbols=exclude_symbols,
        candidate_limit=candidate_limit,
        diagnostics=diagnostics,
        max_positions=max_positions,
        normalized_market=normalized_market,
        screen_asset_type=screen_asset_type,
        screen_category=screen_category,
        sort_by=sort_by,
        sort_order=sort_order,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
        strategy_description=strategy_description,
        validated_strategy=validated_strategy,
        warnings=warnings,
    )


async def _collect_kr_candidates(
    *,
    request: RecommendRequestContext,
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    screen_result = await screen_kr_fn(
        market="kr",
        asset_type=request["screen_asset_type"],
        category=request["screen_category"],
        min_market_cap=request["min_market_cap"],
        max_per=request["max_per"],
        max_pbr=request["max_pbr"],
        min_dividend_yield=request["min_dividend_yield"],
        max_rsi=request["max_rsi"],
        sort_by=request["sort_by"],
        sort_order=request["sort_order"],
        limit=request["candidate_limit"],
    )
    screen_error_raw = screen_result.get("error")
    if screen_error_raw is not None:
        screen_error = str(screen_error_raw).strip() or "unknown error"
        logger.warning("recommend_stocks KR screening failed: %s", screen_error)
        return [], _empty_recommend_response(
            budget=request["budget"],
            strategy=request["validated_strategy"],
            strategy_description=request["strategy_description"],
            warnings=[*request["warnings"], f"KR 후보 스크리닝 실패: {screen_error}"],
            diagnostics=request["diagnostics"],
            fallback_applied=False,
        )

    candidates = [
        _normalize_candidate(item, "kr") for item in screen_result.get("results", [])
    ]
    return candidates, None


async def _collect_us_candidates(
    *,
    request: RecommendRequestContext,
    top_stocks_fallback: Any,
    top_stocks_override: Any,
) -> list[dict[str, Any]]:
    raw_candidates: list[dict[str, Any]] = []
    top_stocks_fn_raw = top_stocks_override if callable(top_stocks_override) else None
    if top_stocks_fn_raw is None:
        top_stocks_fn_raw = top_stocks_fallback
    top_stocks_fn: Callable[..., Awaitable[dict[str, Any]]] = cast(
        Callable[..., Awaitable[dict[str, Any]]],
        top_stocks_fn_raw,
    )

    try:
        top_result = await top_stocks_fn(
            market="us",
            ranking_type="volume",
            limit=min(request["candidate_limit"], 50),
        )
        if top_result.get("error"):
            top_error = str(top_result.get("error")).strip() or "unknown error"
            logger.warning("recommend_stocks US get_top_stocks failed: %s", top_error)
            request["warnings"].append(f"US 후보 수집 실패: {top_error}")
        else:
            raw_candidates = top_result.get("rankings", [])
    except Exception as exc:
        top_error = str(exc).strip() or exc.__class__.__name__
        logger.warning(
            "recommend_stocks US get_top_stocks exception: %s",
            top_error,
            exc_info=True,
        )
        request["warnings"].append(f"US 후보 수집 실패: {top_error}")

    return [_normalize_candidate(item, "us") for item in raw_candidates]


async def _collect_crypto_candidates(
    *,
    request: RecommendRequestContext,
    screen_crypto_fn: Callable[..., Awaitable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    screen_result = await screen_crypto_fn(
        market="crypto",
        asset_type=request["screen_asset_type"],
        category=request["screen_category"],
        min_market_cap=None,
        max_per=None,
        min_dividend_yield=None,
        max_rsi=None,
        sort_by="rsi",
        sort_order="asc",
        limit=CRYPTO_PREFILTER_LIMIT,
    )
    screen_error_raw = screen_result.get("error")
    if screen_error_raw is not None:
        screen_error = str(screen_error_raw).strip() or "unknown error"
        logger.warning("recommend_stocks crypto screening failed: %s", screen_error)
        return [], _empty_recommend_response(
            budget=request["budget"],
            strategy=request["validated_strategy"],
            strategy_description=request["strategy_description"],
            warnings=[
                *request["warnings"],
                f"Crypto 후보 스크리닝 실패: {screen_error}",
            ],
            diagnostics=request["diagnostics"],
            fallback_applied=False,
        )

    screen_warnings = screen_result.get("warnings")
    if isinstance(screen_warnings, list):
        request["warnings"].extend(
            str(warning) for warning in screen_warnings if warning
        )

    candidates = [
        _normalize_candidate(item, "crypto")
        for item in screen_result.get("results", [])
    ]
    request["diagnostics"]["raw_candidates"] = len(candidates)
    return candidates, None


async def _apply_exclusions_and_dedupe(
    *,
    candidates: list[dict[str, Any]],
    request: RecommendRequestContext,
    collect_positions_fn: Callable[..., Awaitable[Any]],
    dedupe: bool = True,
    warn_when_all_excluded: bool = True,
) -> tuple[list[dict[str, Any]], set[str]]:
    exclude_set: set[str] = set()
    if request["exclude_symbols"]:
        exclude_set.update(
            str(symbol).strip().upper()
            for symbol in request["exclude_symbols"]
            if symbol is not None and str(symbol).strip()
        )

    if request["exclude_held"]:
        try:
            holdings_positions, holdings_errors, _, _ = await collect_positions_fn(
                account=None,
                market=request["normalized_market"],
                include_current_price=False,
                user_id=_MCP_USER_ID,
            )
            for position in holdings_positions:
                symbol = position.get("symbol", "")
                if symbol:
                    exclude_set.add(str(symbol).upper())
            if holdings_errors:
                request["warnings"].append(
                    f"보유 종목 조회 중 일부 오류: {len(holdings_errors)}건"
                )
        except Exception as exc:
            holdings_error = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "recommend_stocks holdings lookup failed: %s",
                holdings_error,
                exc_info=True,
            )
            request["warnings"].append(f"보유 종목 조회 실패: {holdings_error}")
    else:
        request["warnings"].append(
            "exclude_held=False: 보유 종목도 추천 대상에 포함됩니다."
        )

    filtered_candidates = [
        candidate
        for candidate in candidates
        if candidate.get("symbol", "").upper() not in exclude_set
    ]
    if warn_when_all_excluded and candidates and not filtered_candidates:
        request["warnings"].append("제외 조건 적용 후 추천 가능한 종목이 없습니다.")

    if not dedupe:
        return filtered_candidates, exclude_set

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
        request["warnings"].append(f"중복 심볼 {duplicate_count}건을 제거했습니다.")
    return deduped_candidates, exclude_set


async def _apply_kr_relaxed_fallback(
    *,
    candidates: list[dict[str, Any]],
    exclude_set: set[str],
    request: RecommendRequestContext,
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if not (
        request["validated_strategy"] in ("value", "dividend")
        and request["normalized_market"] == "kr"
        and len(candidates) < request["max_positions"]
    ):
        return candidates

    logger.info(
        "recommend_stocks 2-stage relaxation triggered strategy=%s strict=%d max_positions=%d",
        request["validated_strategy"],
        len(candidates),
        request["max_positions"],
    )
    fallback_params: dict[str, Any] = {}
    if request["validated_strategy"] == "value":
        fallback_params = {"max_per": 25.0, "max_pbr": 2.0, "min_market_cap": 200}
    elif request["validated_strategy"] == "dividend":
        fallback_params = {"min_dividend_yield": 1.0, "min_market_cap": 200}

    seen_symbols = {
        str(candidate.get("symbol", "")).strip().upper()
        for candidate in candidates
        if str(candidate.get("symbol", "")).strip()
    }
    try:
        fallback_result = await screen_kr_fn(
            market="kr",
            asset_type=request["screen_asset_type"],
            category=request["screen_category"],
            min_market_cap=fallback_params.get("min_market_cap"),
            max_per=fallback_params.get("max_per"),
            max_pbr=fallback_params.get("max_pbr"),
            min_dividend_yield=fallback_params.get("min_dividend_yield"),
            max_rsi=request["max_rsi"],
            sort_by=request["sort_by"],
            sort_order=request["sort_order"],
            limit=request["candidate_limit"],
        )
        if not fallback_result.get("error"):
            fallback_normalized = [
                _normalize_candidate(item, request["normalized_market"])
                for item in fallback_result.get("results", [])
            ]
            added_count = 0
            for fallback_candidate in fallback_normalized:
                symbol = str(fallback_candidate.get("symbol", "")).strip().upper()
                if not symbol or symbol in seen_symbols or symbol in exclude_set:
                    continue
                if request["validated_strategy"] == "dividend":
                    dividend_yield = fallback_candidate.get("dividend_yield")
                    if dividend_yield is None or dividend_yield <= 0:
                        continue
                if request["validated_strategy"] == "value":
                    penalty = 0
                    if fallback_candidate.get("per") is None:
                        penalty -= 12
                    if fallback_candidate.get("pbr") is None:
                        penalty -= 8
                    fallback_candidate["_fallback_penalty"] = penalty
                fallback_candidate["symbol"] = symbol
                seen_symbols.add(symbol)
                candidates.append(fallback_candidate)
                added_count += 1

            if added_count > 0:
                request["diagnostics"]["fallback_applied"] = True
                request["diagnostics"]["fallback_candidates_added"] = added_count
                request["diagnostics"]["fallback_thresholds"] = {
                    "min_market_cap": fallback_params.get("min_market_cap"),
                    "max_per": fallback_params.get("max_per"),
                    "max_pbr": fallback_params.get("max_pbr"),
                    "min_dividend_yield": fallback_params.get("min_dividend_yield"),
                }
                request["warnings"].append(
                    f"{request['validated_strategy']} strict 단계에서 후보가 부족해 fallback을 적용했습니다 (추가 {added_count}건)"
                )
                logger.info(
                    "recommend_stocks fallback applied strategy=%s added=%d total=%d",
                    request["validated_strategy"],
                    added_count,
                    len(candidates),
                )
    except Exception as exc:
        fallback_error = str(exc).strip() or exc.__class__.__name__
        logger.warning("recommend_stocks fallback screening failed: %s", fallback_error)
        request["warnings"].append(f"Fallback 스크리닝 실패: {fallback_error}")

    return candidates


def _score_and_allocate(
    *,
    candidates: list[dict[str, Any]],
    budget: float,
    max_positions: int,
    strategy: str,
) -> tuple[list[dict[str, Any]], float]:
    if not candidates or budget <= 0:
        return [], round(budget, 2)

    if candidates[0].get("market") == "crypto":
        picks = candidates[:max_positions]
        if not picks:
            return [], round(budget, 2)

        per_coin_budget = budget / len(picks)
        recommendations: list[dict[str, Any]] = []
        for item in picks:
            price = _to_float(item.get("price"), default=0.0)
            if price <= 0:
                continue
            quantity = per_coin_budget / price
            recommendations.append(
                {
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "price": price,
                    "quantity": round(quantity, 12),
                    "budget": round(per_coin_budget, 2),
                    "amount": round(per_coin_budget, 2),
                    "reason": _build_crypto_rsi_reason(item),
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

        total_amount = sum(item.get("amount", 0) for item in recommendations)
        return recommendations, round(max(0.0, budget - total_amount), 2)

    validated_strategy = validate_strategy(strategy)
    strategy_weights = get_strategy_scoring_weights(validated_strategy)
    logger.debug(
        "recommend_stocks scoring start strategy=%s weights=%s",
        validated_strategy,
        strategy_weights,
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

    scored_candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    allocated, remaining_budget = _allocate_budget(
        scored_candidates, budget, max_positions
    )
    recommendations = []
    for item in allocated:
        recommendations.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "price": item.get("price"),
                "quantity": item.get("quantity"),
                "amount": item.get("amount"),
                "score": item.get("score"),
                "reason": _build_recommend_reason(
                    item,
                    validated_strategy,
                    item.get("score", 0),
                ),
                "rsi": item.get("rsi"),
                "per": item.get("per"),
                "change_rate": item.get("change_rate"),
            }
        )
    return recommendations, remaining_budget


def _empty_recommend_response(
    *,
    budget: float,
    strategy: str,
    strategy_description: str,
    warnings: list[str],
    diagnostics: dict[str, Any],
    fallback_applied: bool,
    candidates_screened: int = 0,
) -> dict[str, Any]:
    return _build_recommend_response(
        recommendations=[],
        remaining_budget=round(budget, 2),
        strategy=strategy,
        strategy_description=strategy_description,
        candidates_screened=candidates_screened,
        diagnostics=diagnostics,
        fallback_applied=fallback_applied,
        warnings=warnings,
    )


def _build_recommend_response(
    *,
    recommendations: list[dict[str, Any]],
    remaining_budget: float,
    strategy: str,
    strategy_description: str,
    candidates_screened: int,
    diagnostics: dict[str, Any],
    fallback_applied: bool,
    warnings: list[str],
) -> dict[str, Any]:
    total_amount = sum(
        _to_float(item.get("amount"), default=0.0) for item in recommendations
    )
    return {
        "recommendations": recommendations,
        "total_amount": round(total_amount, 2),
        "remaining_budget": round(remaining_budget, 2),
        "strategy": strategy,
        "strategy_description": strategy_description,
        "candidates_screened": candidates_screened,
        "diagnostics": diagnostics,
        "fallback_applied": fallback_applied,
        "warnings": warnings,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }


async def recommend_stocks_impl(
    *,
    budget: float,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None,
    sectors: list[str] | None,
    max_positions: int,
    top_stocks_fallback: Any,
    screen_kr_fn: Callable[..., Awaitable[dict[str, Any]]],
    screen_crypto_fn: Callable[..., Awaitable[dict[str, Any]]],
    top_stocks_override: Any = None,
    exclude_held: bool = True,
) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
    )

    request: RecommendRequestContext = _prepare_recommend_request(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        exclude_held=exclude_held,
    )
    validated_strategy = request["validated_strategy"]
    normalized_market = request["normalized_market"]
    logger.info(
        "recommend_stocks start market=%s strategy=%s budget=%.2f max_positions=%d exclude_held=%s",
        normalized_market,
        validated_strategy,
        budget,
        max_positions,
        exclude_held,
    )

    try:
        logger.info(
            "recommend_stocks screening start market=%s strategy=%s limit=%d sort_by=%s",
            normalized_market,
            validated_strategy,
            request["candidate_limit"],
            request["sort_by"],
        )

        if normalized_market == "kr":
            candidates, early_response = await _collect_kr_candidates(
                request=request,
                screen_kr_fn=screen_kr_fn,
            )
        elif normalized_market == "us":
            candidates = await _collect_us_candidates(
                request=request,
                top_stocks_fallback=top_stocks_fallback,
                top_stocks_override=top_stocks_override,
            )
            early_response = None
        else:
            candidates, early_response = await _collect_crypto_candidates(
                request=request,
                screen_crypto_fn=screen_crypto_fn,
            )

        if early_response is not None:
            return early_response

        logger.info(
            "recommend_stocks screening finished market=%s raw_candidates=%d",
            normalized_market,
            len(candidates),
        )

        candidates_screened = (
            request["diagnostics"]["raw_candidates"]
            if normalized_market == "crypto"
            else len(candidates)
        )

        if normalized_market == "crypto":
            filtered_candidates, _ = await _apply_exclusions_and_dedupe(
                candidates=candidates,
                request=request,
                collect_positions_fn=_collect_portfolio_positions,
                dedupe=False,
                warn_when_all_excluded=False,
            )
            eligible_candidates = [
                item
                for item in filtered_candidates
                if _to_float(item.get("price"), default=0.0) > 0
            ]
            request["diagnostics"]["post_filter_candidates"] = len(eligible_candidates)
            request["diagnostics"]["strict_candidates"] = len(eligible_candidates)
            if not eligible_candidates:
                request["warnings"].append(
                    "스크리닝 결과가 없어 추천 가능한 종목이 없습니다."
                )
                return _empty_recommend_response(
                    budget=request["budget"],
                    strategy=validated_strategy,
                    strategy_description=request["strategy_description"],
                    warnings=request["warnings"],
                    diagnostics=request["diagnostics"],
                    fallback_applied=False,
                    candidates_screened=candidates_screened,
                )

            recommendations, remaining_budget = _score_and_allocate(
                candidates=eligible_candidates,
                budget=request["budget"],
                max_positions=request["max_positions"],
                strategy=validated_strategy,
            )
            return _build_recommend_response(
                recommendations=recommendations,
                remaining_budget=remaining_budget,
                strategy=validated_strategy,
                strategy_description=request["strategy_description"],
                candidates_screened=candidates_screened,
                diagnostics=request["diagnostics"],
                fallback_applied=False,
                warnings=request["warnings"],
            )

        if not candidates:
            request["warnings"].append(
                "스크리닝 결과가 없어 추천 가능한 종목이 없습니다."
            )

        deduped_candidates, exclude_set = await _apply_exclusions_and_dedupe(
            candidates=candidates,
            request=request,
            collect_positions_fn=_collect_portfolio_positions,
        )
        request["diagnostics"]["raw_candidates"] = len(candidates)
        request["diagnostics"]["post_filter_candidates"] = len(candidates)
        request["diagnostics"]["strict_candidates"] = len(deduped_candidates)
        request["diagnostics"]["active_thresholds"] = {
            "min_market_cap": request["min_market_cap"],
            "max_per": request["max_per"],
            "max_pbr": request["max_pbr"],
            "min_dividend_yield": request["min_dividend_yield"],
        }
        for candidate in deduped_candidates:
            if candidate.get("per") is None:
                request["diagnostics"]["per_none_count"] += 1
            if candidate.get("pbr") is None:
                request["diagnostics"]["pbr_none_count"] += 1
            dividend_yield = candidate.get("dividend_yield")
            if dividend_yield is None:
                request["diagnostics"]["dividend_none_count"] += 1
            elif dividend_yield <= 0:
                request["diagnostics"]["dividend_zero_count"] += 1

        deduped_candidates = await _apply_kr_relaxed_fallback(
            candidates=deduped_candidates,
            exclude_set=exclude_set,
            request=request,
            screen_kr_fn=screen_kr_fn,
        )

        logger.info(
            "recommend_stocks allocation start budget=%.2f candidates=%d max_positions=%d",
            budget,
            len(deduped_candidates),
            max_positions,
        )
        recommendations, remaining_budget = _score_and_allocate(
            candidates=deduped_candidates,
            budget=request["budget"],
            max_positions=request["max_positions"],
            strategy=validated_strategy,
        )
        if not recommendations and deduped_candidates:
            valid_prices = [
                _to_float(candidate.get("price"), default=0.0)
                for candidate in deduped_candidates
                if _to_float(candidate.get("price"), default=0.0) > 0
            ]
            if valid_prices:
                min_price = min(valid_prices)
                if budget < min_price:
                    request["warnings"].append(
                        "예산이 최소 구매 금액보다 작아 종목을 배분하지 못했습니다. "
                        f"(budget={budget:.2f}, min_price={min_price:.2f})"
                    )

        response = _build_recommend_response(
            recommendations=recommendations,
            remaining_budget=remaining_budget,
            strategy=validated_strategy,
            strategy_description=request["strategy_description"],
            candidates_screened=candidates_screened,
            diagnostics=request["diagnostics"],
            fallback_applied=request["diagnostics"].get("fallback_applied", False),
            warnings=request["warnings"],
        )
        logger.info(
            "recommend_stocks done recommendations=%d total_amount=%.2f remaining_budget=%.2f",
            len(response["recommendations"]),
            response["total_amount"],
            response["remaining_budget"],
        )
        return response
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


normalize_recommend_market = _normalize_recommend_market
build_recommend_reason = _build_recommend_reason
normalize_candidate = _normalize_candidate
__all__ = [
    "_normalize_recommend_market",
    "_build_recommend_reason",
    "_normalize_candidate",
    "_allocate_budget",
    "_prepare_recommend_request",
    "_collect_kr_candidates",
    "_collect_us_candidates",
    "_collect_crypto_candidates",
    "_apply_exclusions_and_dedupe",
    "_apply_kr_relaxed_fallback",
    "_enrich_missing_rsi",
    "_score_and_allocate",
    "_empty_recommend_response",
    "_build_recommend_response",
    "recommend_stocks_impl",
    "normalize_recommend_market",
    "build_recommend_reason",
    "normalize_candidate",
    "allocate_budget",
]
