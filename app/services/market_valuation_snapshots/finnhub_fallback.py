"""ROB-434: US market_valuation Finnhub fallback (field-fill).

When yahoo .info leaves valuation fields null (operator "ROE rows 0") or the
yahoo call fails (crumb/session), backfill the missing valuation fields from
Finnhub's company_basic_financials metric endpoint. Keeps source='yahoo';
records per-field provenance in raw['_field_provenance']. Default-off settings
gate; inert without FINNHUB_API_KEY. Fail-closed: any Finnhub error leaves raw
unchanged (no fabrication).

Service-layer only — does NOT import app.mcp_server (reuses the finnhub_news
client factory). Single consumer is default_valuation_fetcher.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from app.services.market_valuation_snapshots.builder import (
    _FIELD_SOURCE_KEYS,
    _resolve_raw_value,
)

logger = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _map_finnhub_metrics(metric: dict[str, Any]) -> dict[str, Any]:
    """Finnhub company_basic_financials['metric'] → canonical valuation fields.

    Unit traps (the whole reason this is a dedicated, exhaustively-tested fn):
    - roeTTM is ALREADY percent (yahoo returnOnEquity is a fraction ×100) → no ×100.
    - dividendYieldIndicatedAnnual is percent → ÷100 to the stored ratio (guard ≤0.25).
    - marketCapitalization is in MILLIONS → ×1e6 to absolute USD (guard ≥$100M).
    Missing / non-finite / unparseable → field omitted (fail-closed, never fabricated).
    """
    out: dict[str, Any] = {}
    roe = _to_float(metric.get("roeTTM"))
    if roe is not None:
        out["roe"] = roe  # already percent — do NOT ×100
    per = _to_float(metric.get("peTTM"))
    if per is not None:
        out["per"] = per
    pbr = _to_float(metric.get("pbAnnual"))
    if pbr is not None:
        out["pbr"] = pbr
    dividend_yield = _to_float(metric.get("dividendYieldIndicatedAnnual"))
    if dividend_yield is not None:
        out["dividend_yield"] = dividend_yield / 100.0  # percent → ratio
    market_cap_millions = _to_float(metric.get("marketCapitalization"))
    if market_cap_millions is not None:
        out["market_cap"] = market_cap_millions * 1_000_000.0  # millions → absolute
    high_52w = _to_float(metric.get("52WeekHigh"))
    if high_52w is not None:
        out["high_52w"] = high_52w
    low_52w = _to_float(metric.get("52WeekLow"))
    if low_52w is not None:
        out["low_52w"] = low_52w
    high_date = metric.get("52WeekHighDate")
    if isinstance(high_date, str) and high_date.strip():
        # iso string keeps raw_payload JSON-safe; _payload_from_raw parses to date.
        out["high_52w_date"] = high_date.strip()[:10]
    return out


def _finnhub_fallback_enabled() -> bool:
    try:
        from app.core.config import settings
    except Exception:  # noqa: BLE001
        return False
    return bool(getattr(settings, "market_valuation_finnhub_fallback_enabled", False))


def _has_missing_fields(raw: dict[str, Any]) -> bool:
    return any(_resolve_raw_value(raw, field) is None for field in _FIELD_SOURCE_KEYS)


async def fetch_valuation_finnhub(symbol: str) -> dict[str, Any]:
    """Finnhub company_basic_financials metric → canonical valuation dict.

    Raises ImportError (finnhub lib missing) / ValueError (no key) / API errors —
    the caller (apply_valuation_fallback) catches and fail-closes.
    """
    from app.services.finnhub_news import _get_finnhub_client

    client = _get_finnhub_client()

    def _fetch_sync() -> dict[str, Any]:
        data = client.company_basic_financials(symbol.upper(), "all")
        return (data or {}).get("metric", {}) or {}

    metric = await asyncio.to_thread(_fetch_sync)
    return _map_finnhub_metrics(metric)


async def apply_valuation_fallback(
    symbol: str, raw: dict[str, Any], *, yahoo_failed: bool
) -> dict[str, Any]:
    """Backfill missing valuation fields in ``raw`` from Finnhub when gated on.

    No-op unless the settings flag is on AND there is a gap (or yahoo failed).
    Fills only fields ``raw`` lacks; records provenance in raw['_field_provenance'].
    source stays 'yahoo' (caller never changes it). Fail-closed on any Finnhub error.
    """
    if not _finnhub_fallback_enabled():
        return raw
    if not (yahoo_failed or _has_missing_fields(raw)):
        return raw
    try:
        metrics = await fetch_valuation_finnhub(symbol)
    except Exception as exc:  # noqa: BLE001 — no key / lib / API / rate-limit
        logger.warning("finnhub valuation fallback failed symbol=%s: %s", symbol, exc)
        return raw
    filled: list[str] = []
    for field, value in metrics.items():
        if value is None:
            continue
        if _resolve_raw_value(raw, field) is None:
            raw[field] = value
            filled.append(field)
    if filled:
        provenance = raw.setdefault("_field_provenance", {})
        for field in filled:
            provenance[field] = "finnhub"
    return raw

