from __future__ import annotations

from app.services.n8n_daily_brief_service import build_cio_pending_decision
from tests.fixtures.cio_briefing import plan_v2_section_f_context


def test_target_is_ignored_when_runway_deficit_dominates() -> None:
    ctx = plan_v2_section_f_context(
        exchange_krw=83_318,
        next_obligation={
            "date": "2026-04-29",
            "days_remaining": 12,
            "cash_needed_until": 2_500_000,
        },
        board_response={
            "amount": 1_200_000,
            "target": "BTC",
            "funding_intent": "new_buy",
            "manual_cash_verified": True,
        },
    )

    render = build_cio_pending_decision(ctx)

    assert render.funding_intent == "runway_recovery"
    assert "운영 연료" in render.text
    assert "신규 risk budget 후보" not in render.text


def test_verified_target_with_sufficient_runway_enters_new_buy() -> None:
    ctx = plan_v2_section_f_context(
        exchange_krw=1_200_000,
        unverified_cap={
            "amount": 10_000_000,
            "verified_by_boss_today": True,
            "stale_warning": False,
        },
        next_obligation={
            "date": "2026-04-29",
            "days_remaining": 12,
            "cash_needed_until": 1_200_000,
        },
        board_response={
            "amount": 1_200_000,
            "target": "BTC",
            "funding_intent": "runway_recovery",
            "manual_cash_verified": True,
        },
    )

    render = build_cio_pending_decision(ctx)

    assert render.funding_intent == "new_buy"
    assert "신규 risk budget 후보" in render.text
    assert "운영 연료" not in render.text
