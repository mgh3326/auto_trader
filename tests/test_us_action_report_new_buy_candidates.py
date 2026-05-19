from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    ScreenedUSNewBuyCandidate,
    USHolding,
    USOpenOrder,
)
from app.services.action_report.us.new_buy_candidates import (
    build_us_new_buy_candidate_cards,
)


def _snapshot(**overrides):
    data = {
        "captured_at": datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        "usd_cash": 1000.0,
        "usd_buying_power": 800.0,
        "holdings": [
            USHolding(
                symbol="AAPL",
                display_name="Apple",
                quantity=2,
                average_cost_usd=150,
                cost_basis_usd=300,
                last_price_usd=200,
                value_usd=400,
                pnl_usd=100,
                pnl_rate=33.3,
                sellable_qty=2,
            )
        ],
        "open_orders": [],
    }
    data.update(overrides)
    return KISUSAccountSnapshot(**data)


def test_new_buy_cards_size_against_conservative_kis_usd_buying_power_and_skip_held_symbols():
    cards = build_us_new_buy_candidate_cards(
        account_snapshot=_snapshot(),
        candidates=[
            ScreenedUSNewBuyCandidate(symbol="AAPL", name="Apple", price=200, rsi=45),
            ScreenedUSNewBuyCandidate(
                symbol="MSFT", name="Microsoft", price=110, rsi=48, score=91
            ),
        ],
        research_by_symbol={
            "MSFT": {
                "thesis": "Cloud and AI growth, but entry needs confirmation.",
                "target_price": 125,
                "stop_loss": 103,
                "min_hold_days": 21,
            }
        },
        per_candidate_budget_pct=0.25,
    )

    assert [card.symbol for card in cards] == ["MSFT"]
    card = cards[0]
    assert card.label == "검토 후보"
    assert card.priority_label == "분석 우선순위 1"
    assert card.quantity_estimate == 1
    assert card.notional_estimate_usd == 110.0
    assert card.sizing_basis_usd == 800.0
    assert card.thesis == "Cloud and AI growth, but entry needs confirmation."
    assert card.target_price_usd == 125.0
    assert card.stop_loss_usd == 103.0
    assert card.min_hold_days == 21
    assert "KIS live USD buying power 기준" in card.sizing_note
    assert "기보유 종목 제외" in cards.warnings


def test_new_buy_cards_attach_open_order_calendar_news_and_concentration_risk_notes():
    cards = build_us_new_buy_candidate_cards(
        account_snapshot=_snapshot(
            usd_cash=2000,
            usd_buying_power=2000,
            holdings=[
                USHolding(
                    symbol="QQQM",
                    display_name="Invesco Nasdaq 100 ETF",
                    quantity=10,
                    average_cost_usd=100,
                    cost_basis_usd=1000,
                    last_price_usd=120,
                    value_usd=1200,
                    pnl_usd=200,
                    pnl_rate=20,
                    sellable_qty=10,
                )
            ],
            open_orders=[
                USOpenOrder(
                    symbol="NVDA",
                    side="buy",
                    pending_qty=1,
                    remaining_qty=1,
                    order_id="B-1",
                )
            ],
        ),
        candidates=[
            ScreenedUSNewBuyCandidate(
                symbol="NVDA",
                name="NVIDIA",
                price=500,
                sector="Semiconductors",
                score=97,
            ),
        ],
        calendar_risks_by_symbol={"NVDA": ["earnings in 3 days"]},
        news_risks_by_symbol={"NVDA": ["export-control headline risk"]},
        concentration_symbols={"NVDA", "QQQM"},
        per_candidate_budget_pct=0.5,
    )

    card = cards[0]
    assert card.quantity_estimate == 2
    assert card.notional_estimate_usd == 1000.0
    assert any("open buy order" in note for note in card.risk_notes)
    assert any("calendar: earnings in 3 days" in note for note in card.risk_notes)
    assert any("news: export-control headline risk" in note for note in card.risk_notes)
    assert any("concentration" in note for note in card.risk_notes)
    assert card.thesis.startswith("검토 후보: NVIDIA")
    assert card.target_price_usd == pytest.approx(540.0)
    assert card.stop_loss_usd == pytest.approx(475.0)
    assert card.min_hold_days == 14


def test_new_buy_cards_warn_when_kis_usd_capital_or_price_is_missing():
    cards = build_us_new_buy_candidate_cards(
        account_snapshot=_snapshot(usd_cash=None, usd_buying_power=None),
        candidates=[ScreenedUSNewBuyCandidate(symbol="TSLA", name="Tesla", price=None)],
    )

    assert len(cards) == 1
    assert cards[0].quantity_estimate == 0
    assert cards[0].notional_estimate_usd == 0.0
    assert "KIS live USD buying power 확인 불가" in cards[0].risk_notes
    assert "candidate price 확인 불가" in cards[0].risk_notes
    assert "kis_live_usd_capital_missing" in cards.warnings
