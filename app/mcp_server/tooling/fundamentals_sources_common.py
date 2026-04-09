"""Shared utility helpers for fundamentals providers."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Screen enrichment defaults
# ---------------------------------------------------------------------------

_SCREEN_ENRICHMENT_DEFAULTS: dict[str, Any] = {
    "sector": None,
    "analyst_buy": 0,
    "analyst_hold": 0,
    "analyst_sell": 0,
    "avg_target": None,
    "upside_pct": None,
}


# ---------------------------------------------------------------------------
# Parse / coerce helpers
# ---------------------------------------------------------------------------


def _parse_naver_num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


def _coerce_optional_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return value
    return None


# ---------------------------------------------------------------------------
# Screen enrichment payload builders
# ---------------------------------------------------------------------------


def _build_screen_enrichment_payload(
    *,
    sector: Any,
    consensus: Any,
) -> dict[str, Any]:
    normalized_sector = str(sector).strip() if sector is not None else ""
    consensus_map = consensus if isinstance(consensus, dict) else {}
    payload = dict(_SCREEN_ENRICHMENT_DEFAULTS)
    payload["sector"] = normalized_sector or None
    payload["analyst_buy"] = _parse_naver_int(consensus_map.get("buy_count")) or 0
    payload["analyst_hold"] = _parse_naver_int(consensus_map.get("hold_count")) or 0
    payload["analyst_sell"] = _parse_naver_int(consensus_map.get("sell_count")) or 0
    payload["avg_target"] = _coerce_optional_number(
        consensus_map.get("avg_target_price")
    )
    payload["upside_pct"] = _coerce_optional_number(consensus_map.get("upside_pct"))
    return payload


async def _fetch_screen_enrichment_payload(
    *,
    symbol: str,
    profile_request: Any,
    opinions_request: Any,
    profile_provider: str,
    opinions_provider: str,
) -> dict[str, Any]:
    profile_result, opinions_result = await asyncio.gather(
        profile_request,
        opinions_request,
        return_exceptions=True,
    )

    profile_error = profile_result if isinstance(profile_result, Exception) else None
    opinions_error = opinions_result if isinstance(opinions_result, Exception) else None

    if profile_error is not None and opinions_error is not None:
        raise profile_error from opinions_error

    if profile_error is not None:
        logger.warning(
            "Screen enrichment profile provider failed for %s (%s): %s: %s",
            symbol,
            profile_provider,
            type(profile_error).__name__,
            profile_error,
        )

    if opinions_error is not None:
        logger.warning(
            "Screen enrichment opinions provider failed for %s (%s): %s: %s",
            symbol,
            opinions_provider,
            type(opinions_error).__name__,
            opinions_error,
        )

    profile = profile_result if isinstance(profile_result, dict) else None
    opinions = opinions_result if isinstance(opinions_result, dict) else None
    return _build_screen_enrichment_payload(
        sector=(profile or {}).get("sector"),
        consensus=(opinions or {}).get("consensus"),
    )


__all__ = [
    "_build_screen_enrichment_payload",
    "_coerce_optional_number",
    "_fetch_screen_enrichment_payload",
    "_parse_naver_int",
    "_parse_naver_num",
    "_SCREEN_ENRICHMENT_DEFAULTS",
]
