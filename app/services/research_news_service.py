"""Research pipeline on-demand news fetcher (ROB-115)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services import naver_finance
from app.services.finnhub_news import fetch_news_finnhub

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedArticle:
    url: str
    title: str
    source: str | None
    summary: str | None
    published_at: datetime | None
    provider: str


async def _naver_fetch_news(symbol: str, limit: int) -> list[dict[str, Any]]:
    return await naver_finance.fetch_news(symbol, limit=limit)


async def _finnhub_fetch_news(symbol: str, market: str, limit: int) -> dict[str, Any]:
    return await fetch_news_finnhub(symbol, market, limit)


def _parse_iso_or_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _normalize_naver(items: list[dict[str, Any]]) -> list[NormalizedArticle]:
    out: list[NormalizedArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            NormalizedArticle(
                url=url,
                title=title,
                source=raw.get("source") or None,
                summary=None,
                published_at=_parse_iso_or_date(raw.get("datetime")),
                provider="naver",
            )
        )
    return out


def _normalize_finnhub(payload: dict[str, Any]) -> list[NormalizedArticle]:
    items = payload.get("news") or []
    out: list[NormalizedArticle] = []
    for raw in items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        if not url or not title:
            continue
        out.append(
            NormalizedArticle(
                url=url,
                title=title,
                source=raw.get("source") or None,
                summary=raw.get("summary") or None,
                published_at=_parse_iso_or_date(raw.get("datetime")),
                provider="finnhub",
            )
        )
    return out


async def fetch_symbol_news(
    symbol: str, instrument_type: str, *, limit: int = 20, timeout_s: float = 5.0
) -> list[NormalizedArticle]:
    try:
        if instrument_type == "equity_kr":
            items = await asyncio.wait_for(
                _naver_fetch_news(symbol, limit), timeout=timeout_s
            )
            return _normalize_naver(items)
        if instrument_type == "equity_us":
            payload = await asyncio.wait_for(
                _finnhub_fetch_news(symbol, "us", limit),
                timeout=timeout_s,
            )
            return _normalize_finnhub(payload)
        return []
    except Exception as exc:
        logger.warning(
            "research_news_service.fetch_symbol_news failed: symbol=%s instrument_type=%s err=%s",
            symbol,
            instrument_type,
            exc,
        )
        return []
