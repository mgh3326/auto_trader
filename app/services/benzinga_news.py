"""Benzinga news shadow client (hk 6395).

Read-only comparison harness: polls the Benzinga ``/api/v2/news`` endpoint
and normalizes articles into the same shadow-record shape the CLI writes as
JSONL, so a later ``summary`` pass can compare Benzinga coverage against the
incumbent Finnhub company-news feed over the same window.

Findings measured by the design session on 2026-11-25 (no live call is made
by this worker; these are recorded facts to code against):
Auth works only with the query-parameter token (``token=...``). The
``Authorization: token`` header gives HTTP 401.
``GET https://api.benzinga.com/api/v2/news`` returns a JSON list. Item
fields: ``author, body, channels, created, id, image, importance_rank,
stocks, tags, teaser, title, updated, url``.
``created``/``updated`` are RFC 2822 strings with a ``-0400`` offset even
in late November.
Confirmed 2026-11-25 (design-session live check): ``tickers=NVDA`` returned
0 items although NVDA articles exist; ``primaryTickers=NVDA`` returned the
NVDA articles. The ``tickers`` filter matches only the primary
(first-listed) ticker of an article, so a story filed primarily under
another ticker with NVDA as a secondary mention is invisible to
``tickers=NVDA``.

The CLI therefore queries ``primaryTickers=<symbol>`` per symbol for
coverage, and additionally issues ``tickers=<symbol>`` per symbol as
evidence. Each symbol is queried individually (never joined into one
``primaryTickers=A,B`` call), every article and poll_status row carries its
request parameter name (``query_param``), and explicit zeros stay visible —
0 items is a gap data point, never silence.

Safety:
- Default off: ``BENZINGA_NEWS_SHADOW_ENABLED`` must be exactly ``"true"``.
- The key comes only from env ``BENZINGA_API_KEY``. It is placed into the
  request ``params`` dict inside the request call only, never logged, never
  stored, never written to output. Error text is scrubbed of ``token=...``.
- ``httpx`` with ``follow_redirects=False``.
- No DB, no MCP, no scheduler. Pure fetch + pure parse + pure summary.

Record semantics:
- ``normalize_url`` keeps identifying query parameters and strips only
  tracking ones (``utm_*`` + a closed list). Finnhub company-news URLs are
  normally ``https://finnhub.io/api/news?id=<hash>`` where the query is the
  only article identifier.
- Matching is two-phase: first by normalized URL, then the still-unmatched
  records by normalized title. Records are deduplicated by
  (source, provider_id) first, keeping the earliest sighting.
- ``summary`` reads per-symbol per-source ``poll_status`` records: a cell
  where either source errored is reported and excluded from coverage counts,
  so a failed fetch is never counted as "no news".
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

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
# Without an explicit Accept header Benzinga answers in XML (observed 2026-11-25
# on the pi: every poll failed with a non-JSON content type).
BENZINGA_REQUEST_HEADERS = {"Accept": "application/json"}

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


def _provider_offset(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, OverflowError):
        return None
    offset = parsed.strftime("%z")
    return f"{offset[:3]}:{offset[3:]}" if offset else None


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
    raw_id = item.get("id")
    provider_id = str(raw_id) if raw_id is not None else None
    return {
        "record_type": "article",
        "source": "benzinga",
        "provider_id": provider_id,
        "url": item.get("url") if isinstance(item.get("url"), str) else None,
        "title": item.get("title") if isinstance(item.get("title"), str) else None,
        "teaser": item.get("teaser") if isinstance(item.get("teaser"), str) else None,
        "symbols": symbols,
        "query_symbol": None,
        "channels": _names(item.get("channels")),
        "created_raw": created_raw,
        "published_at_raw": created_raw,
        "published_at": created_utc.isoformat() if created_utc else None,
        "published_offset": _provider_offset(created_raw),
        "published_precision": "second" if created_utc else None,
        "revised_at": None,
        "updated_raw": updated_raw,
        "created_basis": "provider_rfc2822" if created_utc else None,
        "updated_basis": "provider_rfc2822" if updated_utc else None,
        "created_utc": created_utc.isoformat() if created_utc else None,
        "updated_utc": updated_utc.isoformat() if updated_utc else None,
        "first_received_at": first_received_at.astimezone(UTC).isoformat(),
        "poll_id": poll_id,
    }


def _finnhub_created_utc(raw: Any) -> tuple[str | None, str | None]:
    """Interpret a ``fetch_news_finnhub`` datetime as UTC.

    The provider value is a naive host-local string
    (``datetime.fromtimestamp(ts).isoformat()``), so a manual run on a KST
    host would otherwise record values 9 hours off labelled UTC. A naive
    value is read as host-local time *at its own instant* and converted;
    an aware value is converted as-is. Returns ``(utc_iso, basis)``.

    The naive branch must not pin ``datetime.now()``'s offset: across a DST
    switch inside the window that is off by one hour. Calling
    ``astimezone(UTC)`` on the naive value lets the host zone rules supply
    the offset in effect at the record instant.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone(UTC)
        basis = "finnhub_naive_interpreted_as_local"
    else:
        basis = "provider_offset_aware"
    return parsed.astimezone(UTC).isoformat(), basis


