from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    ScreenedUSNewBuyCandidate,
    USHolding,
    USOpenOrder,
)
from app.services.action_report.us.action_classifier import (
    build_us_held_position_action_cards,
)
from app.services.action_report.us.discord_formatter import (
    build_us_action_report_discord_message,
)
from app.services.action_report.us.new_buy_candidates import (
    build_us_new_buy_candidate_cards,
)

_NOW = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)


def _holding(symbol: str, **overrides):
    data = {
        "symbol": symbol,
        "display_name": symbol,
        "quantity": 10.0,
        "average_cost_usd": 100.0,
        "cost_basis_usd": 1000.0,
        "last_price_usd": 125.0,
        "value_usd": 1250.0,
        "pnl_usd": 250.0,
        "pnl_rate": 25.0,
        "sellable_qty": 8.0,
    }
    data.update(overrides)
    return USHolding(**data)


def _snapshot():
    return KISUSAccountSnapshot(
        captured_at=_NOW,
        usd_cash=1200.0,
        usd_buying_power=1000.0,
        holdings=[_holding("AAPL", display_name="Apple")],
        open_orders=[
            USOpenOrder(
                symbol="NVDA",
                side="buy",
                quantity=1,
                remaining_qty=1,
                pending_qty=1,
                order_id="B-1",
            )
        ],
        warnings=["kis_live_us_quote_unavailable:MSFT"],
    )


def test_discord_report_contains_required_sections_and_no_submit_statement():
    snapshot = _snapshot()
    held_actions = build_us_held_position_action_cards(
        account_snapshot=snapshot,
        journals_by_symbol={
            "AAPL": {
                "status": "active",
                "account_type": "live",
                "thesis": "Device cycle thesis",
                "target_price": 120.0,
                "stop_loss": 92.0,
            }
        },
        manual_reference_symbols={"TSLA"},
        now=lambda: _NOW,
    )
    new_buy_cards = build_us_new_buy_candidate_cards(
        account_snapshot=snapshot,
        candidates=[
            ScreenedUSNewBuyCandidate(
                symbol="MSFT", name="Microsoft", price=250, score=90
            ),
            ScreenedUSNewBuyCandidate(symbol="AAPL", name="Apple", price=125, score=99),
        ],
        research_by_symbol={
            "MSFT": {
                "thesis": "Cloud and AI growth, but entry needs confirmation.",
                "target_price": 280,
                "stop_loss": 235,
                "min_hold_days": 21,
            }
        },
        per_candidate_budget_pct=0.25,
    )

    report = build_us_action_report_discord_message(
        account_snapshot=snapshot,
        held_actions=held_actions,
        new_buy_candidates=new_buy_cards,
        manual_reference_symbols={"TSLA"},
    )

    assert "### 1) KIS live account summary" in report
    assert "### 2) Tradeable holdings actions" in report
    assert "### 3) Manual/reference caveat" in report
    assert "### 4) New-buy candidates" in report
    assert "### 5) Open-order and journal warnings" in report
    assert "### 6) Order-before-execution checklist" in report
    assert "### 7) Safety / no-submit statement" in report
    assert "KIS live" in report
    assert "TSLA — 참고용이며 KIS 매도가능/거래가능 수량에 포함하지 않음" in report
    assert "**AAPL** (Apple) — 일부 익절/축소 검토" in report
    assert "근거: target_hit" in report
    assert "**MSFT** Microsoft — 분석 우선순위 1 / 검토 후보" in report
    assert "Cloud and AI growth" in report
    assert "NVDA 매수 미체결 1주" in report
    assert "kis_live_us_quote_unavailable:MSFT" in report
    assert "NO LIVE ORDER WAS SUBMITTED, CANCELLED, OR MODIFIED" in report
    assert "not execution authorization" in report


def test_discord_report_empty_lists_still_keeps_caveats_and_checklist_language():
    snapshot = KISUSAccountSnapshot(
        captured_at=_NOW,
        usd_cash=None,
        usd_buying_power=None,
        holdings=[],
        open_orders=[],
    )

    report = build_us_action_report_discord_message(
        account_snapshot=snapshot,
        held_actions=[],
        new_buy_candidates=[],
        manual_reference_symbols=[],
        title_suffix="dry run fixture",
    )

    assert "dry run fixture" in report
    assert "KIS live tradeable holding action 없음" in report
    assert "신규매수 검토 후보 없음" in report
    assert "Manual/Toss/reference balances are reference-only" in report
    assert "KIS live open orders: none" in report
    assert "preview/checklist" in report
    assert "NO LIVE ORDER" in report
