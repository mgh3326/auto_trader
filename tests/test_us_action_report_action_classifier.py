from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.schemas.us_action_report import KISUSAccountSnapshot, USHolding, USOpenOrder
from app.services.us_action_report.action_classifier import (
    build_us_held_position_action_cards,
)

_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


def _holding(symbol: str, **overrides):
    data = {
        "symbol": symbol,
        "display_name": symbol,
        "quantity": 10.0,
        "average_cost_usd": 100.0,
        "cost_basis_usd": 1000.0,
        "last_price_usd": 110.0,
        "value_usd": 1100.0,
        "pnl_usd": 100.0,
        "pnl_rate": 10.0,
        "sellable_qty": 10.0,
    }
    data.update(overrides)
    return USHolding(**data)


def _snapshot(*, holdings=None, open_orders=None):
    return KISUSAccountSnapshot(
        captured_at=_NOW,
        usd_cash=1000.0,
        usd_buying_power=1000.0,
        holdings=list(holdings or []),
        open_orders=list(open_orders or []),
    )


def test_target_hit_active_journal_produces_trim_card_with_thesis_and_target():
    cards = build_us_held_position_action_cards(
        account_snapshot=_snapshot(
            holdings=[
                _holding(
                    "AAPL",
                    display_name="Apple",
                    last_price_usd=125.0,
                    value_usd=1250.0,
                    pnl_usd=250.0,
                    pnl_rate=25.0,
                )
            ]
        ),
        journals_by_symbol={
            "AAPL": {
                "status": "active",
                "account_type": "live",
                "thesis": "AI device cycle thesis",
                "target_price": 120.0,
                "stop_loss": 92.0,
            }
        },
        now=lambda: _NOW,
    )

    assert len(cards) == 1
    card = cards[0]
    assert card.symbol == "AAPL"
    assert card.action == "trim"
    assert card.suggested_trim_pct == 50
    assert card.executable_qty == 5.0
    assert card.target_price_usd == 120.0
    assert card.stop_loss_usd == 92.0
    assert card.thesis == "AI device cycle thesis"
    assert "target_hit" in card.reason_codes
    assert card.missing_context_codes == []


def test_stop_loss_sell_is_blocked_by_active_min_hold():
    hold_until = _NOW + timedelta(days=5)
    cards = build_us_held_position_action_cards(
        account_snapshot=_snapshot(
            holdings=[
                _holding(
                    "MSFT",
                    last_price_usd=88.0,
                    value_usd=880.0,
                    pnl_usd=-120.0,
                    pnl_rate=-12.0,
                )
            ]
        ),
        journals_by_symbol={
            "MSFT": {
                "status": "active",
                "account_type": "live",
                "thesis": "Long-term cloud thesis",
                "target_price": 130.0,
                "stop_loss": 90.0,
                "hold_until": hold_until,
            }
        },
        now=lambda: _NOW,
    )

    card = cards[0]
    assert card.action == "hold"
    assert card.executable_qty == 0.0
    assert card.hold_until == hold_until
    assert "stop_loss_hit" in card.reason_codes
    assert "min_hold_active" in card.reason_codes


def test_pending_sell_suppresses_duplicate_trim_or_sell_action():
    cards = build_us_held_position_action_cards(
        account_snapshot=_snapshot(
            holdings=[
                _holding(
                    "NVDA",
                    last_price_usd=150.0,
                    value_usd=1500.0,
                    pnl_usd=500.0,
                    pnl_rate=50.0,
                )
            ],
            open_orders=[
                USOpenOrder(
                    symbol="NVDA",
                    side="sell",
                    quantity=2.0,
                    remaining_qty=2.0,
                    pending_qty=2.0,
                    order_id="S-1",
                )
            ],
        ),
        journals_by_symbol={
            "NVDA": {
                "status": "active",
                "account_type": "live",
                "thesis": "GPU thesis",
                "target_price": 140.0,
                "stop_loss": 95.0,
            }
        },
        now=lambda: _NOW,
    )

    card = cards[0]
    assert card.action == "hold"
    assert card.pending_sell_qty == 2.0
    assert card.executable_qty == 0.0
    assert "pending_sell_exists" in card.reason_codes
    assert any("duplicate pending sell" in warning for warning in card.warnings)
    assert any("duplicate pending sell" in warning for warning in cards.warnings)


def test_journal_only_and_manual_reference_symbols_are_warned_not_executable():
    cards = build_us_held_position_action_cards(
        account_snapshot=_snapshot(holdings=[_holding("QQQM")]),
        journals_by_symbol={
            "QQQM": {
                "status": "active",
                "account_type": "live",
                "thesis": "Core Nasdaq",
            },
            "TSLA": {
                "status": "active",
                "account_type": "live",
                "thesis": "Journal only",
            },
        },
        manual_reference_symbols={"TSLA", "PLTR"},
        now=lambda: _NOW,
    )

    assert [card.symbol for card in cards] == ["QQQM"]
    assert any(
        "TSLA: journal_only_not_kis_held" == warning for warning in cards.warnings
    )
    assert any(
        "PLTR: manual_reference_only_not_kis_tradeable" == warning
        for warning in cards.warnings
    )
    assert all(card.symbol != "TSLA" for card in cards)


def test_missing_journal_keeps_kis_held_position_as_watch_with_missing_context():
    cards = build_us_held_position_action_cards(
        account_snapshot=_snapshot(holdings=[_holding("BRK.B", pnl_rate=2.0)]),
        journals_by_symbol={},
        now=lambda: _NOW,
    )

    card = cards[0]
    assert card.symbol == "BRK.B"
    assert card.action == "watch"
    assert card.journal_status == "missing"
    assert "journal_missing" in card.missing_context_codes
    assert card.executable_qty == 0.0
