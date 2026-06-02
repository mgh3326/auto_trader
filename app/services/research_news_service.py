# app/services/research_news_service.py
"""Legacy research-pipeline news shim (ROB-115 → ROB-423).

Thin wrapper that delegates to the unified ``symbol_news_service`` seam and maps
its richer ``SymbolNewsArticle`` back to the legacy ``NormalizedArticle`` shape
consumed by ``app.analysis.stages.news_stage``. No fetch/normalize logic lives
here anymore — single source of truth is ``symbol_news_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services import symbol_news_service

_MARKET_BY_INSTRUMENT = {"equity_kr": "kr", "equity_us": "us"}


@dataclass(frozen=True)
class NormalizedArticle:
    url: str
    title: str
    source: str | None
    summary: str | None
    published_at: datetime | None
    provider: str


async def fetch_symbol_news(
    symbol: str, instrument_type: str, *, limit: int = 20, timeout_s: float = 5.0
) -> list[NormalizedArticle]:
    market = _MARKET_BY_INSTRUMENT.get(instrument_type)
    if market is None:
        return []
    result = await symbol_news_service.fetch_symbol_news(
        symbol, market, instrument_type, limit=limit, timeout_s=timeout_s
    )
    return [
        NormalizedArticle(
            url=a.canonical_url,
            title=a.title,
            source=a.source_name,
            summary=a.summary,
            published_at=a.published_at,
            provider=a.provider,
        )
        for a in result.articles
    ]
