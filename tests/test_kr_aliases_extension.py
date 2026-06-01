# tests/test_kr_aliases_extension.py
import pytest

from app.services.news_entity_matcher import match_symbols


def _symbols(text):
    return {m.symbol for m in match_symbols(text, market="kr")}


@pytest.mark.unit
def test_new_curated_aliases_match():
    assert "000270" in _symbols("기아 신차 출시")  # 기아
    assert "006400" in _symbols("삼성SDI 배터리 수주")  # 삼성SDI
    assert "068270" in _symbols("셀트리온 바이오시밀러 허가")  # 셀트리온


@pytest.mark.unit
def test_compound_alias_maps_to_multiple_symbols_ambiguous():
    # "삼전닉스" → 삼성전자(005930) + SK하이닉스(000660) 동시 매칭(이름충돌 시연).
    syms = _symbols("오늘 삼전닉스 강세")
    assert "005930" in syms
    assert "000660" in syms


@pytest.mark.unit
def test_no_false_positive_on_unrelated_text():
    # 무관 텍스트는 매칭되지 않아야 한다(precision 회귀).
    assert _symbols("점심 삼겹살 맛집 추천") == set()
