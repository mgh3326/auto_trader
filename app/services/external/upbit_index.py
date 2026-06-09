"""Upbit digital-asset-index / altseason data source (ROB-381 PR2).

Read-only public market-data. Two data planes, deliberately split by robots policy
(see ``docs/runbooks/rob-381-upbit-index-altseason-recon.md``):

- **Indices** come from ``datalab-static.upbit.com`` — an S3-hosted public data
  product with no robots restriction. We merge index/master (catalog) +
  index/recent (live value) + index/summary (yield/risk stats).
- **24h breadth** (alts beating BTC) is derived from the **official** Open API
  ``api.upbit.com/v1/ticker`` (``signed_change_rate`` is 24h only). We do NOT use
  the robots-disallowed ``crix-api-cdn`` trends endpoints.

7/30/90d breadth is intentionally out of scope here (official ``/v1/ticker`` has no
multi-period change rate); that is a separate follow-up over ``/v1/candles/days``.

Fail-open: every fetch returns ``None`` (or a partial dict) on failure rather than
raising, so report generation is never blocked. No broker/order/account API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

# datalab-static: robots-clean S3 public data product.
_DATALAB_BASE = "https://datalab-static.upbit.com/platform/v1/index"
INDEX_MASTER_URL = f"{_DATALAB_BASE}/master"
INDEX_RECENT_URL = f"{_DATALAB_BASE}/recent"
INDEX_SUMMARY_URL = f"{_DATALAB_BASE}/summary"

# Official Open API (documented, robots-allowed for these data endpoints).
_OPEN_API_BASE = "https://api.upbit.com/v1"
MARKET_ALL_URL = f"{_OPEN_API_BASE}/market/all"
TICKER_URL = f"{_OPEN_API_BASE}/ticker"

# Composite/market index codes used for the altseason ratio.
ALTCOIN_INDEX_CODE = "IDX.UPBIT.UBAI"  # Upbit Altcoin Index
MARKET_INDEX_CODE = "IDX.UPBIT.UBMI"  # Upbit Market Index

_INDEX_TTL = timedelta(seconds=60)  # datalab responses carry max-age=60
_ALTSEASON_TTL = timedelta(minutes=5)
_HTTP_TIMEOUT = 10.0

_indices_cache: dict[str, Any] | None = None
_indices_cache_expires: datetime | None = None
_altseason_cache: dict[str, Any] | None = None
_altseason_cache_expires: datetime | None = None
_cache_lock = asyncio.Lock()


def _clear_caches() -> None:
    """Clear in-memory caches (for tests)."""
    global _indices_cache, _indices_cache_expires
    global _altseason_cache, _altseason_cache_expires
    _indices_cache = None
    _indices_cache_expires = None
    _altseason_cache = None
    _altseason_cache_expires = None


async def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


def _normalize_index_row(
    master: dict[str, Any],
    recent: dict[str, Any] | None,
    summary_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "code": master.get("code"),
        "symbol": master.get("symbol"),
        "korean_name": master.get("koreanName"),
        "category_type": master.get("categoryType"),
        "index_type": master.get("indexType"),
        "value": None,
        "signed_change_rate_24h": None,
    }
    if recent:
        row["value"] = recent.get("tradePrice")
        row["signed_change_rate_24h"] = recent.get("signedChangeRate")
        row["as_of"] = recent.get("candleDateTime")
    if summary_stats:
        # Pass through the regime-relevant stats only (not the whole blob).
        for key in (
            "dailyYield",
            "weeklyYield",
            "monthlyYield",
            "quarterlyYield",
            "yearlyYield",
            "winningRate",
            "volatility",
            "beta",
            "sharpeRatio",
        ):
            if key in summary_stats:
                row[key] = summary_stats[key]
    return row


async def fetch_upbit_indices() -> dict[str, Any] | None:
    """Fetch + merge Upbit digital-asset indices from datalab-static.

    Returns ``{"source", "as_of", "provenance", "indices": {code: {...}}}`` or
    ``None`` if the catalog fetch fails. Recent/summary are best-effort overlays.
    """
    global _indices_cache, _indices_cache_expires

    async with _cache_lock:
        if (
            _indices_cache is not None
            and _indices_cache_expires
            and now_kst() < _indices_cache_expires
        ):
            return _indices_cache.copy()

    try:
        master = await _get_json(INDEX_MASTER_URL)
    except Exception as exc:
        logger.warning("Failed to fetch Upbit index master: %s", exc)
        return None
    if not isinstance(master, list) or not master:
        logger.warning("Unexpected Upbit index master payload")
        return None

    # Recent + summary are overlays — fail-open to {} so the catalog still returns.
    try:
        recent_rows = await _get_json(INDEX_RECENT_URL)
    except Exception as exc:
        logger.warning("Failed to fetch Upbit index recent: %s", exc)
        recent_rows = []
    try:
        summary_rows = await _get_json(INDEX_SUMMARY_URL)
    except Exception as exc:
        logger.warning("Failed to fetch Upbit index summary: %s", exc)
        summary_rows = []

    recent_by_code = {
        r.get("code"): r for r in recent_rows if isinstance(r, dict) and r.get("code")
    }
    summary_by_code = {
        s.get("code"): (s.get("stats") or {})
        for s in summary_rows
        if isinstance(s, dict) and s.get("code")
    }

    indices: dict[str, Any] = {}
    for m in master:
        if not isinstance(m, dict) or not m.get("code"):
            continue
        code = m["code"]
        indices[code] = _normalize_index_row(
            m, recent_by_code.get(code), summary_by_code.get(code)
        )

    result = {
        "source": "upbit_datalab",
        "provenance": "unofficial_web_endpoint",
        "as_of": now_kst().isoformat(),
        "indices": indices,
    }

    async with _cache_lock:
        _indices_cache = result.copy()
        _indices_cache_expires = now_kst() + _INDEX_TTL
    return result.copy()


async def _fetch_krw_breadth_24h() -> dict[str, Any] | None:
    """24h alt-vs-BTC breadth from the official Open API ticker.

    breadth = fraction of KRW-quoted non-BTC markets whose 24h change exceeds
    KRW-BTC's 24h change. ``None`` if the official data is unavailable.
    """
    try:
        markets = await _get_json(MARKET_ALL_URL)
        krw = [
            m["market"]
            for m in markets
            if isinstance(m, dict) and str(m.get("market", "")).startswith("KRW-")
        ]
        if "KRW-BTC" not in krw:
            return None
        tickers = await _get_json(TICKER_URL, params={"markets": ",".join(krw)})
    except Exception as exc:
        logger.warning("Failed to fetch Upbit KRW breadth: %s", exc)
        return None

    rate_by_market = {
        t["market"]: t.get("signed_change_rate")
        for t in tickers
        if isinstance(t, dict) and t.get("market")
    }
    btc_rate = rate_by_market.get("KRW-BTC")
    if btc_rate is None:
        return None

    alt_rates = [
        rate
        for market, rate in rate_by_market.items()
        if market != "KRW-BTC" and rate is not None
    ]
    if not alt_rates:
        return None

    beating = sum(1 for rate in alt_rates if rate > btc_rate)
    return {
        "window": "24h",
        "method": "open_api_ticker_24h_derived",
        "alts_total": len(alt_rates),
        "alts_beating_btc": beating,
        "alts_beating_btc_pct": round(beating / len(alt_rates), 4),
        "btc_change_24h": btc_rate,
    }


async def fetch_upbit_altseason() -> dict[str, Any] | None:
    """Altseason snapshot: UBAI/UBMI ratio + 24h alt-vs-BTC breadth.

    Returns a partial dict if one plane is unavailable; ``None`` only if both the
    index ratio and the breadth are unavailable.
    """
    global _altseason_cache, _altseason_cache_expires

    async with _cache_lock:
        if (
            _altseason_cache is not None
            and _altseason_cache_expires
            and now_kst() < _altseason_cache_expires
        ):
            return _altseason_cache.copy()

    indices_payload = await fetch_upbit_indices()
    ratio: float | None = None
    if indices_payload:
        idx = indices_payload.get("indices", {})
        ubai = (idx.get(ALTCOIN_INDEX_CODE) or {}).get("value")
        ubmi = (idx.get(MARKET_INDEX_CODE) or {}).get("value")
        if ubai is not None and ubmi:
            ratio = round(float(ubai) / float(ubmi), 6)

    breadth = await _fetch_krw_breadth_24h()

    if ratio is None and breadth is None:
        return None

    result = {
        "source": "upbit_datalab+upbit_open_api",
        "provenance": "unofficial_web_endpoint+official_open_api",
        "as_of": now_kst().isoformat(),
        "ubai_ubmi_ratio": ratio,
        "breadth": breadth,
    }

    async with _cache_lock:
        _altseason_cache = result.copy()
        _altseason_cache_expires = now_kst() + _ALTSEASON_TTL
    return result.copy()
