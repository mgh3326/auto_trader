from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.schemas.us_action_report import (
    KISUSAccountSnapshot,
    KISUSOrderPreviewRequest,
    KISUSOrderSubmitDisabledError,
    USHolding,
    USOpenOrder,
)
from app.services.action_report.us import order_preview
from app.services.action_report.us.order_preview import (
    preview_kis_us_live_order,
    submit_kis_us_live_order_from_preview_disabled,
)


def _holding(symbol: str, **overrides):
    data = {
        "symbol": symbol,
        "display_name": symbol,
        "quantity": 10.0,
        "average_cost_usd": 100.0,
        "cost_basis_usd": 1000.0,
        "last_price_usd": 100.0,
        "value_usd": 1000.0,
        "pnl_usd": 0.0,
        "pnl_rate": 0.0,
        "sellable_qty": 10.0,
    }
    data.update(overrides)
    return USHolding(**data)


def _snapshot(*, holdings=None, open_orders=None):
    return KISUSAccountSnapshot(
        captured_at="2026-05-14T12:00:00Z",
        usd_cash=2000.0,
        usd_buying_power=2000.0,
        holdings=list(holdings or []),
        open_orders=list(open_orders or []),
    )


def test_sell_preview_passes_with_kis_live_sellable_qty_and_submit_disabled():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(holdings=[_holding("QQQM", sellable_qty=4.0)]),
        request=KISUSOrderPreviewRequest(
            symbol="QQQM",
            side="sell",
            quantity=2.0,
            limit_price_usd=102.0,
            reference_price_usd=100.0,
        ),
    )

    assert preview.status == "pass"
    assert preview.submit_enabled is False
    assert preview.blocked_reasons == []
    assert preview.notional_usd == 204.0
    assert preview.check_details["sellableQty"] == 4.0


def test_sell_preview_warns_when_ladder_is_entirely_above_market():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(holdings=[_holding("IONQ", sellable_qty=8.0)]),
        request=KISUSOrderPreviewRequest(
            symbol="IONQ",
            side="sell",
            quantity=2.0,
            limit_price_usd=64.0,
            reference_price_usd=63.95,
            ladder_rungs=[
                {"quantity": 2.0, "limitPriceUsd": 64.0},
                {"quantity": 3.0, "limitPriceUsd": 66.0},
                {"quantity": 3.0, "limitPriceUsd": 68.0},
            ],
        ),
    )

    assert preview.status == "pass"
    assert "ladder_all_above_market" in preview.warnings
    assert "ladder_missing_near_market_anchor" in preview.warnings
    fill_safety = preview.check_details["fillSafety"]
    assert fill_safety["allRungsAboveMarket"] is True
    assert fill_safety["hasMarketableAnchor"] is False
    assert fill_safety["hasNearMarketAnchor"] is False
    assert fill_safety["suggestedAnchorRung"]["limitPriceUsd"] == 63.95
    assert fill_safety["rungs"][0]["distancePct"] == pytest.approx(0.0782)
    assert fill_safety["rungs"][0]["nearAboveMarket"] is True


def test_sell_preview_keeps_near_market_anchor_ladder_warning_free():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(holdings=[_holding("IONQ", sellable_qty=8.0)]),
        request=KISUSOrderPreviewRequest(
            symbol="IONQ",
            side="sell",
            quantity=2.0,
            limit_price_usd=63.95,
            reference_price_usd=63.95,
            atr_usd=4.0,
            ladder_rungs=[
                {"quantity": 2.0, "limitPriceUsd": 63.95},
                {"quantity": 3.0, "limitPriceUsd": 66.0},
                {"quantity": 3.0, "limitPriceUsd": 68.0},
            ],
        ),
    )

    assert preview.status == "pass"
    assert "ladder_all_above_market" not in preview.warnings
    assert "ladder_missing_near_market_anchor" not in preview.warnings
    fill_safety = preview.check_details["fillSafety"]
    assert fill_safety["allRungsAboveMarket"] is False
    assert fill_safety["hasMarketableAnchor"] is True
    assert fill_safety["hasNearMarketAnchor"] is True
    assert fill_safety["rungs"][1]["atrMultiple"] == pytest.approx(0.5125)


