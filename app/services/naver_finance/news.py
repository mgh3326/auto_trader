"""Naver stock news fetching via the m.stock.naver.com JSON API.

The legacy finance.naver.com/item/news_news.naver HTML table was retired
upstream (HTTP 410 Gone as of 2026-09-16; #904 desk probe). The replacement
symbol-scoped feed is::

    GET https://m.stock.naver.com/api/news/stock/{code}?pageSize={groups}&page={page}

returning a JSON array of groups ``[{total, items: [article, ...]}, ...]``
(newest first; ``pageSize`` counts groups, ``page`` starts at 1). Article keys
per the desk probe: ``id`` (officeId+articleId dedupe key), ``officeId``,
``articleId``, ``officeName``, ``datetime`` (``YYYYMMDDHHMM`` KST), ``type``,
``title``, ``titleFull``, ``body``, ``photoType``, ``imageOriginLink``,
``mobileNewsUrl``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.naver_finance.parser import DEFAULT_HEADERS

logger = logging.getLogger(__name__)

NAVER_STOCK_NEWS_API = "https://m.stock.naver.com/api/news/stock"
# ``pageSize`` counts news groups, not articles; request a bounded bundle and
# flatten. Single request keeps provider fan-out identical to the HTML era.
_NEWS_GROUPS_PER_REQUEST = 10
_KST = timezone(timedelta(hours=9))


class NaverNewsContractError(RuntimeError):
    """The news API returned a payload outside the probed shape."""


@dataclass(frozen=True)
class NaverNewsFetchResult:
    items: list[dict[str, Any]]
    skipped: dict[str, int] = field(default_factory=dict)


def _parse_news_datetime(value: Any) -> str | None:
    """``YYYYMMDDHHMM`` (KST wall clock) -> ISO-8601 string with +09:00 offset."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) != 12 or not value.isdigit():
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=_KST).isoformat()


def _normalize_news_item(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Map one provider article to the normalized feed dict.

    Returns ``(item, None)`` on success or ``(None, skip_reason)`` when the
    item is malformed — the caller counts skips by reason.
    """
    if not isinstance(raw, dict):
        return None, "invalid_item"
    title = (
        str(raw.get("title") or "").strip() or str(raw.get("titleFull") or "").strip()
    )
    if not title:
        return None, "missing_title"
    url = str(raw.get("mobileNewsUrl") or "").strip()
    if not url or not url.startswith("http"):
        return None, "missing_url"
    published = _parse_news_datetime(raw.get("datetime"))
    if published is None:
        return None, "invalid_datetime"
    office_id = raw.get("officeId")
    article_id = raw.get("articleId")
    return (
        {
            "title": title,
            "url": url,
            "source": str(raw.get("officeName") or "").strip(),
            "datetime": published,
            "id": str(raw.get("id") or "").strip() or None,
            "officeId": str(office_id) if office_id is not None else None,
            "articleId": str(article_id) if article_id is not None else None,
        },
        None,
    )


async def _fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """GET JSON helper — mirrors ``_fetch_html`` so tests patch one seam."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(url, params=params, headers=DEFAULT_HEADERS)
        response.raise_for_status()
        return response.json()


async def fetch_stock_news(code: str, limit: int = 20) -> NaverNewsFetchResult:
    """Fetch symbol news from the m.stock.naver.com JSON API.

    Provider failures propagate (HTTPStatusError, JSON decode errors,
    :class:`NaverNewsContractError` on an unexpected envelope) so callers can
    mark the ingestion as failed instead of pretending success. Malformed
    items are dropped and counted in ``result.skipped`` by reason.
    """
    url = f"{NAVER_STOCK_NEWS_API}/{code}"
    payload = await _fetch_json(
        url,
        params={
            "pageSize": _NEWS_GROUPS_PER_REQUEST,
            "page": 1,
        },
    )
    if not isinstance(payload, list):
        raise NaverNewsContractError(
            f"unexpected news payload {type(payload).__name__}; expected list of groups"
        )

    items: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()

    def _count(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def _result() -> NaverNewsFetchResult:
        if skipped:
            logger.warning(
                "naver news: skipped %s malformed item(s) for %s: %s",
                sum(skipped.values()),
                code,
                skipped,
            )
        return NaverNewsFetchResult(items=items, skipped=skipped)

    for group in payload:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            _count("invalid_group")
            continue
        for raw in group["items"]:
            item, reason = _normalize_news_item(raw)
            if item is None:
                _count(reason or "invalid_item")
                continue
            dedupe_key = item["id"] or item["url"]
            if dedupe_key in seen:
                _count("duplicate")
                continue
            seen.add(dedupe_key)
            items.append(item)
            if len(items) >= limit:
                return _result()

    return _result()


async def fetch_news(code: str, limit: int = 20) -> list[dict[str, Any]]:
    """Fetch stock news items for a 6-digit Korean stock code.

    Returns the normalized item list; drop counts are available via
    :func:`fetch_stock_news` for callers that surface ingestion detail.
    """
    result = await fetch_stock_news(code, limit=limit)
    return result.items
