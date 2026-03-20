from __future__ import annotations

import pytest

from app.analysis.prompt import build_json_prompt, build_prompt
from tests._analysis_support import (
    build_analysis_sample_df,
    build_minute_candles,
    sample_fundamental_info,
    sample_position_info,
)


@pytest.mark.unit
def test_build_prompt_includes_optional_sections() -> None:
    prompt = build_prompt(
        df=build_analysis_sample_df(),
        ticker="005930",
        stock_name="삼성전자",
        currency="₩",
        unit_shares="주",
        fundamental_info=sample_fundamental_info(),
        position_info=sample_position_info(),
        minute_candles=build_minute_candles(),
    )

    assert "삼성전자(005930)" in prompt
    assert "[기본 정보]" in prompt
    assert "[보유 자산 정보]" in prompt
    assert "[단기(분) 캔들 정보]" in prompt
    assert "[가격 지표]" in prompt
    assert "[거래량 지표]" in prompt
    assert "[최근 10거래일 (날짜·종가·거래량)]" in prompt


@pytest.mark.unit
def test_build_json_prompt_appends_json_schema_instructions() -> None:
    prompt = build_json_prompt(
        df=build_analysis_sample_df(),
        ticker="AAPL",
        stock_name="Apple",
        currency="$",
        unit_shares="주",
        fundamental_info=sample_fundamental_info(),
        position_info=sample_position_info(),
        minute_candles=build_minute_candles(),
    )

    assert "반드시 아래 JSON 형식으로만 답변하세요:" in prompt
    assert '"decision": "매수/관망/매도 중 하나"' in prompt
    assert '"price_analysis"' in prompt
    assert "다른 설명 없이 오직 JSON만 출력하세요." in prompt
