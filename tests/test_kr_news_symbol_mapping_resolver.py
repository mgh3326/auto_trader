import dataclasses

import pytest

from app.services.kr_news_symbol_mapping.contract import (
    MAPPING_SOURCE_PRIORITY,
    NER_CONFIDENCE,
    CandidateRow,
    MappedSymbol,
)
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.news_entity_matcher import SymbolMatch


@pytest.mark.unit
def test_mapped_symbol_is_frozen():
    m = MappedSymbol(
        symbol="005930",
        market="kr",
        mapping_source="naver_code",
        confidence=1.0,
        is_primary=True,
        matched_term=None,
    )
    assert m.confidence == 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.is_primary = False  # type: ignore[misc]


@pytest.mark.unit
def test_priority_order_and_ner_confidence_constants():
    assert MAPPING_SOURCE_PRIORITY["naver_code"] < MAPPING_SOURCE_PRIORITY["candidate"]
    assert MAPPING_SOURCE_PRIORITY["candidate"] < MAPPING_SOURCE_PRIORITY["ner"]
    assert 0.0 < NER_CONFIDENCE < 1.0


@pytest.mark.unit
def test_candidate_row_holds_score_rank():
    row = CandidateRow(
        symbol="000660",
        source="news_ingestor",
        score=0.8,
        rank=1,
        matched_term="하이닉스",
    )
    assert row.symbol == "000660"
    assert row.score == 0.8


def _ner(symbol, term="x"):
    return SymbolMatch(
        symbol=symbol,
        market="kr",
        canonical_name=symbol,
        matched_term=term,
        reason="alias_dict",
    )


@pytest.mark.unit
def test_naver_code_is_confirmed_primary():
    out = resolve_article_symbols(
        market="kr", stock_symbol="005930", related_rows=(), ner_matches=()
    )
    assert len(out) == 1
    assert out[0].symbol == "005930"
    assert out[0].mapping_source == "naver_code"
    assert out[0].confidence == 1.0
    assert out[0].is_primary is True


@pytest.mark.unit
def test_candidate_source_uses_score_and_matched_term():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(
            CandidateRow(
                symbol="000660",
                source="news_ingestor",
                score=0.8,
                rank=1,
                matched_term="하이닉스",
            ),
        ),
        ner_matches=(),
    )
    assert len(out) == 1
    assert out[0].mapping_source == "candidate"
    assert out[0].confidence == 0.8
    assert out[0].is_primary is True  # 단일 후보


@pytest.mark.unit
def test_candidate_missing_score_uses_default_confidence():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(CandidateRow(symbol="000660", source="x", score=None, rank=2),),
        ner_matches=(),
    )
    assert out[0].confidence == 0.7  # CANDIDATE_DEFAULT_CONFIDENCE


@pytest.mark.unit
def test_ner_single_match_is_primary():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(),
        ner_matches=(_ner("035420", "네이버"),),
    )
    assert out[0].mapping_source == "ner"
    assert out[0].confidence == 0.5
    assert out[0].is_primary is True


@pytest.mark.unit
def test_ner_ambiguity_holds_back_is_primary():
    # 이름충돌(복합 별칭 "삼전닉스" → 005930 + 000660), 확정/후보 disambiguator 없음.
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(),
        ner_matches=(_ner("005930", "삼전닉스"), _ner("000660", "삼전닉스")),
    )
    assert {m.symbol for m in out} == {"005930", "000660"}
    assert all(m.is_primary is False for m in out)  # 강제 단일 금지


@pytest.mark.unit
def test_higher_priority_source_wins_per_symbol():
    # 같은 symbol이 candidate + ner 둘 다 → candidate(우선)로 합쳐짐.
    out = resolve_article_symbols(
        market="kr",
        stock_symbol=None,
        related_rows=(CandidateRow(symbol="035420", source="x", score=0.9),),
        ner_matches=(_ner("035420", "네이버"),),
    )
    assert len(out) == 1
    assert out[0].mapping_source == "candidate"
    assert out[0].confidence == 0.9


@pytest.mark.unit
def test_naver_code_present_makes_other_symbols_non_primary():
    out = resolve_article_symbols(
        market="kr",
        stock_symbol="005930",
        related_rows=(),
        ner_matches=(_ner("000660", "하이닉스"),),
    )
    by_symbol = {m.symbol: m for m in out}
    assert by_symbol["005930"].is_primary is True
    assert by_symbol["000660"].is_primary is False