def test_sell_preview_rejects_manual_only_quantity_as_not_sellable():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(
            holdings=[
                _holding(
                    "TSLA",
                    manual_only=True,
                    source_of_truth=False,
                    is_tradeable=False,
                    sellable_qty=10.0,
                )
            ]
        ),
        request=KISUSOrderPreviewRequest(
            symbol="TSLA",
            side="sell",
            quantity=1.0,
            limit_price_usd=100.0,
            reference_price_usd=100.0,
        ),
    )

    assert preview.status == "blocked"
    assert "manual_only_quantity_not_sellable" in preview.blocked_reasons
    assert "kis_live_sellable_quantity_missing" in preview.blocked_reasons
    assert preview.submit_enabled is False


def test_preview_rejects_duplicate_pending_order_for_same_side():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(
            holdings=[_holding("NVDA")],
            open_orders=[
                USOpenOrder(
                    symbol="NVDA",
                    side="sell",
                    pending_qty=1.0,
                    remaining_qty=1.0,
                    order_id="pending-sell-1",
                )
            ],
        ),
        request=KISUSOrderPreviewRequest(
            symbol="NVDA",
            side="sell",
            quantity=1.0,
            limit_price_usd=101.0,
            reference_price_usd=100.0,
        ),
    )

    assert preview.status == "blocked"
    assert "duplicate_pending_order_exists" in preview.blocked_reasons
    assert preview.check_details["pendingDuplicateCount"] == 1


def test_buy_preview_requires_journal_fields_and_checks_bounds_and_price_sanity():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(
            open_orders=[
                USOpenOrder(
                    symbol="MSFT",
                    side="buy",
                    pending_qty=1.0,
                    remaining_qty=1.0,
                    order_id="pending-buy-1",
                )
            ]
        ),
        request=KISUSOrderPreviewRequest(
            symbol="MSFT",
            side="buy",
            quantity=6.0,
            limit_price_usd=150.0,
            reference_price_usd=100.0,
            thesis="",
        ),
        max_quantity=5.0,
        max_notional_usd=500.0,
        max_limit_deviation_pct=10.0,
    )

    assert preview.status == "blocked"
    assert "duplicate_pending_order_exists" in preview.blocked_reasons
    assert "buy_journal_required_fields_missing" in preview.blocked_reasons
    assert "quantity_exceeds_preview_bound" in preview.blocked_reasons
    assert "notional_exceeds_preview_bound" in preview.blocked_reasons
    assert "limit_price_deviation_exceeds_bound" in preview.blocked_reasons
    assert preview.check_details["missingBuyJournalFields"] == [
        "thesis",
        "strategy",
        "target_price_usd",
        "stop_loss_usd",
        "min_hold_days",
    ]


def test_buy_preview_accepts_required_fields_from_request_or_active_journal():
    preview = preview_kis_us_live_order(
        account_snapshot=_snapshot(),
        request=KISUSOrderPreviewRequest(
            symbol="MSFT",
            side="buy",
            quantity=2.0,
            limit_price_usd=100.0,
            reference_price_usd=101.0,
            thesis="AI platform thesis",
            target_price_usd=120.0,
        ),
        journals_by_symbol={
            "MSFT": {
                "strategy": "us_action_report_mvp",
                "stop_loss": 95.0,
                "min_hold_days": 14,
            }
        },
    )

    assert preview.status == "pass"
    assert preview.blocked_reasons == []
    assert preview.submit_enabled is False


def test_preview_flow_does_not_call_submit_cancel_or_modify_methods():
    source = textwrap.dedent(inspect.getsource(order_preview.preview_kis_us_live_order))
    tree = ast.parse(source)
    forbidden = {"submit_order", "place_order", "cancel_order", "modify_order"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            assert func.attr not in forbidden
        elif isinstance(func, ast.Name):
            assert func.id not in forbidden


def test_submit_path_is_explicitly_disabled():
    with pytest.raises(KISUSOrderSubmitDisabledError):
        submit_kis_us_live_order_from_preview_disabled()