def normalize_finnhub_record(
    item: dict[str, Any],
    *,
    poll_id: str,
    first_received_at: datetime,
) -> dict[str, Any]:
    """Project one ``fetch_news_finnhub`` item onto the shadow record shape."""
    created_raw = (
        item.get("datetime_raw")
        if isinstance(item.get("datetime_raw"), str)
        else item.get("datetime")
        if isinstance(item.get("datetime"), str)
        else None
    )
    created_utc, created_basis = _finnhub_created_utc(item.get("datetime"))
    provider_id = item.get("id")
    return {
        "record_type": "article",
        "source": "finnhub",
        "provider_id": str(provider_id)
        if provider_id is not None
        else item.get("url") or item.get("title"),
        "url": item.get("url"),
        "title": item.get("title"),
        "teaser": item.get("summary"),
        "symbols": [],
        "query_symbol": None,
        "channels": [item["source"]] if isinstance(item.get("source"), str) else [],
        "created_raw": created_raw,
        "published_at_raw": created_raw,
        "published_at": created_utc,
        "published_offset": "+00:00" if created_utc else None,
        "published_precision": "second" if created_utc else None,
        "revised_at": None,
        "updated_raw": None,
        "created_basis": created_basis,
        "updated_basis": None,
        "created_utc": created_utc,
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
    ticker_param: str = "primaryTickers",
) -> list[dict[str, Any]]:
    """Fetch one ticker's window and return normalized shadow records.

    One ticker per call, one request parameter per call: the caller loops
    over symbols and over ``("primaryTickers", "tickers")``, recording
    per-symbol per-parameter counts, including zeros. ``primaryTickers``
    is the coverage query (confirmed 2026-11-25: ``tickers=NVDA`` returns
    0 items while ``primaryTickers=NVDA`` returns the NVDA articles);
    ``tickers`` is evidence for the comparison. The ``token`` param is
    built inside this call only; failures carry status + ticker, never
    the URL or key material.
    """
    symbol = ticker.strip().upper()
    params: dict[str, Any] = {
        "token": api_key,
        ticker_param: symbol,
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
            response = await client.get(
                BENZINGA_NEWS_URL, params=params, headers=BENZINGA_REQUEST_HEADERS
            )
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


# Query parameters that never identify an article — stripped from the
# canonical URL key. Everything else in the query is kept: Finnhub
# company-news URLs are normally ``https://finnhub.io/api/news?id=<hash>``
# where the query is the only identifier.
_TRACKING_QUERY_PARAMS = frozenset(
    {
        "utm",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "vero_id",
        "_hsenc",
        "_hsmi",
        "srsltid",
        "wickedsource",
        "wickedid",
    }
)


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_QUERY_PARAMS


def normalize_url(url: Any) -> str | None:
    """Canonical URL key: lowercase host+path, identifying query kept.

    Scheme, fragment, and tracking parameters are dropped; remaining query
    parameters are sorted for a canonical order.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    try:
        pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k and not _is_tracking_param(k)
        ]
    except ValueError:
        return None
    pairs.sort()
    key = f"{parts.netloc.lower()}{path}"
    if pairs:
        key += f"?{urlencode(pairs)}"
    return key


def normalize_title(title: Any) -> str | None:
    """Canonical title key: lowercase, collapsed whitespace."""
    if not isinstance(title, str) or not title.strip():
        return None
    return re.sub(r"\s+", " ", title.strip().lower())


def _seen_at(record: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(record.get("first_received_at")))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _dedupe_articles(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Dedupe by (source, provider_id), keeping the earliest sighting.

    The CLI queries each symbol separately, so one story tagged NVDA and AMD
    is written twice; each poll also writes its own file. Counting those as
    separate unique items would inflate coverage. Returns (kept, removed).

    Records with an empty or missing ``provider_id`` keep their own
    identity: merging them onto one shared key would silently drop
    distinct stories.
    """
    best: dict[tuple[Any, Any], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    removed = 0
    for record in records:
        provider_id = record.get("provider_id")
        if provider_id is None or (
            isinstance(provider_id, str) and not provider_id.strip()
        ):
            unkeyed.append(record)
            continue
        key = (record.get("source"), provider_id)
        seen = _seen_at(record)
        prior = best.get(key)
        if prior is None:
            best[key] = record
            continue
        removed += 1
        prior_seen = _seen_at(prior)
        if seen is not None and (prior_seen is None or seen < prior_seen):
            best[key] = record
    return list(best.values()) + unkeyed, removed


def _cell(record: dict[str, Any]) -> tuple[Any, Any]:
    return (record.get("poll_id"), record.get("query_symbol"))


def summarize_shadow(
    benzinga_records: list[dict[str, Any]],
    finnhub_records: list[dict[str, Any]],
    poll_frames: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Compare two shadow streams over the same window (pure function).

    Inputs may mix ``article`` rows with ``poll_status`` rows (anything whose
    ``record_type`` is not ``"article"`` is a status row). A (poll_id, symbol)
    cell where either source reports ``status="error"`` is unreliable: its
    articles are excluded from coverage counts and reported separately, so a
    failed fetch is never counted as "no news".

    ``poll_frames`` carries the per-poll ``poll_header`` / ``poll_completion``
    records. A poll with a header but no completion record was killed or
    timed out mid-pass: every planned symbol cell is unreliable. A completed
    poll whose completion lists fewer symbols than the header planned left
    symbols unpolled: those cells are unreliable too. Both are reported in
    ``incomplete_polls`` so a partial collection never reads as a gap.

    - Dedupe: by (source, provider_id), keeping the earliest sighting.
    - Overlap: same article in both sources — first by normalized URL, then
      the still-unmatched records by normalized title, paired greedily in
      sighting order.
    - Unique: per-source articles with no counterpart.
    - Latency: ``first_received_at`` difference in seconds, only for matched
      pairs observed in the same poll window (same ``poll_id``); pairs from
      different polls report no latency (window policy: the benchmark judges
      latency only where both sources were observed at the same time).
    - Duplicate rate: per source, share of removed duplicates over articles.
    """
    statuses = [
        r
        for r in list(benzinga_records) + list(finnhub_records)
        if r.get("record_type") != "article"
    ]
    unreliable: set[tuple[Any, Any]] = set()
    for status in statuses:
        if (
            status.get("record_type") == "poll_status"
            and status.get("status") == "error"
        ):
            unreliable.add(_cell(status))

    planned: dict[tuple[Any, Any], set[Any]] = {}
    completed: dict[tuple[Any, Any], set[Any]] = {}
    for frame in poll_frames:
        frame_type = frame.get("record_type")
        poll_id = frame.get("poll_id")
        if poll_id is None:
            continue
        # hk 7092: a caller-supplied poll_id may be reused across runs, so a
        # header and its completion must also agree on the per-run attempt id.
        # Frames written before attempt ids existed key on (poll_id, None).
        key = (poll_id, frame.get("attempt_id"))
        if frame_type == "poll_header":
            symbols = frame.get("symbols")
            if isinstance(symbols, list):
                planned.setdefault(key, set()).update(
                    s for s in symbols if isinstance(s, str)
                )
        elif frame_type == "poll_completion":
            symbols = frame.get("symbols_completed")
            if isinstance(symbols, list):
                completed.setdefault(key, set()).update(
                    s for s in symbols if isinstance(s, str)
                )
    incomplete_polls: list[dict[str, Any]] = []
    for poll_id, attempt_id in sorted(planned, key=str):
        wanted = planned[(poll_id, attempt_id)]
        done = completed.get((poll_id, attempt_id))
        entry: dict[str, Any] = {"poll_id": poll_id}
        if attempt_id is not None:
            entry["attempt_id"] = attempt_id
        if done is None:
            entry["reason"] = "no_completion_record"
            entry["symbols"] = sorted(wanted, key=str)
            incomplete_polls.append(entry)
            unreliable.update((poll_id, symbol) for symbol in wanted)
        else:
            missing = wanted - done
            if missing:
                entry["reason"] = "symbols_unpolled"
                entry["symbols"] = sorted(missing, key=str)
                incomplete_polls.append(entry)
                unreliable.update((poll_id, symbol) for symbol in missing)
    ben_all = [r for r in benzinga_records if r.get("record_type") == "article"]
    fin_all = [r for r in finnhub_records if r.get("record_type") == "article"]
    ben_excluded = [r for r in ben_all if _cell(r) in unreliable]
    fin_excluded = [r for r in fin_all if _cell(r) in unreliable]
    ben, ben_dupes = _dedupe_articles(
        [r for r in ben_all if _cell(r) not in unreliable]
    )
    fin, fin_dupes = _dedupe_articles(
        [r for r in fin_all if _cell(r) not in unreliable]
    )

    def by_url(
        records: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            key = normalize_url(record.get("url"))
            if key is not None:
                groups.setdefault(key, []).append(record)
        return groups

    def by_title(
        records: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            key = normalize_title(record.get("title"))
            if key is not None:
                groups.setdefault(f"title:{key}", []).append(record)
        return groups

    def order(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            records, key=lambda r: _seen_at(r) or datetime.max.replace(tzinfo=UTC)
        )

    ben_url, fin_url = by_url(ben), by_url(fin)
    overlap_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ben_leftover: list[dict[str, Any]] = []
    fin_leftover: list[dict[str, Any]] = []
    for key in sorted(set(ben_url) & set(fin_url)):
        ben_side, fin_side = order(ben_url[key]), order(fin_url[key])
        overlap_pairs.extend(zip(ben_side, fin_side, strict=False))
        ben_leftover.extend(ben_side[len(fin_side) :])
        fin_leftover.extend(fin_side[len(ben_side) :])
    for key in sorted(set(ben_url) - set(fin_url)):
        ben_leftover.extend(ben_url[key])
    for key in sorted(set(fin_url) - set(ben_url)):
        fin_leftover.extend(fin_url[key])

    ben_leftover.extend(r for r in ben if normalize_url(r.get("url")) is None)
    fin_leftover.extend(r for r in fin if normalize_url(r.get("url")) is None)
    ben_unique = sum(1 for r in ben_leftover if normalize_title(r.get("title")) is None)
    fin_unique = sum(1 for r in fin_leftover if normalize_title(r.get("title")) is None)
    ben_title = by_title(
        [r for r in ben_leftover if normalize_title(r.get("title")) is not None]
    )
    fin_title = by_title(
        [r for r in fin_leftover if normalize_title(r.get("title")) is not None]
    )
    for key in sorted(set(ben_title) & set(fin_title)):
        ben_side, fin_side = order(ben_title[key]), order(fin_title[key])
        overlap_pairs.extend(zip(ben_side, fin_side, strict=False))
        ben_unique += max(0, len(ben_side) - len(fin_side))
        fin_unique += max(0, len(fin_side) - len(ben_side))
    for key in sorted(set(ben_title) - set(fin_title)):
        ben_unique += len(ben_title[key])
    for key in sorted(set(fin_title) - set(ben_title)):
        fin_unique += len(fin_title[key])

    latencies: list[float] = []
    latency_pairs = 0
    for ben_first, fin_first in overlap_pairs:
        if ben_first.get("poll_id") != fin_first.get("poll_id"):
            continue
        ben_seen, fin_seen = _seen_at(ben_first), _seen_at(fin_first)
        if ben_seen is None or fin_seen is None:
            continue
        latency_pairs += 1
        latencies.append((ben_seen - fin_seen).total_seconds())

    lat_sorted = sorted(latencies)

    def duplicate_rate(count: int, removed: int) -> float | None:
        if not count:
            return None
        return removed / count

    ben_by_param: dict[str, int] = {}
    for record in ben:
        param = record.get("query_param") or "unknown"
        ben_by_param[str(param)] = ben_by_param.get(str(param), 0) + 1

    return {
        "benzinga_count": len(ben),
        "finnhub_count": len(fin),
        "benzinga_by_query_param": dict(sorted(ben_by_param.items())),
        "overlap_articles": len(overlap_pairs),
        "benzinga_unique": ben_unique,
        "finnhub_unique": fin_unique,
        "latency_pairs_same_poll": latency_pairs,
        "latency_seconds": {
            "min": lat_sorted[0] if lat_sorted else None,
            "p50": lat_sorted[len(lat_sorted) // 2] if lat_sorted else None,
            "max": lat_sorted[-1] if lat_sorted else None,
        },
        "benzinga_duplicate_rate": duplicate_rate(len(ben) + ben_dupes, ben_dupes),
        "finnhub_duplicate_rate": duplicate_rate(len(fin) + fin_dupes, fin_dupes),
        "benzinga_duplicates_removed": ben_dupes,
        "finnhub_duplicates_removed": fin_dupes,
        "unreliable_symbol_polls": [
            {"poll_id": poll, "symbol": symbol}
            for poll, symbol in sorted(unreliable, key=str)
        ],
        "incomplete_polls": incomplete_polls,
        "excluded_unreliable_articles": {
            "benzinga": len(ben_excluded),
            "finnhub": len(fin_excluded),
        },
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
