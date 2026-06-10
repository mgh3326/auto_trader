"""Deterministic relevance hints builder (ROB-491 — non-authoritative)."""

from __future__ import annotations

import pytest

from app.services.symbol_news_relevance import build_relevance_hints


@pytest.mark.unit
def test_alias_match_recorded_as_hint() -> None:
    hints = build_relevance_hints(
        symbol="035420",
        market="kr",
        title="네이버 D2SF, AI 보안 스타트업에 신규 투자",
    )
    assert hints is not None
    assert "네이버" in hints["alias_match"]


@pytest.mark.unit
def test_symbol_code_in_text_counts_as_alias_match() -> None:
    hints = build_relevance_hints(
        symbol="035420", market="kr", title="035420 거래량 급증"
    )
    assert hints is not None
    assert "035420" in hints["alias_match"]


@pytest.mark.unit
def test_no_signals_returns_none() -> None:
    assert (
        build_relevance_hints(
            symbol="035420",
            market="kr",
            title="판다 아이바오, 셋째 출산",
        )
        is None
    )


@pytest.mark.unit
def test_invest_keywords_recorded() -> None:
    hints = build_relevance_hints(
        symbol="035420",
        market="kr",
        title="젠슨 황이 만나고 간 대기업들, 'AI 보안' 스타트업에 투자",
    )
    assert hints is not None
    assert hints.get("invest_keywords")


@pytest.mark.unit
def test_blacklist_api_is_gone() -> None:
    """하드코딩 노이즈 분기 폐기 — 사례 후행적 분기 재도입 방지 가드."""
    import app.services.symbol_news_relevance as mod

    assert not hasattr(mod, "_KR_TITLE_NOISE_TERMS")
    assert not hasattr(mod, "classify_symbol_news_relevance")
