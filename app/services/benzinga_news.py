"""Benzinga news shadow client (hk 6395).

Read-only comparison harness: polls the Benzinga ``/api/v2/news`` endpoint
and normalizes articles into the same shadow-record shape the CLI writes as
JSONL, so a later ``summary`` pass can compare Benzinga coverage against the
incumbent Finnhub company-news feed over the same window.

Findings measured by the design session on 2026-11-25 (no live call is made
by this worker; these are recorded facts to code against):
- Auth works only with the query-parameter token (``token=...``). The
  ``Authorization: token`` header gives HTTP 401.
- ``GET https://api.benzinga.com/api/v2/news`` returns a JSON list. Item
  fields: ``author, body, channels, created, id, image, importance_rank,
  stocks, tags, teaser, title, updated, url``.
- ``created``/``updated`` are RFC 2822 strings with a ``-0400`` offset even
  in late November.
- ``tickers=NVDA`` returned 0 items although NVDA articles exist under
  ``tickers=AAPL``; unexplained.

Query hypothesis for the ``tickers=NVDA`` anomaly (NOT verified — no live
call from this worker; the design session confirms): the ``tickers`` filter
may match only the primary/first-listed ticker of an article rather than any
mentioned ticker, so a story filed primarily under AAPL with NVDA as a
secondary mention is invisible to ``tickers=NVDA``. The CLI therefore
queries each symbol individually (one request per symbol) instead of joining
symbols into a single ``tickers=A,B`` call, records per-symbol result counts
(including explicit zeros), and never treats "0 items" as "no news" without
the poll evidence. If the per-symbol query also returns 0 for a symbol that
should have coverage, the JSONL still shows the empty poll and the summary
still counts the gap.

Safety:
- Default off: ``BENZINGA_NEWS_SHADOW_ENABLED`` must be exactly ``"true"``.
- The key comes only from env ``BENZINGA_API_KEY``. It is placed into the
  request ``params`` dict inside the request call only, never logged, never
  stored, never written to output. Error text is scrubbed of ``token=...``.
- ``httpx`` with ``follow_redirects=False``.
- No DB, no MCP, no scheduler. Pure fetch + pure parse + pure summary.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

try:
    logging.getLogger("httpcore").setLevel(logging.WARNING)
except Exception:  # pragma: no cover - logging config must never fail
    pass

SHADOW_ENABLED_ENV = "BENZINGA_NEWS_SHADOW_ENABLED"
API_KEY_ENV = "BENZINGA_API_KEY"

BENZINGA_NEWS_URL = "https://api.benzinga.com/api/v2/news"
BENZINGA_NEWS_PATH = "/api/v2/news"

REDACTED = "***"
_TOKEN_PARAM_RE = re.compile(r"token=[^&\s]*")

DEFAULT_TIMEOUT_S = 15.0
DEFAULT_PAGE_SIZE = 50

# The four symbols the Dagu copy polls during US sessions (brief §Desired).
DAGU_SYMBOLS = ("NVDA", "AMD", "QQQ", "SMH")


class BenzingaShadowDisabled(Exception):
    """The shadow gate is off — no network call was made."""


class BenzingaApiKeyMissing(Exception):
    """No usable key in BENZINGA_API_KEY — no network call was made."""


class BenzingaNewsHTTPError(Exception):
    """Non-2xx status from the Benzinga news endpoint (redacted)."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class BenzingaNewsTransportError(Exception):
    """Sanitized network-layer failure (no URL, no key material)."""


def shadow_enabled() -> bool:
    """Exact env gate — only the string ``"true"`` enables the shadow."""
    return os.environ.get(SHADOW_ENABLED_ENV) == "true"


def require_enabled() -> None:
    if not shadow_enabled():
        raise BenzingaShadowDisabled(
            f"{SHADOW_ENABLED_ENV} is not exactly 'true'; refusing to contact Benzinga"
        )


def read_api_key() -> str:
    """Read the key from env only. The value never appears in errors."""
    key = os.environ.get(API_KEY_ENV, "")
    if not key or not key.strip():
        raise BenzingaApiKeyMissing(
            f"{API_KEY_ENV} is not set; refusing to contact Benzinga"
        )
    return key.strip()


def redact_token(text: str) -> str:
    """Scrub ``token=...`` query material from error/log text."""
    return _TOKEN_PARAM_RE.sub(f"token={REDACTED}", text)


def parse_provider_time(raw: Any) -> datetime | None:
    """Parse a Benzinga RFC 2822 timestamp (e.g. ``-0400``) to aware UTC.

    Returns ``None`` for missing/unparseable values — the raw string is
    always preserved by the caller, so nothing is lost.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _names(value: Any) -> list[str]:
    """Benzinga ``stocks``/``channels``/``tags`` entries: str or {name: ...}."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
        elif isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out


