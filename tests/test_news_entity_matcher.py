"""Unit tests for the deterministic news entity matcher (ROB-130)."""

from __future__ import annotations

import pytest

from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols,
    match_symbols_for_article,
)


@pytest.mark.unit
def test_us_amazon_alias_matches_amzn():
    matches = match_symbols("Amazon raises guidance on AWS demand", market="us")
    symbols = [m.symbol for m in matches]
    assert "AMZN" in symbols
    amzn = next(m for m in matches if m.symbol == "AMZN")
    assert amzn.market == "us"
    assert amzn.reason == "alias_dict"
    assert amzn.matched_term.lower() == "amazon"


@pytest.mark.unit
def test_us_ticker_uppercase_matches():
    matches = match_symbols("AMZN options skew flips bullish", market="us")
    assert any(m.symbol == "AMZN" for m in matches)


@pytest.mark.unit
def test_kr_samsung_korean_alias_matches_005930():
    matches = match_symbols("삼성전자 1분기 실적 호조, 삼전 강세", market="kr")
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_kr_samjeon_short_alias_matches():
    matches = match_symbols("삼전 매수 우위", market="kr")
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_crypto_bitcoin_alias_matches_btc():
    matches = match_symbols("비트코인 7만달러 회복, Bitcoin ETF 유입 지속", market="crypto")
    symbols = [m.symbol for m in matches]
    assert "BTC" in symbols


@pytest.mark.unit
def test_crypto_krw_pair_matches_btc():
    matches = match_symbols("KRW-BTC 거래대금 급증", market="crypto")
    assert any(m.symbol == "BTC" for m in matches)


@pytest.mark.unit
def test_market_filter_excludes_other_markets():
    matches = match_symbols("Amazon, 삼성전자 모두 강세", market="us")
    symbols = {m.symbol for m in matches}
    assert "AMZN" in symbols
    assert "005930" not in symbols  # market=us must filter KR


@pytest.mark.unit
def test_no_match_returns_empty_list():
    assert match_symbols("Random unrelated content about weather", market="us") == []


@pytest.mark.unit
def test_us_word_boundary_no_false_positive_for_amd_in_amid():
    # "amid" must NOT match "AMD"
    matches = match_symbols("Stocks rally amid easing inflation", market="us")
    assert not any(m.symbol == "AMD" for m in matches)


@pytest.mark.unit
def test_match_for_article_uses_title_summary_keywords():
    matches = match_symbols_for_article(
        title="실적발표",
        summary=None,
        keywords=["삼성전자", "반도체"],
        market="kr",
    )
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_match_returns_sorted_unique_by_symbol():
    matches = match_symbols("Amazon Amazon AMZN keeps rising", market="us")
    amzn_matches = [m for m in matches if m.symbol == "AMZN"]
    assert len(amzn_matches) == 1  # deduped
    assert isinstance(matches[0], SymbolMatch)
