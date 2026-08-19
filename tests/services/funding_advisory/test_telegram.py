from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.services.funding_advisory.telegram import (
    CARD_HEADER,
    HANDOFF_LABEL,
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
