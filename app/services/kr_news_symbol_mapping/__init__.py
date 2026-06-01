"""KR 뉴스-종목 매핑 read-model (ROB-398 Slice 1)."""

from app.services.kr_news_symbol_mapping.contract import (
    ArticleView,
    CandidateRow,
    Freshness,
    MappedArticle,
    MappedSymbol,
    SymbolNewsMapping,
)
from app.services.kr_news_symbol_mapping.freshness import derive_freshness
from app.services.kr_news_symbol_mapping.query_service import get_symbol_news_mapping
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols

__all__ = [
    "ArticleView",
    "CandidateRow",
    "Freshness",
    "MappedArticle",
    "MappedSymbol",
    "SymbolNewsMapping",
    "derive_freshness",
    "get_symbol_news_mapping",
    "resolve_article_symbols",
]
