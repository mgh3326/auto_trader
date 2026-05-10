"""ROB-169 — KR news investment relevance unit tests."""

from __future__ import annotations

import json
import pathlib

import pytest

from app.services.kr_news_relevance_service import (
    KrNewsRelevance,
    score_kr_news_article,
    user_facing_kr_category,
)


def test_empty_article_returns_low_relevance_with_no_matches():
    relevance = score_kr_news_article(
        {"title": "", "summary": "", "feed_source": "", "keywords": []}
    )

    assert isinstance(relevance, KrNewsRelevance)
    assert relevance.score == 0
    assert relevance.bucket == "low"
    assert relevance.category is None
    assert relevance.include_in_briefing is False
    assert relevance.matched_terms == []
    assert relevance.noise_reason == "low_kr_relevance"


def test_society_crime_kr_article_no_symbol_is_dropped_with_kr_crime_reason():
    relevance = score_kr_news_article(
        {
            "title": "'광주 여고생 살해' 피의자 사이코패스 검사 결과 공개된다",
            "summary": "검찰은 살해 피의자에 대한 사이코패스 평가 결과를 곧 공개할 예정이다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["사회", "범죄", "피의자"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason == "kr_crime"
    assert relevance.score < 35
    assert any(t in relevance.matched_terms for t in ("살해", "피의자", "사이코패스"))


def test_celebrity_scandal_kr_article_is_dropped_as_kr_society():
    relevance = score_kr_news_article(
        {
            "title": "유명 아이돌 열애설 인정… 소속사 공식 입장",
            "summary": "스캔들로 번진 사생활 이슈에 팬들이 충격을 받았다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["연예"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason == "kr_society"


def test_traffic_accident_kr_article_with_no_invest_signal_is_dropped():
    relevance = score_kr_news_article(
        {
            "title": "고속도로 추돌 사고로 3중 추돌… 1명 사망",
            "summary": "경찰은 음주운전 가능성도 조사 중이다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["사고"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is False
    assert relevance.noise_reason in ("kr_society", "kr_crime", "kr_no_invest_signal")


def test_kospi_market_summary_no_symbol_is_included():
    relevance = score_kr_news_article(
        {
            "title": "코스피 2600 돌파… 외국인 순매수 지속",
            "summary": "코스닥도 동반 상승하며 투자심리가 개선되었다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["코스피", "코스닥", "주식"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is True
    assert relevance.score >= 35
    assert relevance.category in ("kr_index", "kr_macro")
    assert "코스피" in relevance.matched_terms or "코스닥" in relevance.matched_terms


def test_kr_ipo_article_no_symbol_is_included():
    relevance = score_kr_news_article(
        {
            "title": "이달 공모주 IPO 대어 상장… 청약 경쟁률 역대 최고",
            "summary": "유가증권시장 상장 일정과 청약 일정이 확정되었다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["공모주", "상장"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is True
    assert relevance.score >= 35


def test_interest_rate_article_no_symbol_is_included():
    relevance = score_kr_news_article(
        {
            "title": "한국은행 기준금리 동결… 시장 반응은?",
            "summary": "금융통화위원회는 기준금리를 3.5%로 유지하기로 결정했다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["금리", "한국은행"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is True
    assert relevance.score >= 35


def test_semiconductor_policy_article_no_symbol_is_included():
    relevance = score_kr_news_article(
        {
            "title": "정부, 반도체 산업 지원금 확대 발표… 삼성·SK 수혜 기대",
            "summary": "보조금과 법인세 감면 혜택으로 파운드리 투자가 늘어날 전망이다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": ["반도체", "정책"],
            "stock_symbol": None,
        }
    )

    assert relevance.include_in_briefing is True
    assert relevance.score >= 35


def test_article_with_stock_symbol_always_included():
    relevance = score_kr_news_article(
        {
            "title": "삼성전자 1분기 실적 발표",
            "summary": "영업이익이 전년 대비 크게 증가했다.",
            "feed_source": "browser_naver_mainnews",
            "keywords": [],
            "stock_symbol": "005930",
        }
    )

    assert relevance.include_in_briefing is True
    assert relevance.score >= 30
    assert relevance.category == "kr_symbol"


def test_user_facing_kr_category_maps_correctly():
    assert user_facing_kr_category("kr_macro") == "kr_macro"
    assert user_facing_kr_category("kr_index") == "kr_index"
    assert user_facing_kr_category("kr_industry") == "kr_industry"
    assert user_facing_kr_category(None) is None


def test_empty_article_noise_reason_is_low_kr_relevance():
    relevance = score_kr_news_article(
        {"title": "", "summary": "", "feed_source": "", "keywords": []}
    )
    assert relevance.noise_reason == "low_kr_relevance"


_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "kr_news_relevance"


def _load_cases(name: str) -> list[dict]:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _load_cases("positive_market_wide.json"), ids=lambda c: c["id"])
def test_positive_market_wide_kr_articles_are_included(case):
    relevance = score_kr_news_article(case)

    assert relevance.include_in_briefing is True, (
        f"{case['id']!r} expected included; got noise_reason={relevance.noise_reason!r}, "
        f"score={relevance.score}, matched={relevance.matched_terms}"
    )
    if "expected_category" in case:
        assert relevance.category == case["expected_category"]


@pytest.mark.parametrize("case", _load_cases("negative_society_crime.json"), ids=lambda c: c["id"])
def test_negative_society_crime_kr_articles_are_excluded(case):
    relevance = score_kr_news_article(case)

    assert relevance.include_in_briefing is False, (
        f"{case['id']!r} expected excluded; got score={relevance.score}, matched={relevance.matched_terms}"
    )
    if "expected_noise_reason" in case:
        assert relevance.noise_reason == case["expected_noise_reason"]


@pytest.mark.parametrize("case", _load_cases("borderline.json"), ids=lambda c: c["id"])
def test_borderline_kr_articles_lean_to_expected_include(case):
    relevance = score_kr_news_article(case)
    assert relevance.include_in_briefing is case["expected_include"], (
        f"{case['id']!r} expected include={case['expected_include']}; "
        f"got include={relevance.include_in_briefing}, noise_reason={relevance.noise_reason!r}, "
        f"score={relevance.score}"
    )
