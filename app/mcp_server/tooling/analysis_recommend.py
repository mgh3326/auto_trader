"""Recommend-stocks helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
import datetime
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

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
    return {
        "symbol": symbol,
        "name": item.get("name") or item.get("shortname") or "",
        "price": _to_float(item.get("close") or item.get("price") or item.get("trade_price")),
        "change_rate": _to_float(item.get("change_rate") or 0),
        "volume": _to_int(item.get("volume") or 0),
        "market_cap": _to_float(item.get("market_cap") or 0),
        "per": _to_optional_float(item.get("per")),
        "pbr": _to_optional_float(item.get("pbr")),
        "dividend_yield": _to_optional_float(item.get("dividend_yield")),
        "rsi_14": _to_optional_float(item.get("rsi") or item.get("rsi_14")),
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
) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import (
        _collect_portfolio_positions,
        _get_indicators_impl,
    )

    if budget <= 0:
        raise ValueError("budget must be positive")
    if max_positions < 1 or max_positions > 20:
        raise ValueError("max_positions must be between 1 and 20")

    validated_strategy = validate_strategy(strategy)
    normalized_market = _normalize_recommend_market(market)
    logger.info(
        "recommend_stocks start market=%s strategy=%s budget=%.2f max_positions=%d",
        normalized_market,
        validated_strategy,
        budget,
        max_positions,
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

        strategy_screen_params = get_strategy_screen_params(validated_strategy)
        sort_by = strategy_screen_params.get("sort_by", "volume")
        sort_order = strategy_screen_params.get("sort_order", "desc")
        min_market_cap = strategy_screen_params.get("min_market_cap")
        max_per = strategy_screen_params.get("max_per")
        max_pbr = strategy_screen_params.get("max_pbr")
        min_dividend_yield = strategy_screen_params.get("min_dividend_yield")
        max_rsi = strategy_screen_params.get("max_rsi")
        logger.debug(
            "recommend_stocks strategy params strategy=%s params=%s",
            validated_strategy,
            strategy_screen_params,
        )

        screen_asset_type = None
        screen_category = sectors[0] if sectors else None

        if normalized_market == "crypto":
            if screen_category is not None:
                warnings.append(
                    "crypto market does not support sectors/category filter; ignored."
                )
            screen_asset_type = None
            screen_category = None
            if max_per is not None:
                warnings.append("crypto market does not support max_per filter; ignored.")
                max_per = None
            if max_pbr is not None:
                warnings.append("crypto market does not support max_pbr filter; ignored.")
                max_pbr = None
            if min_dividend_yield is not None:
                warnings.append(
                    "crypto market does not support min_dividend_yield filter; ignored."
                )
                min_dividend_yield = None
            if sort_by == "dividend_yield":
                warnings.append(
                    "crypto market does not support dividend_yield sorting; fallback to volume."
                )
                sort_by = "volume"

        if normalized_market == "us" and max_pbr is not None:
            warnings.append("us market screener does not support max_pbr filter; ignored.")
            max_pbr = None

        logger.info(
            "recommend_stocks screening start market=%s strategy=%s limit=%d sort_by=%s",
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
            )
            screen_error_raw = screen_result.get("error")
            if screen_error_raw is not None:
                screen_error = str(screen_error_raw).strip() or "unknown error"
                logger.warning("recommend_stocks KR screening failed: %s", screen_error)
                return {
                    "recommendations": [],
                    "total_amount": 0,
                    "remaining_budget": budget,
                    "strategy": validated_strategy,
                    "strategy_description": get_strategy_description(validated_strategy),
                    "candidates_screened": 0,
                    "diagnostics": diagnostics,
                    "fallback_applied": False,
                    "disclaimer": "투자 권유가 아닙니다. 모든 투자의 책임은 투자자에게 있습니다.",
                    "warnings": [*warnings, f"KR 후보 스크리닝 실패: {screen_error}"],
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            raw_candidates = screen_result.get("results", [])
        elif normalized_market == "us":
            us_limit = min(candidate_limit, 50)
            top_stocks_fn = top_stocks_override if callable(top_stocks_override) else None
            if top_stocks_fn is None:
                top_stocks_fn = top_stocks_fallback
            try:
                top_result = await top_stocks_fn(
                    market="us",
                    ranking_type="volume",
                    limit=us_limit,
                )
                if top_result.get("error"):
                    top_error = str(top_result.get("error")).strip() or "unknown error"
                    logger.warning(
                        "recommend_stocks US get_top_stocks failed: %s", top_error
                    )
                    warnings.append(f"US 후보 수집 실패: {top_error}")
                    raw_candidates = []
                else:
                    raw_candidates = top_result.get("rankings", [])
            except Exception as exc:
                top_error = str(exc).strip() or exc.__class__.__name__
                logger.warning(
                    "recommend_stocks US get_top_stocks exception: %s",
                    top_error,
                    exc_info=True,
                )
                warnings.append(f"US 후보 수집 실패: {top_error}")
                raw_candidates = []
        else:
            screen_result = await screen_crypto_fn(
                market="crypto",
                asset_type=screen_asset_type,
                category=screen_category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                min_dividend_yield=min_dividend_yield,
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=candidate_limit,
            )
            screen_error_raw = screen_result.get("error")
            if screen_error_raw is not None:
                screen_error = str(screen_error_raw).strip() or "unknown error"
                logger.warning(
                    "recommend_stocks crypto screening failed: %s", screen_error
                )
                return {
                    "recommendations": [],
                    "total_amount": 0,
                    "remaining_budget": budget,
                    "strategy": validated_strategy,
                    "strategy_description": get_strategy_description(validated_strategy),
                    "candidates_screened": 0,
                    "diagnostics": diagnostics,
                    "fallback_applied": False,
                    "disclaimer": "투자 권유가 아닙니다. 모든 투자의 책임은 투자자에게 있습니다.",
                    "warnings": [*warnings, f"Crypto 후보 스크리닝 실패: {screen_error}"],
                    "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            raw_candidates = screen_result.get("results", [])
        logger.info(
            "recommend_stocks screening finished market=%s raw_candidates=%d",
            normalized_market,
            len(raw_candidates),
        )
        candidates = [_normalize_candidate(c, normalized_market) for c in raw_candidates]
        if not candidates:
            warnings.append("스크리닝 결과가 없어 추천 가능한 종목이 없습니다.")

        exclude_set: set[str] = set()
        if exclude_symbols:
            manual_excludes = [
                str(symbol).strip().upper()
                for symbol in exclude_symbols
                if symbol is not None and str(symbol).strip()
            ]
            exclude_set.update(manual_excludes)
            logger.debug("recommend_stocks manual exclusions=%d", len(manual_excludes))

        logger.debug("recommend_stocks holdings exclusion lookup start")
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
                warnings.append(f"보유 종목 조회 중 일부 오류: {len(holdings_errors)}건")
            logger.debug(
                "recommend_stocks holdings exclusions=%d total_exclusions=%d",
                holdings_exclusions,
                len(exclude_set),
            )
        except Exception as exc:
            holdings_error = str(exc).strip() or exc.__class__.__name__
            logger.warning(
                "recommend_stocks holdings lookup failed: %s",
                holdings_error,
                exc_info=True,
            )
            warnings.append(f"보유 종목 조회 실패: {holdings_error}")

        filtered_candidates = [
            c for c in candidates if c.get("symbol", "").upper() not in exclude_set
        ]
        if candidates and not filtered_candidates:
            warnings.append("제외 조건 적용 후 추천 가능한 종목이 없습니다.")

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
            "recommend_stocks candidate post-processing normalized=%d filtered=%d deduped=%d",
            len(candidates),
            len(filtered_candidates),
            len(deduped_candidates),
        )

        diagnostics["raw_candidates"] = len(raw_candidates)
        diagnostics["post_filter_candidates"] = len(candidates)
        diagnostics["strict_candidates"] = len(deduped_candidates)
        diagnostics["active_thresholds"] = {
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "min_dividend_yield": min_dividend_yield,
        }
        for c in deduped_candidates:
            if c.get("per") is None:
                diagnostics["per_none_count"] += 1
            if c.get("pbr") is None:
                diagnostics["pbr_none_count"] += 1
            dy = c.get("dividend_yield")
            if dy is None:
                diagnostics["dividend_none_count"] += 1
            elif dy <= 0:
                diagnostics["dividend_zero_count"] += 1

        needs_fallback = (
            validated_strategy in ("value", "dividend")
            and normalized_market == "kr"
            and len(deduped_candidates) < max_positions
        )
        if needs_fallback:
            logger.info(
                "recommend_stocks 2-stage relaxation triggered strategy=%s strict=%d max_positions=%d",
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

            try:
                fallback_result = await screen_kr_fn(
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
                            "recommend_stocks fallback applied strategy=%s added=%d total=%d",
                            validated_strategy,
                            added_count,
                            len(deduped_candidates),
                        )
            except Exception as exc:
                fallback_error = str(exc).strip() or exc.__class__.__name__
                logger.warning(
                    "recommend_stocks fallback screening failed: %s",
                    fallback_error,
                )
                warnings.append(f"Fallback 스크리닝 실패: {fallback_error}")

        rsi_missing_candidates = [
            c for c in deduped_candidates[:20] if c.get("rsi_14") is None
        ]
        if rsi_missing_candidates:
            logger.debug(
                "recommend_stocks rsi enrichment start count=%d",
                len(rsi_missing_candidates),
            )
            rsi_semaphore = asyncio.Semaphore(5)

            async def _fetch_rsi_for_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
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
                                "recommend_stocks RSI fetch failed symbol=%s error=%s",
                                symbol,
                                indicators.get("error"),
                            )
                            return candidate
                        rsi_data = indicators.get("indicators", {}).get("rsi", {})
                        rsi_value = rsi_data.get("14") or rsi_data.get("rsi_14")
                        if rsi_value is not None:
                            candidate["rsi_14"] = _to_optional_float(rsi_value)
                    except Exception as exc:
                        logger.debug(
                            "recommend_stocks RSI fetch exception symbol=%s error=%s",
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
                    if not isinstance(updated, Exception):
                        rsi_missing_candidates[i].update(updated)
            except Exception as exc:
                logger.debug("recommend_stocks RSI batch fetch failed: %s", exc)

        strategy_weights = get_strategy_scoring_weights(validated_strategy)
        logger.debug(
            "recommend_stocks scoring start strategy=%s weights=%s",
            validated_strategy,
            strategy_weights,
        )
        scored_candidates = []
        for item in deduped_candidates:
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
            "recommend_stocks allocation start budget=%.2f candidates=%d max_positions=%d",
            budget,
            len(scored_candidates),
            max_positions,
        )
        allocated, remaining_budget = _allocate_budget(
            scored_candidates, budget, max_positions
        )

        if not allocated and scored_candidates:
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
                    "rsi_14": item.get("rsi_14"),
                    "per": item.get("per"),
                    "change_rate": item.get("change_rate"),
                }
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
            "candidates_screened": len(candidates),
            "diagnostics": diagnostics,
            "fallback_applied": diagnostics.get("fallback_applied", False),
            "disclaimer": "투자 권유가 아닙니다. 모든 투자의 책임은 투자자에게 있습니다.",
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


normalize_recommend_market = _normalize_recommend_market
build_recommend_reason = _build_recommend_reason
normalize_candidate = _normalize_candidate
allocate_budget = _allocate_budget

__all__ = [
    "_normalize_recommend_market",
    "_build_recommend_reason",
    "_normalize_candidate",
    "_allocate_budget",
    "recommend_stocks_impl",
    "normalize_recommend_market",
    "build_recommend_reason",
    "normalize_candidate",
    "allocate_budget",
]
