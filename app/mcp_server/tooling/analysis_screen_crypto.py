from __future__ import annotations

import datetime
import math
from typing import Any, TypedDict

type CryptoCandidate = dict[str, Any]
type CryptoFiltersApplied = dict[str, Any]
type CryptoScreenMeta = dict[str, Any]
type CryptoScreenResponse = dict[str, Any]


class RsiEnrichmentDiagnostics(TypedDict):
    attempted: int
    succeeded: int
    failed: int
    rate_limited: int
    timeout: int
    error_samples: list[str]


class CoinGeckoMarketCapData(TypedDict, total=False):
    market_cap: float | None
    market_cap_rank: int | None


class CoinGeckoPayload(TypedDict, total=False):
    data: dict[str, CoinGeckoMarketCapData]
    cached: bool
    age_seconds: float | None
    stale: bool
    error: str | None


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _extract_market_symbol(symbol: Any) -> str | None:
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if "-" in text:
        token = text.split("-", maxsplit=1)[1].strip()
        return token or None
    return text


def _compute_rsi_bucket(rsi: Any) -> int:
    rsi_value = _to_optional_float(rsi)
    if rsi_value is None:
        return 999
    return int(rsi_value // 5) * 5


def _sort_crypto_by_rsi_bucket(
    items: list[CryptoCandidate],
) -> list[CryptoCandidate]:
    return sorted(
        items,
        key=lambda item: (
            int(item.get("rsi_bucket", 999)),
            -float(item.get("trade_amount_24h") or 0.0),
        ),
    )


def _sort_and_limit(
    results: list[CryptoCandidate],
    sort_by: str,
    sort_order: str,
    limit: int,
) -> list[CryptoCandidate]:
    if not results:
        return []

    sort_field_map = {
        "volume": "volume",
        "trade_amount": "trade_amount_24h",
        "market_cap": "market_cap",
        "change_rate": "change_rate",
        "dividend_yield": "dividend_yield",
        "rsi": "rsi",
        "score": "score",
    }
    field = sort_field_map.get(sort_by, "volume")
    reverse = sort_order == "desc"

    def sort_value(item: CryptoCandidate) -> float:
        value = item.get(field)
        if field in {"rsi", "score"} and value is None:
            return -999.0 if reverse else 999.0
        return float(value or 0)

    results.sort(key=sort_value, reverse=reverse)
    return results[:limit]


def _build_screen_response(
    results: list[CryptoCandidate],
    total_count: int,
    filters_applied: CryptoFiltersApplied,
    market: str,
    rsi_enrichment: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    meta_fields: CryptoScreenMeta | None = None,
) -> CryptoScreenResponse:
    diagnostics: dict[str, Any] = {
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "rate_limited": 0,
        "timeout": 0,
        "error_samples": [],
    }
    if rsi_enrichment:
        diagnostics.update(
            {
                "attempted": int(rsi_enrichment.get("attempted", 0) or 0),
                "succeeded": int(rsi_enrichment.get("succeeded", 0) or 0),
                "failed": int(rsi_enrichment.get("failed", 0) or 0),
                "rate_limited": int(rsi_enrichment.get("rate_limited", 0) or 0),
                "timeout": int(rsi_enrichment.get("timeout", 0) or 0),
                "error_samples": [
                    str(message)[:100]
                    for message in (rsi_enrichment.get("error_samples") or [])[:3]
                ],
            }
        )

    response_meta: CryptoScreenMeta = {"rsi_enrichment": diagnostics}
    if meta_fields:
        response_meta.update(meta_fields)

    response: CryptoScreenResponse = {
        "results": results,
        "total_count": total_count,
        "returned_count": len(results),
        "filters_applied": filters_applied,
        "market": market,
        "meta": response_meta,
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    if warnings:
        response["warnings"] = warnings
    return response


def _append_rsi_enrichment_warnings(
    warnings: list[str], rsi_enrichment: dict[str, Any]
) -> None:
    timeout_count = int(rsi_enrichment.get("timeout", 0) or 0)
    if timeout_count > 0:
        warnings.append(
            f"Crypto RSI enrichment timed out for {timeout_count} symbols; partial results returned"
        )

    rate_limited_count = int(rsi_enrichment.get("rate_limited", 0) or 0)
    if rate_limited_count > 0:
        warnings.append(
            "Crypto RSI enrichment hit rate limits for "
            f"{rate_limited_count} symbols; partial results returned"
        )


def _merge_coingecko_market_caps(
    candidates: list[CryptoCandidate], coingecko_payload: dict[str, Any]
) -> None:
    coingecko_data = coingecko_payload.get("data") or {}
    for item in candidates:
        symbol = _extract_market_symbol(
            item.get("symbol") or item.get("original_market")
        )
        cap_data = coingecko_data.get(symbol or "") if symbol else None
        if cap_data:
            if cap_data.get("market_cap") is not None:
                item["market_cap"] = cap_data.get("market_cap")
            item["market_cap_rank"] = cap_data.get("market_cap_rank")
        item["market_warning"] = None
        item["rsi_bucket"] = _compute_rsi_bucket(item.get("rsi"))
        item.pop("score", None)


def _append_coingecko_warning(
    warnings: list[str], coingecko_payload: dict[str, Any]
) -> None:
    coingecko_error = coingecko_payload.get("error")
    if not coingecko_error:
        return
    if coingecko_payload.get("stale"):
        warnings.append("CoinGecko market-cap refresh failed; stale cache was used.")
        return
    warnings.append(
        "CoinGecko market-cap data unavailable; market_cap fields remain null."
    )


def _apply_max_rsi_filter(
    candidates: list[CryptoCandidate], max_rsi: float | None
) -> list[CryptoCandidate]:
    if max_rsi is None:
        return candidates
    filtered: list[CryptoCandidate] = []
    for item in candidates:
        rsi_value = _to_optional_float(item.get("rsi"))
        if rsi_value is not None and rsi_value <= max_rsi:
            filtered.append(item)
    return filtered


def _sort_crypto_candidates(
    candidates: list[CryptoCandidate],
    sort_by: str,
    sort_order: str,
    warnings: list[str],
) -> tuple[list[CryptoCandidate], str]:
    applied_sort_order = sort_order
    if sort_by == "rsi":
        if sort_order == "desc":
            warnings.append(
                "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
            )
        applied_sort_order = "asc"
        return _sort_crypto_by_rsi_bucket(candidates), applied_sort_order
    return _sort_and_limit(
        candidates, sort_by, sort_order, len(candidates)
    ), applied_sort_order


async def finalize_crypto_screen(
    candidates: list[CryptoCandidate],
    filters_applied: CryptoFiltersApplied,
    market: str,
    limit: int,
    max_rsi: float | None,
    warnings: list[str],
    rsi_enrichment: dict[str, Any],
    coingecko_payload: dict[str, Any],
    total_markets: int,
    top_by_volume: int,
    filtered_by_warning: int,
    filtered_by_crash: int,
    source: str | None = None,
) -> CryptoScreenResponse:
    _append_rsi_enrichment_warnings(warnings, rsi_enrichment)
    _merge_coingecko_market_caps(candidates, coingecko_payload)
    _append_coingecko_warning(warnings, coingecko_payload)

    filtered = _apply_max_rsi_filter(candidates, max_rsi)
    sort_by = str(filters_applied.get("sort_by") or "trade_amount")
    sort_order = str(filters_applied.get("sort_order") or "desc")
    ordered, applied_sort_order = _sort_crypto_candidates(
        filtered, sort_by, sort_order, warnings
    )

    results = ordered[:limit]
    for item in results:
        item.pop("score", None)

    filters_applied["sort_order"] = applied_sort_order
    meta_fields: CryptoScreenMeta = {
        "total_markets": total_markets,
        "top_by_volume": top_by_volume,
        "filtered_by_warning": filtered_by_warning,
        "filtered_by_crash": filtered_by_crash,
        "rsi_enriched": int(rsi_enrichment.get("succeeded", 0) or 0),
        "final_count": len(results),
        "coingecko_cached": bool(coingecko_payload.get("cached")),
        "coingecko_age_seconds": coingecko_payload.get("age_seconds"),
    }
    if source is not None:
        meta_fields["source"] = source

    rsi_enrichment_dict = dict(rsi_enrichment)
    return _build_screen_response(
        results,
        len(filtered),
        filters_applied,
        market,
        rsi_enrichment=rsi_enrichment_dict,
        warnings=warnings if warnings else None,
        meta_fields=meta_fields,
    )


__all__ = [
    "CoinGeckoPayload",
    "CryptoCandidate",
    "CryptoFiltersApplied",
    "CryptoScreenResponse",
    "RsiEnrichmentDiagnostics",
    "finalize_crypto_screen",
]
