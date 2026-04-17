from __future__ import annotations

import re

import pytest

from app.services.cio_coin_briefing.prompts import gate_phrases, render_invariants

EXPECTED_STRINGS = [
    "G2_LINE_RUNWAY",
    "G2_LINE_NEW_BUDGET",
    "G2_RECOMMENDATION_FIXED",
    "HARD_GATE_REMINDER",
    "FRAMING_AB_PATH_NON_EXCLUSIVE",
    "PATH_SECTION_AB_REPEAT",
    "BOARD_QUESTIONS_TEMPLATE",
]

EXPECTED_LINE_LISTS = [
    "G2_RUNWAY_FUEL_LINES",
    "G2_NEW_BUDGET_LINES",
]


@pytest.mark.unit
def test_gate_phrase_string_constants_exist_and_are_non_empty():
    for name in EXPECTED_STRINGS:
        value = getattr(gate_phrases, name)
        assert isinstance(value, str), name
        assert value.strip(), name


@pytest.mark.unit
def test_gate_phrase_line_lists_exist_and_are_non_empty():
    for name in EXPECTED_LINE_LISTS:
        value = getattr(gate_phrases, name)
        assert isinstance(value, list), name
        assert value, name
        assert all(isinstance(line, str) and line.strip() for line in value), name


@pytest.mark.unit
def test_gate_phrase_placeholders_survive_copy():
    assert "{hard_gate_symbol}" in gate_phrases.BOARD_QUESTIONS_TEMPLATE
    assert "{quantity_range}" in gate_phrases.BOARD_QUESTIONS_TEMPLATE
    assert "{amount}" in "\n".join(gate_phrases.G2_RUNWAY_FUEL_LINES)
    assert "{days}" in "\n".join(gate_phrases.G2_RUNWAY_FUEL_LINES)
    assert "{amount}" in "\n".join(gate_phrases.G2_NEW_BUDGET_LINES)


@pytest.mark.unit
def test_forbidden_patterns_are_compiled_regexes():
    assert isinstance(gate_phrases.FORBIDDEN_PATTERNS, list)
    assert len(gate_phrases.FORBIDDEN_PATTERNS) == 11
    assert all(
        isinstance(pattern, re.Pattern) for pattern in gate_phrases.FORBIDDEN_PATTERNS
    )


@pytest.mark.unit
def test_render_invariants_exist_with_callable_stubs():
    invariants = render_invariants.RENDER_INVARIANTS
    assert isinstance(invariants, list)
    assert len(invariants) == 9

    invariant_by_name = {invariant.name: invariant for invariant in invariants}
    assert (
        invariant_by_name["dust_aggregate_line"].description
        == "§4 통합 포트폴리오 쏠림 말미에 'Dust aggregate: N symbols / total KRW / portfolio pct' 한 줄 존재"
    )
    assert (
        invariant_by_name["fail_closed_anchor_routing"].description
        == "Missing-field fail-closed anchor (⚠️ ... 누락 ...) 가 등장하면 보드 채널 전송 금지, 운영팀 에스컬레이션 라우팅"
    )

    for invariant in invariants:
        assert isinstance(invariant, render_invariants.Invariant)
        assert invariant.name.strip()
        assert invariant.description.strip()
        assert callable(invariant.check)
        with pytest.raises(NotImplementedError):
            invariant.check("rendered briefing markdown")
