from __future__ import annotations

from app.services.n8n_daily_brief_service import (
    build_cio_pending_decision,
    build_tc_preliminary,
    validate_render_invariants,
)
from tests.fixtures.cio_briefing import plan_v2_section_f_context


def test_plan_v2_section_g_checklist() -> None:
    ctx = plan_v2_section_f_context()

    tc_render = build_tc_preliminary(ctx)
    cio_render = build_cio_pending_decision(ctx)
    combined_text = tc_render.text + "\n" + cio_render.text

    checks = {
        "framing_box_top": tc_render.text.splitlines()[2].startswith(
            "요약: 경로 A·B 병행 가능."
        ),
        "cash_rows_separate": "거래소 KRW:" in tc_render.text
        and "미확인 cap (보스 확인 전):" in tc_render.text,
        "unverified_flags": all(
            flag in tc_render.text
            for flag in [
                "미확인 cap (보스 확인 전): 10,000,000 KRW",
                "stale_warning",
            ]
        ),
        "daily_burn_rendered": "일일 소진 (daily_burn): 80,000 KRW" in tc_render.text,
        "dust_excluded_from_actionable_table": "APT: 654 KRW" not in combined_text,
        "dust_footnote_one_line": sum(
            1 for line in tc_render.text.splitlines() if line.startswith("🧹 Dust")
        )
        == 1,
        "three_tier_obligation_table": all(
            item in tc_render.text
            for item in ["516,682 KRW", "1,116,682 KRW", "2,316,682 KRW"]
        )
        and "cushion_after_obligation" in tc_render.text,
        "default_rule_matches_sample": cio_render.funding_intent == "runway_recovery"
        and "운영 연료" in cio_render.text,
        "path_a_b_split": "경로 A:" in tc_render.text and "경로 B:" in tc_render.text,
        "two_block_followup": "TC Preliminary" in tc_render.text
        and "CIO Pending Decision" in cio_render.embed["title"],
        "g1_g6_order": all(f"- G{idx}:" in cio_render.text for idx in range(1, 7))
        and cio_render.text.index("- G1:") < cio_render.text.index("- G6:"),
        "board_questions_split": "[funding-confirmation]" in cio_render.text
        and "[action]" in cio_render.text
        and cio_render.text.index("[funding-confirmation]")
        < cio_render.text.index("[action]"),
    }

    assert validate_render_invariants(tc_render.text, ctx, phase="tc_preliminary") == []
    assert validate_render_invariants(cio_render.text, ctx, phase="cio_pending") == []
    failed_checks = {name: value for name, value in checks.items() if not value}
    assert failed_checks == {}
