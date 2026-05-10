"""ROB-169: KR alias-data smoke tests for KR investment relevance constants."""

from __future__ import annotations

from app.services.news_entity_alias_data import (
    KR_BIG_CAP_GROUP_SYMBOLS,
    KR_BROAD_MARKET_TERMS,
    KR_CRIME_TERMS,
    KR_INVEST_KEYWORDS,
    KR_NOISE_TERMS,
    KR_SOCIETY_TERMS,
)


def test_kr_broad_market_terms_include_indices_and_macro():
    expected = {"코스피", "코스닥", "kospi", "kosdaq", "기준금리", "환율", "원달러", "ipo"}
    assert expected.issubset({t.lower() for t in KR_BROAD_MARKET_TERMS})


def test_kr_invest_keywords_cover_core_industries_and_policy():
    expected = {"반도체", "배터리", "etf", "공모주", "상장", "금융위", "한국은행"}
    assert expected.issubset({t.lower() for t in KR_INVEST_KEYWORDS} | {t.lower() for t in KR_BROAD_MARKET_TERMS})


def test_kr_society_terms_cover_crime_and_celebrity_noise():
    assert "살해" in KR_CRIME_TERMS
    assert "피의자" in KR_CRIME_TERMS
    assert "연예" in KR_SOCIETY_TERMS or "연예인" in KR_SOCIETY_TERMS
    assert "사이코패스" in KR_NOISE_TERMS or "사이코패스" in KR_CRIME_TERMS


def test_kr_big_cap_group_symbols_includes_top_kospi():
    assert {"005930", "000660"}.issubset(KR_BIG_CAP_GROUP_SYMBOLS)
