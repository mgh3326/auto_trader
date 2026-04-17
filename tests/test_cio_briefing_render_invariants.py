from __future__ import annotations

from collections.abc import Callable

import pytest

from app.schemas.n8n.board_brief import GateResult
from app.services.n8n_daily_brief_service import (
    RenderInvariantError,
    build_cio_pending_decision,
    validate_render_invariants,
)
from tests.fixtures.cio_briefing import plan_v2_section_f_context, replace_once


def _append_immediate_buy(text: str) -> str:
    return text + "\nCIO 권고 (1) 즉시 매수"


def _append_fail_closed_anchor(text: str) -> str:
    return text + "\n⚠️ synthetic 누락 — 테스트 anchor"


def _duplicate_dust_line(text: str) -> str:
    dust_line = next(line for line in text.splitlines() if line.startswith("🧹 Dust"))
    return text + f"\n{dust_line}"


INVARIANT_CASES: list[tuple[str, Callable[[str], str], str]] = [
    (
        "funding_rows",
        replace_once("- 거래소 KRW:", "- 거래소 원화:"),
        "funding_rows",
    ),
    (
        "runway_excludes_unverified_cap",
        replace_once(
            "runway 산식: 83,318 KRW / 80,000 KRW = 1.04일",
            "runway 산식: 83,318 KRW + 10,000,000 KRW / 80,000 KRW = 126.04일",
        ),
        "runway_excludes_unverified_cap",
    ),
    (
        "ab_anchor_triple",
        replace_once(
            "경로 A (입금) 와 경로 B (현물 부분매도) 는 **상호배타 아님**. 병행 가능합니다.",
            "경로 A와 경로 B를 검토합니다.",
        ),
        "ab_anchor_triple",
    ),
    (
        "g2_phrase_exactly_one",
        lambda text: (
            text
            + "\n- 이번 1,200,000 원은 G3 (runway/obligation) 통과 후 신규 risk budget 후보."
        ),
        "g2_phrase_exactly_one",
    ),
    (
        "immediate_buy_requires_g2_g5_pass",
        _append_immediate_buy,
        "immediate_buy_requires_g2_g5_pass",
    ),
    ("dust_aggregate", _duplicate_dust_line, "dust_aggregate"),
    ("fail_closed_anchor", _append_fail_closed_anchor, "fail_closed_anchor"),
]


@pytest.mark.parametrize(
    ("case_name", "mutate", "violation_code"),
    INVARIANT_CASES,
    ids=[case[0] for case in INVARIANT_CASES],
)
def test_render_invariant_positive_and_negative_fixtures(
    case_name: str,
    mutate: Callable[[str], str],
    violation_code: str,
) -> None:
    ctx = plan_v2_section_f_context()
    positive = build_cio_pending_decision(ctx).text
    if case_name == "immediate_buy_requires_g2_g5_pass":
        positive = _append_immediate_buy(positive)
        assert validate_render_invariants(positive, ctx, phase="cio_pending") == []
        negative_ctx = ctx.model_copy(
            update={
                "gate_results": ctx.gate_results
                | {"G4": GateResult(status="fail", detail="target below MA20")}
            }
        )
    else:
        assert validate_render_invariants(positive, ctx, phase="cio_pending") == []
        negative_ctx = ctx

    with pytest.raises(RenderInvariantError) as exc_info:
        build_cio_pending_decision(negative_ctx, text_postprocessor=mutate)

    assert [violation.code for violation in exc_info.value.violations] == [
        violation_code
    ]


def test_full_suite_pass_plan_v2_section_f_sample() -> None:
    ctx = plan_v2_section_f_context()

    render = build_cio_pending_decision(ctx)

    assert validate_render_invariants(render.text, ctx, phase="cio_pending") == []
