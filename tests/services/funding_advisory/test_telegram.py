from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.services.funding_advisory.telegram import (
    CARD_HEADER,
    HANDOFF_LABEL,
    JIT_DECLARED_DISCLAIMER,
    JIT_DEPOSIT_BASIS,
    JIT_DEPOSIT_HEADER,
    deliver_claimed_advisory,
    render_funding_advisory_card,
)


def advisory_view() -> dict:
    route_ids = [
        "EXTERNAL_PARKING_KRW",
        "USD_CONVERSION",
        "CREDIT_LINE_SHORT_TERM",
        "PROFITABLE_TRIM",
        "LOSS_CUT_ROTATION",
    ]
    return {
        "advisory_id": "26c5cbf1-ab53-4ac8-bdbb-979a13e12f03",
        "revision_id": "b320f8f4-934f-4d81-bc3c-e8cc25a80a5d",
        "target": {
            "market": "crypto",
            "account_mode": "upbit",
            "symbol": "KRW-BTC",
            "currency": "KRW",
        },
        "trigger": {
            "gate_name": "crypto_non_funding_gate",
            "gate_version": "crypto-gate.v1",
            "gate_evaluated_at": "2026-08-15T09:00:00+09:00",
        },
        "need": {
            "required_cash": "100000",
            "target_buying_power": "40000",
            "shortfall": "60000",
            "other_pending_required": "20000",
            "reserved_cash": "10000",
            "operational_gap_including_other_pending": "90000",
        },
        "routes": [
            {
                "route_id": route_id,
                "label": route_id,
                "route_fundable_amount": "60000"
                if route_id == "EXTERNAL_PARKING_KRW"
                else None,
                "comparison": "unavailable",
                "deadline_status": "unknown",
            }
            for route_id in route_ids
        ],
        "combination": {"remaining_gap": "60000", "selected": False},
    }


def test_card_is_explicitly_non_executing_and_discloses_pending_cash() -> None:
    card = render_funding_advisory_card(advisory_view())

    assert card.text.startswith(CARD_HEADER)
    assert "이 후보 shortfall: 60000 KRW" in card.text
    assert "다른 pending 매수: 20000 KRW" in card.text
    assert "pending/reserved 포함 운영상 gap: 90000 KRW" in card.text
    assert "자동 실행 없음" in card.text
    assert "조달 완료 판정은 target broker buying power 재조회 후" in card.text


def test_trim_and_loss_rows_use_non_cta_handoff_label() -> None:
    card = render_funding_advisory_card(advisory_view())

    assert card.text.count(HANDOFF_LABEL) == 2
    assert "매도 제안 검토" not in card.text


def test_card_buttons_are_reference_only_urls_without_callbacks() -> None:
    card = render_funding_advisory_card(advisory_view())
    buttons = [
        button for row in card.inline_keyboard["inline_keyboard"] for button in row
    ]

    assert {button["text"] for button in buttons} == {
        "상세 보기 · 읽기 전용",
        "외부 잔고 선언 갱신 · 돈 이동 아님",
    }
    assert all("url" in button for button in buttons)
    assert all("callback_data" not in button for button in buttons)


@pytest.mark.asyncio
async def test_unclaimed_page_view_never_calls_telegram_notifier() -> None:
    view = advisory_view()
    view["delivery"] = {"action": "none", "reason": "page_refresh_no_delivery"}
    notifier = AsyncMock()

    result = await deliver_claimed_advisory(
        view,
        now=datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
        notifier=notifier,
    )

    assert result == {"status": "not_sent", "reason": "page_refresh_no_delivery"}
    notifier.send_approval_message.assert_not_called()
    notifier.edit_message.assert_not_called()


def deferred_view() -> dict:
    view = advisory_view()
    view["jit_funding"] = {
        "disposition": "deferred_with_condition",
        "condition": {
            "kind": "operator_deposit_to_target_account",
            "deposit_amount": "60000",
            "deposit_amount_basis": "candidate_shortfall",
            "currency": "KRW",
            "operational_gap_amount": "90000",
            "declared_cover": "sufficient",
            "declared_total_disclosure_only": "640000",
            "satisfied_by": "target_broker_buying_power_reobservation",
        },
        "next_step": "operator_deposit_then_reevaluate",
        "rejected_for_insufficient_cash": False,
        "creates_proposal": False,
        "executes_money_movement": False,
        "declared_cash_counted_toward_buying_power": False,
        "declared_cash_is_display_evidence_only": True,
    }
    return view


def test_card_asks_for_the_shortfall_not_the_declared_total() -> None:
    card = render_funding_advisory_card(deferred_view())

    assert "입금 60000 KRW 시 실행 가능" in card.text
    assert "입금 640000 KRW 시 실행 가능" not in card.text
    assert JIT_DEPOSIT_HEADER in card.text
    assert JIT_DEPOSIT_BASIS in card.text
    assert "pending/reserved 포함 시 90000 KRW" in card.text
    assert "선언 커버: 선언 여력으로 커버 가능 (선언 총액 640000 KRW)" in card.text
    assert JIT_DECLARED_DISCLAIMER in card.text
    assert "조건부 보류(deferred_with_condition)" in card.text
    assert "입금 확인 뒤 재평가에서 제안 생성 · 기존 승인 경로 그대로" in card.text


def test_card_deposit_line_tracks_shortfall_when_declaration_is_smaller() -> None:
    view = deferred_view()
    view["jit_funding"]["condition"]["declared_cover"] = "partial"
    view["jit_funding"]["condition"]["declared_total_disclosure_only"] = "10000"

    card = render_funding_advisory_card(view)

    assert "입금 60000 KRW 시 실행 가능" in card.text
    assert "선언 커버: 선언 여력 일부만 커버 (선언 총액 10000 KRW)" in card.text


def test_card_has_no_jit_block_without_a_deferred_disposition() -> None:
    plain = render_funding_advisory_card(advisory_view())
    fundable = advisory_view()
    fundable["jit_funding"] = {
        "disposition": "fundable_now",
        "condition": None,
        "next_step": "existing_proposal_creation_and_approval_path",
    }

    assert JIT_DEPOSIT_HEADER not in plain.text
    assert JIT_DEPOSIT_HEADER not in render_funding_advisory_card(fundable).text


def test_jit_block_adds_no_button_or_callback() -> None:
    card = render_funding_advisory_card(deferred_view())

    rows = card.inline_keyboard["inline_keyboard"]
    assert all("callback_data" not in button for row in rows for button in row)
    assert all("url" in button for row in rows for button in row)
