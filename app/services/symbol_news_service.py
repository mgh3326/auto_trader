# app/services/symbol_news_service.py
"""Unified on-demand symbol news service (ROB-423 PR1).

Single normalized seam over the service-layer provider fetchers
(``naver_finance.fetch_news``, ``finnhub_news.fetch_news_finnhub``). Consumed by
the ``get_news`` MCP tool, the snapshot-backed news collector, and (via a thin
shim) the legacy research news path. No MCP imports, no LLM, no order/broker
surface. Each article keeps the provider's original item in
``provider_metadata["source_item"]`` so byte-compatible envelopes can be rebuilt.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.services import naver_finance
from app.services.finnhub_news import fetch_news_finnhub

logger = logging.getLogger(__name__)

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


@dataclass(frozen=True)
class SymbolNewsArticle:
    provider: str
    market: str
    symbol: str
    external_article_id: str | None
    title: str
    source_name: str | None
    canonical_url: str
    summary: str | None
    published_at: datetime | None
    fetched_at: datetime
    related_symbols: list[str] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SymbolNewsFetchResult:
    symbol: str
    market: str
    provider: str
    status: str  # ok | empty | unavailable | error
    requested_limit: int
    returned_count: int
    articles: list[SymbolNewsArticle]
    error_code: str | None = None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _naver_external_id(url: str) -> str | None:
    """``officeId:articleId`` from a Naver news_read URL, else None."""
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    article_id = (q.get("article_id") or [None])[0]
    office_id = (q.get("office_id") or [None])[0]
    if office_id and article_id:
        return f"{office_id}:{article_id}"
    return article_id or None


def _url_hash(url: str) -> str | None:
    if not url:
        return None
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


async def _fetch_naver(
    symbol: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    items = await naver_finance.fetch_news(symbol, limit=limit)
    out: list[SymbolNewsArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            SymbolNewsArticle(
                provider="naver",
                market="kr",
                symbol=symbol,
                external_article_id=_naver_external_id(url),
                title=title,
                source_name=raw.get("source") or None,
                canonical_url=url,
                summary=None,
                published_at=_parse_dt(raw.get("datetime")),
                fetched_at=fetched_at,
                related_symbols=[],
                provider_metadata={"source_item": raw},
            )
        )
    return out


async def _fetch_finnhub(
    symbol: str, market: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    payload = await fetch_news_finnhub(symbol, market, limit)
    out: list[SymbolNewsArticle] = []
    for raw in payload.get("news") or []:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        related_raw = raw.get("related") or ""
        related = [s for s in str(related_raw).split(",") if s]
        out.append(
            SymbolNewsArticle(
                provider="finnhub",
                market=market,
                symbol=symbol,
                external_article_id=_url_hash(url),
                title=title,
                source_name=raw.get("source") or None,
                canonical_url=url,
                summary=raw.get("summary") or None,
                published_at=_parse_dt(raw.get("datetime")),
                fetched_at=fetched_at,
                related_symbols=related,
                provider_metadata={
                    "sentiment": raw.get("sentiment"),
                    "related": related_raw,
                    "source_item": raw,
                },
            )
        )
    return out


async def fetch_symbol_news(
    symbol: str,
    market: str,
    instrument_type: str | None = None,
    *,
    limit: int = 20,
    timeout_s: float = 5.0,
) -> SymbolNewsFetchResult:
    """On-demand normalized news for one symbol. Fail-soft (never raises)."""
    market = (market or "").lower()
    provider = "naver" if market == "kr" else "finnhub"
    fetched_at = _utcnow()
    try:
        if market == "kr":
            articles = await asyncio.wait_for(
                _fetch_naver(symbol, limit, fetched_at), timeout=timeout_s
            )
        elif market in ("us", "crypto"):
            articles = await asyncio.wait_for(
                _fetch_finnhub(symbol, market, limit, fetched_at), timeout=timeout_s
            )
        else:
            return SymbolNewsFetchResult(
                symbol,
                market,
                provider,
                "unavailable",
                limit,
                0,
                [],
                "unsupported_market",
            )
    except Exception as exc:  # noqa: BLE001 — overlay evidence, fail soft
        logger.warning(
            "symbol_news_service.fetch_symbol_news failed: symbol=%s market=%s err=%s",
            symbol,
            market,
            exc,
        )
        return SymbolNewsFetchResult(
            symbol, market, provider, "error", limit, 0, [], type(exc).__name__
        )
    status = "ok" if articles else "empty"
    return SymbolNewsFetchResult(
        symbol, market, provider, status, limit, len(articles), articles, None
    )
