"""뉴스-종목 매핑 read-model query_service (ROB-398 Slice 1).

article-provider(DI)로 ArticleView 들을 받아 per-article provenance 를 resolve 하고
target symbol 을 매핑한 기사만 모아 freshness 와 함께 반환. read-only.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

from app.services.kr_news_symbol_mapping.contract import (
    FRESHNESS_TTL_HOURS,
    ArticleView,
    MappedArticle,
    SymbolNewsMapping,
)
from app.services.kr_news_symbol_mapping.freshness import derive_freshness
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.news_entity_matcher import match_symbols_for_article

ArticleProvider = Callable[[str, str, int, int], Awaitable[Sequence[ArticleView]]]


async def _empty_provider(
    symbol: str, market: str, hours: int, limit: int
) -> list[ArticleView]:
    # 기본 provider: DB 연결은 후속 슬라이스. 미연결 시 honest 빈 결과.
    return []


async def get_symbol_news_mapping(
    symbol: str,
    *,
    market: str = "kr",
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
    ttl_hours: int = FRESHNESS_TTL_HOURS,
    article_provider: ArticleProvider | None = None,
) -> SymbolNewsMapping:
    now = now or datetime.now(UTC)
    provider = article_provider or _empty_provider
    target = symbol.upper()

    raw_articles = await provider(symbol, market, hours, limit)

    mapped_articles: list[MappedArticle] = []
    as_ofs: list[datetime] = []
    for av in raw_articles:
        ner_matches = match_symbols_for_article(
            title=av.title, summary=av.summary, keywords=av.keywords, market=market
        )
        mapped = resolve_article_symbols(
            market=market,
            stock_symbol=av.stock_symbol,
            related_rows=av.related_rows,
            ner_matches=ner_matches,
        )
        if not any(m.symbol == target for m in mapped):
            continue
        # target 매핑을 앞으로 정렬(소비자 편의), 결정적 순서 유지.
        ordered = tuple(sorted(mapped, key=lambda m: (m.symbol != target, m.symbol)))
        mapped_articles.append(
            MappedArticle(
                as_of=av.as_of,
                title=av.title,
                mapped_symbols=ordered,
                url=av.url,
                summary=av.summary,
            )
        )
        as_ofs.append(av.as_of)

    freshness = derive_freshness(as_ofs, now=now, ttl_hours=ttl_hours)
    return SymbolNewsMapping(
        symbol=target,
        market=market,
        articles=tuple(mapped_articles),
        freshness=freshness,
    )
