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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.services import naver_finance, symbol_news_store
from app.services.finnhub_news import fetch_news_finnhub
from app.services.symbol_news_store import FeedArticleInput, StoredSymbolNews

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
    excluded_count: int = 0
    degraded: bool = False
    fetch_error: str | None = None


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


_PENDING_RELEVANCE: dict[str, Any] = {
    "status": "pending",
    "relationship": None,
    "relevance": None,
    "price_relevance": None,
    "score": None,
    "reason": None,
    "judged_by": None,
    "judged_at": None,
    "hints": None,
}


def symbol_news_store_hints(symbol: str, title: str) -> dict[str, Any] | None:
    from app.services.symbol_news_relevance import build_relevance_hints

    return build_relevance_hints(symbol=symbol, market="kr", title=title)


async def _fetch_naver(
    symbol: str, limit: int, fetched_at: datetime
) -> list[SymbolNewsArticle]:
    """Pure normalize: URL dedupe only — no filtering, no relevance verdicts."""
    items = await naver_finance.fetch_news(symbol, limit=limit)
    out: list[SymbolNewsArticle] = []
    seen_urls: set[str] = set()
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
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


def _stored_to_article(
    row: StoredSymbolNews,
    *,
    provider: str,
    market: str,
    symbol: str,
    fetched_at: datetime,
    raw_by_url: dict[str, Any],
) -> SymbolNewsArticle:
    source_item = raw_by_url.get(row.url) or {
        "title": row.title,
        "url": row.url,
        "source": row.source or "",
        "datetime": row.published_at.isoformat() if row.published_at else None,
        **({"summary": row.summary or ""} if provider == "finnhub" else {}),
    }
    external_id = (
        _naver_external_id(row.url) if provider == "naver" else _url_hash(row.url)
    )
    return SymbolNewsArticle(
        provider=provider,
        market=market,
        symbol=symbol,
        external_article_id=external_id,
        title=row.title,
        source_name=row.source,
        canonical_url=row.url,
        summary=row.summary if provider == "finnhub" else None,
        published_at=row.published_at,
        fetched_at=fetched_at,
        related_symbols=[],
        provider_metadata={"source_item": source_item, "relevance": row.relevance},
    )


async def _maybe_enqueue_judgment(market: str, symbol: str, new_pending: int) -> None:
    """ROB-506: fire-and-forget judgment enqueue. Never raises into get_news."""
    if new_pending <= 0:
        return
    if not settings.NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED:
        return
    try:
        # Lazy import — keeps the taskiq broker out of plain MCP import paths.
        from app.tasks.news_relevance_judgment_tasks import (
            news_relevance_judge_pending,
        )

        await news_relevance_judge_pending.kiq(
            market=market, symbol=symbol, dry_run=False
        )
    except Exception as exc:  # noqa: BLE001 — enqueue must be fail-open
        logger.warning(
            "symbol_news_service: judgment enqueue failed (fail-open): "
            "market=%s symbol=%s err=%s",
            market,
            symbol,
            exc,
        )


async def _persist_and_load(
    symbol: str,
    market: str,
    provider: str,
    feed_source: str,
    fetched: list[SymbolNewsArticle],
    limit: int,
    fetched_at: datetime,
) -> tuple[list[SymbolNewsArticle], int] | None:
    """Persist this window then serve canonical DB state. None → DB unavailable."""
    inserted: Any = 0
    try:
        async with AsyncSessionLocal() as db:
            if fetched:
                inserted = await symbol_news_store.upsert_feed_articles(
                    db,
                    market,
                    symbol,
                    [
                        FeedArticleInput(
                            url=a.canonical_url,
                            title=a.title,
                            source=a.source_name,
                            published_at=a.published_at,
                            summary=a.summary,
                        )
                        for a in fetched
                    ],
                    feed_source=feed_source,
                )
            stored, excluded_count = await symbol_news_store.load_symbol_news(
                db, symbol, market, limit
            )
    except Exception as exc:  # noqa: BLE001 — cache layer must not kill the tool
        logger.warning(
            "symbol_news_service: store unavailable, degrading: "
            "market=%s symbol=%s err=%s",
            market,
            symbol,
            exc,
        )
        return None
    new_pending = inserted if isinstance(inserted, int) else 0
    await _maybe_enqueue_judgment(market, symbol, new_pending)
    raw_by_url = {
        a.canonical_url: a.provider_metadata.get("source_item") for a in fetched
    }
    articles = [
        _stored_to_article(
            row,
            provider=provider,
            market=market,
            symbol=symbol,
            fetched_at=fetched_at,
            raw_by_url=raw_by_url,
        )
        for row in stored
    ]
    return articles, excluded_count


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

    if market == "kr":
        fetched: list[SymbolNewsArticle] | None
        fetch_error: str | None = None
        try:
            fetched = await asyncio.wait_for(
                _fetch_naver(symbol, limit, fetched_at), timeout=timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — fall back to DB cache
            logger.warning(
                "symbol_news_service: naver fetch failed: symbol=%s err=%s",
                symbol,
                exc,
            )
            fetched = None
            fetch_error = type(exc).__name__

        persisted = await _persist_and_load(
            symbol,
            "kr",
            "naver",
            symbol_news_store.KR_FEED_SOURCE,
            fetched or [],
            limit,
            fetched_at,
        )
        if persisted is not None:
            articles, excluded_count = persisted
            if fetched is None and not articles:
                return SymbolNewsFetchResult(
                    symbol,
                    market,
                    provider,
                    "error",
                    limit,
                    0,
                    [],
                    fetch_error or "naver_fetch_failed",
                )
            status = "ok" if articles else "empty"
            return SymbolNewsFetchResult(
                symbol,
                market,
                provider,
                status,
                limit,
                len(articles),
                articles,
                None,
                excluded_count=excluded_count,
                degraded=fetched is None,
                fetch_error=fetch_error,
            )
        # DB 불가 — 기존 on-demand 동작으로 degrade (전부 pending 표시)
        if fetched is None:
            return SymbolNewsFetchResult(
                symbol,
                market,
                provider,
                "error",
                limit,
                0,
                [],
                fetch_error or "naver_fetch_failed",
            )
        articles = [
            replace(
                a,
                provider_metadata={
                    **a.provider_metadata,
                    "relevance": {
                        **_PENDING_RELEVANCE,
                        "hints": symbol_news_store_hints(symbol, a.title),
                    },
                },
            )
            for a in fetched
        ]
        status = "ok" if articles else "empty"
        return SymbolNewsFetchResult(
            symbol, market, provider, status, limit, len(articles), articles, None
        )

    try:
        if market in ("us", "crypto"):
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
