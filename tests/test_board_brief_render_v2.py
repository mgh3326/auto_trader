from __future__ import annotations

from app.schemas.n8n.board_brief import BoardBriefContext, BoardFundingResponse
from app.services.n8n_daily_brief_service import (
    build_cio_pending_decision,
    build_tc_preliminary,
    resolve_funding_intent,
)


def _v2_context() -> BoardBriefContext:
    return BoardBriefContext.model_validate(
        {
            "exchange_krw": 1_000_000,
            "unverified_cap": {
                "amount": 5_000_000,
                "verified_by_boss_today": False,
                "stale_warning": True,
            },
            "daily_burn_krw": 100_000,
            "next_obligation": {
                "date": "2026-04-24",
                "days_remaining": 7,
                "cash_needed_until": 2_500_000,
            },
            "tier_scenarios": [
                {
                    "label": "T1",
                    "deposit_amount": 1_500_000,
                    "target_exchange_krw": 2_500_000,
                    "buffer_days": 25,
                    "cushion_after_obligation": 0,
                },
                {
                    "label": "T2",
                    "deposit_amount": 3_500_000,
                    "target_exchange_krw": 4_500_000,
                    "buffer_days": 45,
                    "cushion_after_obligation": 2_000_000,
                },
            ],
            "holdings": [
                {"symbol": "BTC", "current_krw_value": 7_000_000, "dust": False},
                {"symbol": "DOGE", "current_krw_value": 3_000, "dust": True},
                {"symbol": "XRP", "current_krw_value": 2_000, "dust": True},
            ],
            "dust_items": [
                {"symbol": "DOGE", "current_krw_value": 3_000},
                {"symbol": "XRP", "current_krw_value": 2_000},
            ],
        }
    )


def _context_with_updates(**updates: object) -> BoardBriefContext:
    return BoardBriefContext.model_validate(
        _v2_context().model_dump(mode="json") | updates
    )


def test_resolve_funding_intent_prefers_runway_when_obligation_dominates() -> None:
    ctx = _v2_context()
    board_response = BoardFundingResponse(
        amount=1_000_000,
        target="BTC",
        funding_intent="new_buy",
        manual_cash_verified=True,
    )

    intent, lines = resolve_funding_intent(ctx, board_response)

    assert intent == "runway_recovery"
    assert lines == [
        "- 이번 1,000,000 원은 **운영 연료** 로 귀속 — coinmoogi DCA 7 일 지속분 + 만기 cushion.",
        "- 신규 매수 여력으로 전용 금지. G2 에서 차단.",
    ]


def test_resolve_funding_intent_allows_verified_target_as_new_buy() -> None:
    ctx = _context_with_updates(
        next_obligation={
            "date": "2026-04-24",
            "days_remaining": 7,
            "cash_needed_until": 1_500_000,
        }
    )
    board_response = BoardFundingResponse(
        amount=1_000_000,
        target="BTC",
        funding_intent="runway_recovery",
        manual_cash_verified=True,
    )

    intent, lines = resolve_funding_intent(ctx, board_response)

    assert intent == "new_buy"
    assert lines == [
        "- 이번 1,000,000 원은 G3 (runway/obligation) 통과 후 신규 risk budget 후보.",
        "- 이 경우에도 G4 시장 regime → G5 volatility halt → G6 보조지표 통과 여부 추가 판정 필요.",
    ]


def test_resolve_funding_intent_defaults_unverified_target_to_runway() -> None:
    ctx = _context_with_updates(
        next_obligation={
            "date": "2026-04-24",
            "days_remaining": 7,
            "cash_needed_until": 1_500_000,
        }
    )
    board_response = BoardFundingResponse(
        amount=1_000_000,
        target="BTC",
        funding_intent="new_buy",
        manual_cash_verified=False,
    )

    intent, lines = resolve_funding_intent(ctx, board_response)

    assert intent == "runway_recovery"
    assert lines == [
        "- 이번 1,000,000 원은 **운영 연료** 로 귀속 — coinmoogi DCA 7 일 지속분 + 만기 cushion.",
        "- 신규 매수 여력으로 전용 금지. G2 에서 차단.",
    ]


def test_tc_preliminary_renders_v2_cash_dust_and_tier_sections() -> None:
    render = build_tc_preliminary(_v2_context())
    text = render.text

    assert text.startswith(
        "📊 TC Preliminary — 입금 약속 반영 시나리오 (pledged, 거래소 미반영)"
    )
    assert text.index("거래소 KRW") < text.index("미확인 cap (보스 확인 전)")
    assert text.index("미확인 cap (보스 확인 전)") < text.index(
        "일일 소진 (daily_burn)"
    )
    assert text.index("일일 소진 (daily_burn)") < text.index("다음 의무")
    assert text.index("다음 의무") < text.index("runway 산식")
    assert "runway 산식: 1,000,000 KRW / 100,000 KRW = 10.00일" in text
    assert "🧹 Dust 2종목 · 합계 5,000 KRW · 포트폴리오 0.07%" in text
    assert (
        "deposit_amount | next_obligation | cash_needed_until | "
        "cushion_after_obligation | target_exchange_krw | buffer_days (보조)"
    ) in text
    assert "1,500,000 KRW | 2026-04-24 / D-7 | 2,500,000 KRW" in text
    assert "**A 와 B 는 상호배타 아님 — 병행 가능.**" in text


def test_cio_pending_injects_one_g2_phrase_set_and_question_tags() -> None:
    ctx = _context_with_updates(
        next_obligation={
            "date": "2026-04-24",
            "days_remaining": 7,
            "cash_needed_until": 1_500_000,
        },
        board_response={
            "amount": 1_000_000,
            "target": "BTC",
            "funding_intent": "new_buy",
            "manual_cash_verified": True,
        },
    )

    render = build_cio_pending_decision(ctx)
    text = render.text

    assert render.funding_intent == "new_buy"
    assert text.count("신규 risk budget 후보") == 1
    assert "운영 연료" not in text
    assert "질문 (Step 1 답변 반영 — 재질문 아님)" in text
    assert "[funding-confirmation]" in text
    assert "[action]" in text
