"""provenance 통합 + is_primary 파생 (ROB-398 Slice 1, 순수 함수).

우선순위 naver_code(1.0) > candidate > ner. is_primary 는 확정 소스(naver_code)
또는 단일 후보일 때만 True; 그 외(복수 후보, 확정 없음)면 전부 False(모호성 보류).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.services.kr_news_symbol_mapping.contract import (
    CANDIDATE_DEFAULT_CONFIDENCE,
    MAPPING_SOURCE_PRIORITY,
    NER_CONFIDENCE,
    CandidateRow,
    MappedSymbol,
)
from app.services.news_entity_matcher import SymbolMatch


def _candidate_confidence(score: float | None) -> float:
    if score is None:
        return CANDIDATE_DEFAULT_CONFIDENCE
    return max(0.0, min(1.0, float(score)))


def resolve_article_symbols(
    *,
    market: str,
    stock_symbol: str | None,
    related_rows: Sequence[CandidateRow],
    ner_matches: Sequence[SymbolMatch],
) -> list[MappedSymbol]:
    # symbol -> (priority, mapping_source, confidence, matched_term)
    best: dict[str, tuple[int, str, float, str | None]] = {}

    def _offer(
        symbol: str, source: str, confidence: float, matched_term: str | None
    ) -> None:
        symbol = symbol.upper()
        priority = MAPPING_SOURCE_PRIORITY[source]
        existing = best.get(symbol)
        if existing is None or priority < existing[0]:
            best[symbol] = (priority, source, confidence, matched_term)

    if stock_symbol:
        _offer(stock_symbol, "naver_code", 1.0, None)
    for row in related_rows:
        _offer(
            row.symbol, "candidate", _candidate_confidence(row.score), row.matched_term
        )
    for match in ner_matches:
        _offer(match.symbol, "ner", NER_CONFIDENCE, match.matched_term)

    if not best:
        return []

    confirmed_symbol = stock_symbol.upper() if stock_symbol else None
    only_one = len(best) == 1

    out: list[MappedSymbol] = []
    for symbol in sorted(best):
        _priority, source, confidence, matched_term = best[symbol]
        if confirmed_symbol is not None:
            is_primary = symbol == confirmed_symbol
        else:
            is_primary = only_one
        out.append(
            MappedSymbol(
                symbol=symbol,
                market=market,
                mapping_source=source,
                confidence=confidence,
                is_primary=is_primary,
                matched_term=matched_term,
            )
        )
    return out