def normalize_record(
    item: dict[str, Any],
    *,
    poll_id: str,
    first_received_at: datetime,
) -> dict[str, Any]:
    """Project one raw Benzinga item onto the shadow JSONL record shape."""
    created_raw = item.get("created") if isinstance(item.get("created"), str) else None
    updated_raw = item.get("updated") if isinstance(item.get("updated"), str) else None
    created_utc = parse_provider_time(created_raw)
    updated_utc = parse_provider_time(updated_raw)
    symbols = sorted({s.upper() for s in _names(item.get("stocks")) if s})
    return {
        "source": "benzinga",
        "provider_id": str(item.get("id")),
        "url": item.get("url") if isinstance(item.get("url"), str) else None,
        "title": item.get("title") if isinstance(item.get("title"), str) else None,
        "teaser": item.get("teaser") if isinstance(item.get("teaser"), str) else None,
        "symbols": symbols,
        "channels": _names(item.get("channels")),
        "created_raw": created_raw,
        "updated_raw": updated_raw,
        "created_utc": created_utc.isoformat() if created_utc else None,
        "updated_utc": updated_utc.isoformat() if updated_utc else None,
        "first_received_at": first_received_at.astimezone(UTC).isoformat(),
        "poll_id": poll_id,
    }


def normalize_finnhub_record(
    item: dict[str, Any],
    *,
    poll_id: str,
    first_received_at: datetime,
) -> dict[str, Any]:
    """Project one ``fetch_news_finnhub`` item onto the shadow record shape."""
    return {
        "source": "finnhub",
        "provider_id": item.get("url") or item.get("title"),
        "url": item.get("url"),
        "title": item.get("title"),
        "teaser": item.get("summary"),
        "symbols": [],
        "channels": [item["source"]] if isinstance(item.get("source"), str) else [],
        "created_raw": item.get("datetime"),
        "updated_raw": None,
        "created_utc": item.get("datetime"),
        "updated_utc": None,
        "first_received_at": first_received_at.astimezone(UTC).isoformat(),
        "poll_id": poll_id,
    }


def _new_http_client(timeout_s: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)


async def fetch_benzinga_news(
    ticker: str,
    *,
    api_key: str,
    poll_id: str,
    now: datetime,
    page_size: int = DEFAULT_PAGE_SIZE,
    date_from: str | None = None,
    date_to: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    client_factory: Any = None,
) -> list[dict[str, Any]]:
    """Fetch one ticker's window and return normalized shadow records.

    One ticker per call (the tickers=NVDA anomaly hypothesis): the caller
    loops over symbols and records per-symbol counts, including zeros.
    The ``token`` param is built inside this call only; failures carry
    status + ticker, never the URL or key material.
    """
    symbol = ticker.strip().upper()
    params: dict[str, Any] = {
        "token": api_key,
        "tickers": symbol,
        "displayOutput": "full",
        "pageSize": max(1, page_size),
    }
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to

    factory = client_factory or _new_http_client
    failure: BaseException | None = None
    response: httpx.Response | None = None
    try:
        async with factory(timeout_s) as client:
            response = await client.get(BENZINGA_NEWS_URL, params=params)
    except httpx.TimeoutException as exc:
        failure = BenzingaNewsTransportError(
            f"benzinga news timeout {type(exc).__name__} for {symbol!r}"
        )
    except httpx.TransportError as exc:
        failure = BenzingaNewsTransportError(
            f"benzinga news transport error {type(exc).__name__} for {symbol!r}"
        )
    except httpx.HTTPError as exc:
        failure = BenzingaNewsTransportError(
            redact_token(
                f"benzinga news request error {type(exc).__name__} for {symbol!r}"
            )
        )
    if failure is not None:
        raise failure
    assert response is not None  # failure would have raised above

    status = response.status_code
    if status != 200:
        raise BenzingaNewsHTTPError(
            f"benzinga news HTTP {status} for {symbol!r} (path {BENZINGA_NEWS_PATH})",
            status_code=status,
        )
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise BenzingaNewsHTTPError(
            f"benzinga news non-JSON content-type (HTTP {status}) for {symbol!r} "
            f"(path {BENZINGA_NEWS_PATH})",
            status_code=status,
        )
    try:
        payload = response.json()
    except Exception:
        raise BenzingaNewsHTTPError(
            f"benzinga news non-JSON body for {symbol!r} (path {BENZINGA_NEWS_PATH})",
            status_code=status,
        ) from None
    if not isinstance(payload, list):
        raise BenzingaNewsHTTPError(
            f"benzinga news non-list JSON for {symbol!r} (path {BENZINGA_NEWS_PATH})",
            status_code=status,
        ) from None
    for row_index, row in enumerate(payload):
        if not isinstance(row, dict) or "error" in row:
            raise BenzingaNewsHTTPError(
                f"benzinga news row {row_index} carries an error payload "
                f"(HTTP {status}) for {symbol!r}",
                status_code=status,
            ) from None
    return [
        normalize_record(row, poll_id=poll_id, first_received_at=now) for row in payload
    ]


