from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.symbol import to_db_symbol
from app.services.benzinga_news import normalize_title

BASE_URL = "https://api.prod.fivelines.co.kr"
USER_AGENT = "auto_trader-news-shadow/1.0 (read-only; contact: operator)"
MIN_REQUEST_INTERVAL_S = 0.5
MAX_PAGES = 10
DEFAULT_PAGE_SIZE = 50
_US_TICKER_TAG = re.compile(r"\$([A-Z][A-Z0-9.\-]{0,14})\Z")


class SaveTickerError(RuntimeError):
    """Closed Save+ HTTP or response-contract error."""


class SaveTickerClient:
    """Small, read-only async client for the unofficial Save+ public API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
        min_interval_s: float = MIN_REQUEST_INTERVAL_S,
        sleep=asyncio.sleep,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout_s = timeout_s
        self._min_interval_s = min_interval_s
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._clock = asyncio.get_running_loop().time if _in_loop() else None

    async def __aenter__(self) -> SaveTickerClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(self._timeout_s),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError(
                "SaveTickerClient must be used as an async context manager"
            )
        if self._clock is None:
            self._clock = asyncio.get_running_loop().time
        if self._last_request_at is not None and self._clock is not None:
            elapsed = self._clock() - self._last_request_at
            if elapsed < self._min_interval_s:
                await self._sleep(self._min_interval_s - elapsed)
        self._last_request_at = self._clock() if self._clock is not None else 0.0
        try:
            response = await self._client.get(
                path,
                params=dict(params),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                follow_redirects=False,
            )
            response.raise_for_status()
            media_type = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if media_type != "application/json":
                raise SaveTickerError("InvalidContentType")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SaveTickerError(type(exc).__name__) from None
        if not isinstance(payload, dict):
            raise SaveTickerError("InvalidResponse")
        return payload

    async def news_page(
        self, page: int, page_size: int, *, sort: str = "created_at_desc"
    ) -> dict[str, Any]:
        return await self._get(
            "/api/news",
            {"page": page, "page_size": page_size, "sort": sort},
        )

    async def search_page(
        self, search: str, page: int, page_size: int
    ) -> dict[str, Any]:
        return await self._get(
            "/api/search",
            {"search": search, "page": page, "page_size": page_size},
        )

    async def top_stories(self) -> dict[str, Any]:
        return await self._get("/api/news/top-stories", {})

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _count(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        name = _text(item.get("name")) if isinstance(item, dict) else _text(item)
        if name is not None:
            names.append(name)
    return names


def _parse_created(value: Any) -> tuple[str | None, str | None]:
    raw = _text(value)
    if raw is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw, None
    if parsed.tzinfo is None:
        return raw, None
    return raw, parsed.astimezone(UTC).isoformat()


def _us_ticker_symbols(item: dict[str, Any]) -> list[str]:
    symbols: list[str] = []
    tickers = item.get("tickers")
    if isinstance(tickers, list):
        for ticker in tickers:
            symbol = _text(ticker.get("symbol")) if isinstance(ticker, dict) else None
            if symbol is not None and not symbol.upper().endswith(".KR"):
                normalized = to_db_symbol(symbol.upper())
                if normalized not in symbols:
                    symbols.append(normalized)
    tag_names = item.get("tag_names")
    if isinstance(tag_names, list):
        for tag in tag_names:
            if not isinstance(tag, str):
                continue
            match = _US_TICKER_TAG.fullmatch(tag.strip().upper())
            if match is None:
                continue
            normalized = to_db_symbol(match.group(1))
            if normalized not in symbols:
                symbols.append(normalized)
    return symbols


def normalize_item(item: Any) -> dict[str, Any] | None:
    """Project a valid provider item while retaining the unmodified wire row."""
    if not isinstance(item, dict):
        return None
    item_id = _text(item.get("id"))
    title = _text(item.get("title"))
    created_raw, created_utc = _parse_created(item.get("created_at"))
    if item_id is None or title is None or created_utc is None:
        return None
    for field in ("view_count", "comment_count"):
        if field in item and item[field] is not None and _count(item[field]) is None:
            return None
    for field in ("like_stats", "vote_stats"):
        stats = item.get(field)
        if stats is not None and not isinstance(stats, dict):
            return None
        if isinstance(stats, dict):
            count_field = "like_count" if field == "like_stats" else "total_count"
            if (
                count_field in stats
                and stats[count_field] is not None
                and _count(stats[count_field]) is None
            ):
                return None
    for field in ("tickers", "content_labels"):
        values = item.get(field)
        if values is not None and not isinstance(values, list):
            return None
        if isinstance(values, list):
            for value in values:
                if (
                    not isinstance(value, dict)
                    or _text(value.get("symbol" if field == "tickers" else "name"))
                    is None
                ):
                    return None
    for field in ("is_top_story", "is_headline_only"):
        if field in item and item[field] is not None and type(item[field]) is not bool:
            return None
    ticker_symbols = _us_ticker_symbols(item)
    like_stats = item.get("like_stats")
    vote_stats = item.get("vote_stats")
    likes = (
        _count(like_stats.get("like_count")) if isinstance(like_stats, dict) else None
    )
    votes = (
        _count(vote_stats.get("total_count")) if isinstance(vote_stats, dict) else None
    )
    comments = _count(item.get("comment_count"))
    views = _count(item.get("view_count"))
    labels = _name_list(item.get("content_labels"))
    extra = item.get("extra")
    source_created_raw, source_created_utc = _parse_created(
        extra.get("source_created_at") if isinstance(extra, dict) else None
    )
    return {
        "record_type": "article",
        "source": "saveticker",
        "provider_id": item_id,
        "url": None,
        "title": title,
        "summary": _text(item.get("content")),
        "teaser": None,
        "symbols": ticker_symbols,
        "query_symbol": None,
        "query_param": None,
        "tickers": ticker_symbols,
        "labels": labels,
        "source_name": _text(item.get("source")),
        "channel": _text(item.get("channel")),
        "created_raw": created_raw,
        "created_utc": created_utc,
        "source_created_raw": source_created_raw,
        "source_created_utc": source_created_utc,
        "view_count": views,
        "comment_count": comments,
        "like_stats": like_stats,
        "like_count": likes,
        "vote_stats": vote_stats,
        "vote_count": votes,
        "is_top_story": item.get("is_top_story")
        if type(item.get("is_top_story")) is bool
        else False,
        "is_headline_only": item.get("is_headline_only")
        if type(item.get("is_headline_only")) is bool
        else False,
        "raw_item": item,
    }


def normalize_envelope(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """Return valid normalized rows and the number of malformed provider items."""
    if not isinstance(payload, dict) or not isinstance(payload.get("news_list"), list):
        raise SaveTickerError("InvalidNewsEnvelope")
    rows = []
    malformed = 0
    for item in payload["news_list"]:
        normalized = normalize_item(item)
        if normalized is None:
            malformed += 1
        else:
            rows.append(normalized)
    return rows, malformed


def summary_key_title(record: dict[str, Any]) -> str | None:
    return normalize_title(record.get("title"))


__all__ = [
    "BASE_URL",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGES",
    "MIN_REQUEST_INTERVAL_S",
    "SaveTickerClient",
    "SaveTickerError",
    "normalize_envelope",
    "normalize_item",
    "summary_key_title",
    "_us_ticker_symbols",
]
