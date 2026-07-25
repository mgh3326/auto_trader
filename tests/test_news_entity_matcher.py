"""Unit tests for the deterministic news entity matcher (ROB-130)."""

from __future__ import annotations

import pytest

from app.services.news_entity_matcher import (
    SymbolMatch,
    match_kr_universe_symbols,
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
    matches = match_symbols(
        "비트코인 7만달러 회복, Bitcoin ETF 유입 지속", market="crypto"
    )
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
@pytest.mark.parametrize("field", ["title", "summary", "keywords"])
def test_match_for_article_strips_url_metadata_before_matching(field: str):
    kwargs = {
        "title": "마켓레이더 오전 자료",
        "summary": "증권사 시장 요약",
        "keywords": None,
        "market": "kr",
    }
    metadata = "canonical_url:https://finance.naver.com/market_info_read.naver"
    if field == "title":
        kwargs["title"] = metadata
    elif field == "summary":
        kwargs["summary"] = metadata
    else:
        kwargs["keywords"] = [metadata]

    matches = match_symbols_for_article(**kwargs)

    assert not any(m.symbol == "035420" for m in matches)


@pytest.mark.unit
def test_match_for_article_drops_malformed_url_like_metadata_without_crashing():
    matches = match_symbols_for_article(
        title="마켓레이더 오전 자료",
        summary="증권사 시장 요약",
        keywords=[
            "canonical_url:https://finance.naver.com/[bad",
            "source_url:http://[malformed",
            "https://finance.naver.com/[broken",
            "[malformed",
            "foo[bar.com",
        ],
        market="kr",
    )

    assert not any(m.symbol == "035420" for m in matches)


@pytest.mark.unit
def test_match_for_article_keeps_naver_origin_metadata_separate_from_naver_corp():
    matches = match_symbols_for_article(
        title="반도체 업황 회복에 삼성전자 강세",
        summary="증권사 시장 요약",
        keywords=[
            "source:browser_naver_research",
            "canonical_url:https://finance.naver.com/research/company_read.naver?foo=[bad",
        ],
        market="kr",
    )
    symbols = {m.symbol for m in matches}

    assert "005930" in symbols
    assert "035420" not in symbols


@pytest.mark.unit
def test_match_for_article_still_matches_naver_when_article_mentions_company():
    matches = match_symbols_for_article(
        title="네이버 AI 투자 확대",
        summary="플랫폼 기업 실적 개선 기대",
        keywords=["canonical_url:https://finance.naver.com/news/mainnews.naver"],
        market="kr",
    )

    assert any(m.symbol == "035420" for m in matches)


@pytest.mark.unit
def test_match_returns_sorted_unique_by_symbol():
    matches = match_symbols("Amazon Amazon AMZN keeps rising", market="us")
    amzn_matches = [m for m in matches if m.symbol == "AMZN"]
    assert len(amzn_matches) == 1  # deduped
    assert isinstance(matches[0], SymbolMatch)


@pytest.mark.unit
def test_match_for_article_uses_summary():
    matches = match_symbols_for_article(
        title="Market update",
        summary="Amazon AWS revenue beats expectations",
        keywords=None,
        market="us",
    )
    assert any(m.symbol == "AMZN" for m in matches)


@pytest.mark.unit
def test_market_none_returns_all_markets():
    matches = match_symbols("Amazon, 삼성전자 모두 강세", market=None)
    symbols = {m.symbol for m in matches}
    assert "AMZN" in symbols
    assert "005930" in symbols


@pytest.mark.unit
def test_match_results_sorted_by_market_then_symbol():
    matches = match_symbols("Amazon, Google rise; 삼성전자 강세", market=None)
    keys = [(m.market, m.symbol) for m in matches]
    assert keys == sorted(keys)


@pytest.mark.unit
def test_match_for_article_with_market_none_finds_us_alias_in_korean_text():
    """ROB-172 contract: callers that omit `market` must search ALL_ALIASES
    so a KR-feed article carrying `엔비디아` resolves to NVDA/us.
    """
    matches = match_symbols_for_article(
        title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
        summary="엔비디아의 차세대 GPU 발표가 국내 반도체 공급망에 호재로 작용",
        keywords=["엔비디아", "반도체"],
        market=None,
    )
    by_symbol = {m.symbol: m for m in matches}
    assert "NVDA" in by_symbol, f"expected NVDA in matches, got {sorted(by_symbol)}"
    assert by_symbol["NVDA"].market == "us"
    assert by_symbol["NVDA"].reason == "alias_dict"
    assert by_symbol["NVDA"].matched_term == "엔비디아"


# ---------------------------------------------------------------------------
# match_kr_universe_symbols (ROB-916)
# ---------------------------------------------------------------------------

_UNIVERSE = [
    ("042660", "한화오션"),
    ("000880", "한화"),
    ("005930", "삼성전자"),
    ("000810", "삼성화재"),
    ("017670", "SK텔레콤"),
    ("034730", "SK"),
]


@pytest.mark.unit
def test_kr_universe_matches_hanwha_ocean_by_full_name():
    """ROB-916 evidence case: 042660 title mentions must resolve to 한화오션."""
    matches = match_kr_universe_symbols(
        "한화오션, 3943억 규모 VLCC 2척 수주", _UNIVERSE
    )
    symbols = {m.symbol for m in matches}
    assert "042660" in symbols


@pytest.mark.unit
def test_kr_universe_overlap_guard_suppresses_shorter_substring_match():
    """부분 문자열 오탐 가드: "한화오션" 안의 "한화"(000880)는 별도 매치되면 안 됨."""
    matches = match_kr_universe_symbols(
        "한화오션, 3943억 규모 VLCC 2척 수주", _UNIVERSE
    )
    symbols = {m.symbol for m in matches}
    assert "000880" not in symbols
    assert symbols == {"042660"}


@pytest.mark.unit
def test_kr_universe_samsung_electronics_and_fire_insurance_resolve_independently():
    """삼성전자/삼성화재 혼동 가드 — 둘 다 온전한 회사명으로 독립 매치돼야 함."""
    matches = match_kr_universe_symbols(
        "삼성전자 실적 호조, 삼성화재는 배당 확대 검토", _UNIVERSE
    )
    symbols = {m.symbol for m in matches}
    assert symbols == {"005930", "000810"}


@pytest.mark.unit
def test_kr_universe_bare_short_name_still_matches_without_overlap():
    matches = match_kr_universe_symbols("한화 그룹 지배구조 개편 검토", _UNIVERSE)
    symbols = {m.symbol for m in matches}
    assert "000880" in symbols


@pytest.mark.unit
def test_kr_universe_sk_telecom_mention_does_not_also_tag_bare_sk_holding():
    matches = match_kr_universe_symbols(
        "SK텔레콤, 실적 안정·배당 정상화 기대", _UNIVERSE
    )
    symbols = {m.symbol for m in matches}
    assert symbols == {"017670"}


@pytest.mark.unit
def test_kr_universe_no_match_returns_empty_list():
    assert match_kr_universe_symbols("오늘 날씨는 맑음", _UNIVERSE) == []


@pytest.mark.unit
def test_kr_universe_empty_text_returns_empty_list():
    assert match_kr_universe_symbols("", _UNIVERSE) == []


@pytest.mark.unit
def test_kr_universe_match_reason_is_kr_symbol_universe_name():
    matches = match_kr_universe_symbols("한화오션 수주 소식", _UNIVERSE)
    assert matches
    assert all(m.reason == "kr_symbol_universe_name" for m in matches)
    assert all(m.market == "kr" for m in matches)