def normalize_url(url: Any) -> str | None:
    """Canonical URL key: lowercase host+path, no scheme/query/fragment."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("", parts.netloc.lower(), path, "", ""))


def normalize_title(title: Any) -> str | None:
    """Canonical title key: lowercase, collapsed whitespace."""
    if not isinstance(title, str) or not title.strip():
        return None
    return re.sub(r"\s+", " ", title.strip().lower())


def _match_key(record: dict[str, Any]) -> str | None:
    return normalize_url(record.get("url")) or (
        f"title:{normalize_title(record.get('title'))}"
        if normalize_title(record.get("title"))
        else None
    )


def summarize_shadow(
    benzinga_records: list[dict[str, Any]],
    finnhub_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two shadow streams over the same window (pure function).

    - Overlap: same article in both sources, by normalized URL then title.
    - Unique: per-source items with no counterpart.
    - Latency: ``first_received_at`` difference in seconds, only for matched
      pairs observed in the same poll window (same ``poll_id``); pairs from
      different polls report no latency (window policy: the benchmark judges
      latency only where both sources were observed at the same time).
    - Duplicate rate: per source, share of records beyond the first per key.
    """
    ben_keys: dict[str, list[int]] = {}
    for i, record in enumerate(benzinga_records):
        key = _match_key(record)
        if key is not None:
            ben_keys.setdefault(key, []).append(i)
    fin_keys: dict[str, list[int]] = {}
    for i, record in enumerate(finnhub_records):
        key = _match_key(record)
        if key is not None:
            fin_keys.setdefault(key, []).append(i)

    overlap_keys = sorted(set(ben_keys) & set(fin_keys))
    latencies: list[float] = []
    latency_pairs = 0
    for key in overlap_keys:
        ben_first = benzinga_records[ben_keys[key][0]]
        fin_first = finnhub_records[fin_keys[key][0]]
        if ben_first.get("poll_id") != fin_first.get("poll_id"):
            continue
        try:
            ben_seen = datetime.fromisoformat(str(ben_first["first_received_at"]))
            fin_seen = datetime.fromisoformat(str(fin_first["first_received_at"]))
        except (ValueError, TypeError, KeyError):
            continue
        latency_pairs += 1
        latencies.append((ben_seen - fin_seen).total_seconds())

    def duplicate_rate(
        records: list[dict[str, Any]], keys: dict[str, list[int]]
    ) -> float | None:
        if not records:
            return None
        extra = sum(len(v) - 1 for v in keys.values())
        return extra / len(records)

    ben_overlap = sum(len(ben_keys[k]) for k in overlap_keys)
    fin_overlap = sum(len(fin_keys[k]) for k in overlap_keys)
    lat_sorted = sorted(latencies)
    return {
        "benzinga_count": len(benzinga_records),
        "finnhub_count": len(finnhub_records),
        "overlap_articles": len(overlap_keys),
        "benzinga_unique": len(benzinga_records) - ben_overlap,
        "finnhub_unique": len(finnhub_records) - fin_overlap,
        "latency_pairs_same_poll": latency_pairs,
        "latency_seconds": {
            "min": lat_sorted[0] if lat_sorted else None,
            "p50": lat_sorted[len(lat_sorted) // 2] if lat_sorted else None,
            "max": lat_sorted[-1] if lat_sorted else None,
        },
        "benzinga_duplicate_rate": duplicate_rate(benzinga_records, ben_keys),
        "finnhub_duplicate_rate": duplicate_rate(finnhub_records, fin_keys),
    }


__all__ = [
    "API_KEY_ENV",
    "DAGU_SYMBOLS",
    "SHADOW_ENABLED_ENV",
    "BenzingaApiKeyMissing",
    "BenzingaNewsHTTPError",
    "BenzingaNewsTransportError",
    "BenzingaShadowDisabled",
    "fetch_benzinga_news",
    "normalize_finnhub_record",
    "normalize_record",
    "normalize_title",
    "normalize_url",
    "parse_provider_time",
    "read_api_key",
    "redact_token",
    "require_enabled",
    "shadow_enabled",
    "summarize_shadow",
]
