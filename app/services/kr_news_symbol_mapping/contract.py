"""뉴스-종목 매핑 read-model 계약 — provenance-rich 통합 뷰 (ROB-398 Slice 1).

기존 데이터(article.stock_symbol / news_article_related_symbols / 별칭 matcher)
위의 read-only 뷰. write/migration 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# mapping_source 우선순위 (작을수록 우선). naver_code = 확정, ner = 최약.
MAPPING_SOURCE_PRIORITY: dict[str, int] = {
    "naver_code": 0,
    "candidate": 1,
    "ner": 2,
}

NER_CONFIDENCE: float = 0.5
CANDIDATE_DEFAULT_CONFIDENCE: float = 0.7  # candidate row.score 부재 시
FRESHNESS_TTL_HOURS: int = 24


@dataclass(frozen=True)
class CandidateRow:
    """news_article_related_symbols 한 행의 read-only 뷰 (candidate source)."""

    symbol: str
    source: str
    score: float | None = None
    rank: int | None = None
    matched_term: str | None = None


@dataclass(frozen=True)
class MappedSymbol:
    symbol: str
    market: str
    mapping_source: str  # "naver_code" | "candidate" | "ner"
    confidence: float  # 0.0..1.0
    is_primary: bool
    matched_term: str | None


@dataclass(frozen=True)
class ArticleView:
    """resolver/query_service 입력용 기사 뷰 (DB ORM 비의존, 테스트 친화)."""

    market: str
    stock_symbol: str | None
    related_rows: tuple[CandidateRow, ...]
    title: str | None
    summary: str | None
    keywords: tuple[str, ...]
    as_of: datetime
    url: str | None = None


@dataclass(frozen=True)
class MappedArticle:
    as_of: datetime
    title: str | None
    mapped_symbols: tuple[MappedSymbol, ...]
    url: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class Freshness:
    overall: str  # "fresh" | "stale" | "unavailable"
    latest_as_of: datetime | None
    stale_reason: str | None


@dataclass(frozen=True)
class SymbolNewsMapping:
    symbol: str
    market: str
    articles: tuple[MappedArticle, ...]
    freshness: Freshness
