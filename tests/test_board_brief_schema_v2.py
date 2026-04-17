from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.schemas.n8n.board_brief import (
    BoardBriefContext,
    BtcRegimePayload,
    CIOFollowupRequest,
    HardGateCandidate,
    NextObligationPayload,
    TCFollowupRequest,
    TierScenario,
    UnverifiedCapPayload,
)


def _full_v2_payload() -> dict:
    return {
        "exchange_krw": 1_500_000,
        "unverified_cap": {
            "amount": 10_000_000,
            "confirmed_at": "2026-04-17T09:30:00+09:00",
            "verified_by_boss_today": True,
            "stale_warning": False,
        },
        "next_obligation": {
            "date": "2026-04-24",
            "days_remaining": 7,
            "cash_needed_until": 700_000,
        },
        "tier_scenarios": [
            {
                "label": "T1",
                "target_exchange_krw": 2_000_000,
                "deposit_amount": 500_000,
                "buffer_days": 7,
                "cushion_after_obligation": 1_300_000,
            },
            {
                "label": "T2",
                "target_exchange_krw": 5_000_000,
                "deposit_amount": 3_500_000,
                "buffer_days": 14,
                "cushion_after_obligation": 4_300_000,
            },
        ],
        "hard_gate_candidates": [
            {
                "symbol": "SOL",
                "proposal": "부분매도",
                "amount_range": "8~10 SOL",
            }
        ],
        "data_sufficient_by_symbol": {"SOL": True, "BTC": False},
        "btc_regime": {
            "close_vs_20d_ma": "above",
            "ma20_slope": "up",
            "drawdown_14d_pct": 4.2,
        },
    }


def test_board_brief_context_round_trips_full_v2_payload() -> None:
    context = BoardBriefContext.model_validate(_full_v2_payload())

    assert context.exchange_krw == 1_500_000
    assert context.unverified_cap == UnverifiedCapPayload(
        amount=10_000_000,
        confirmed_at=datetime.fromisoformat("2026-04-17T09:30:00+09:00"),
        verified_by_boss_today=True,
        stale_warning=False,
    )
    assert context.next_obligation == NextObligationPayload(
        date=date(2026, 4, 24),
        days_remaining=7,
        cash_needed_until=700_000,
    )
    assert context.tier_scenarios[0] == TierScenario(
        label="T1",
        target_exchange_krw=2_000_000,
        deposit_amount=500_000,
        buffer_days=7,
        cushion_after_obligation=1_300_000,
    )
    assert context.hard_gate_candidates == [
        HardGateCandidate(symbol="SOL", proposal="부분매도", amount_range="8~10 SOL")
    ]
    assert context.btc_regime == BtcRegimePayload(
        close_vs_20d_ma="above",
        ma20_slope="up",
        drawdown_14d_pct=4.2,
    )

    dumped = context.model_dump(mode="json")
    assert BoardBriefContext.model_validate(dumped) == context


def test_board_brief_context_defaults_optional_v2_sections_cleanly() -> None:
    context = BoardBriefContext()

    assert context.exchange_krw == 0
    assert context.unverified_cap is None
    assert context.next_obligation is None
    assert context.tier_scenarios == []
    assert context.hard_gate_candidates == []
    assert context.data_sufficient_by_symbol == {}
    assert context.btc_regime is None
    assert context.manual_cash_krw == 0


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (UnverifiedCapPayload, {"amount": -1}),
        (
            NextObligationPayload,
            {"date": "2026-04-24", "days_remaining": -1, "cash_needed_until": 0},
        ),
        (
            NextObligationPayload,
            {"date": "2026-04-24", "days_remaining": 0, "cash_needed_until": -1},
        ),
        (
            TierScenario,
            {
                "label": "T4",
                "target_exchange_krw": 0,
                "deposit_amount": 0,
                "buffer_days": 0,
                "cushion_after_obligation": 0,
            },
        ),
        (
            BtcRegimePayload,
            {
                "close_vs_20d_ma": "sideways",
                "ma20_slope": "up",
                "drawdown_14d_pct": 0,
            },
        ),
    ],
)
def test_board_brief_v2_payloads_reject_invalid_values(
    model: type, payload: dict
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_followup_requests_accept_v2_fields_and_keep_manual_cash_compat() -> None:
    payload = _full_v2_payload() | {"manual_cash_krw": 9_000_000}

    tc_request = TCFollowupRequest.model_validate(payload)
    cio_request = CIOFollowupRequest.model_validate(payload)

    assert tc_request.manual_cash_krw == 9_000_000
    assert tc_request.unverified_cap is not None
    assert tc_request.unverified_cap.amount == 10_000_000
    assert cio_request.tier_scenarios[1].label == "T2"
    assert cio_request.btc_regime is not None
    assert cio_request.btc_regime.ma20_slope == "up"
