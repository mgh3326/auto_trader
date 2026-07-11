from datetime import UTC, datetime

import pytest

from app.services.order_proposals.broker_gateway import (
    SUPPORTED_TARGET_ACTIONS,
    cancel_target_order,
    fetch_target_order,
)
from app.services.order_proposals.errors import OrderProposalError

NOW = datetime(2026, 7, 11, 8, 23, tzinfo=UTC)


def _order_row(**overrides):
    return {
        "order_id": "manual-1",
        "symbol": "KRW-AVAX",
        "side": "sell",
        "status": "pending",
        "remaining_qty": 2,
        "ordered_price": 41000,
        "order_type": "limit",
        **overrides,
    }


@pytest.mark.unit
def test_supported_target_actions_are_live_kis_and_upbit_only():
    assert SUPPORTED_TARGET_ACTIONS == frozenset(
        {
            ("kis_live", "equity_kr"),
            ("kis_live", "equity_us"),
            ("upbit", "crypto"),
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_accepts_unattributed_open_order_and_routes_history_lookup():
    captured = {}

    async def fake_history(**kwargs):
        captured.update(kwargs)
        return {"orders": [_order_row()], "errors": []}

    snapshot = await fetch_target_order(
        order_id="manual-1",
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        now=NOW,
        history_fn=fake_history,
    )

    assert snapshot.broker_order_id == "manual-1"
    assert snapshot.status == "open"
    assert captured == {
        "symbol": "KRW-AVAX",
        "status": "all",
        "order_id": "manual-1",
        "market": "crypto",
        "limit": 20,
        "is_mock": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_preserves_remaining_quantity_for_fresh_drift_comparison():
    def fake_history(**_kwargs):
        return {"orders": [_order_row(remaining_qty=1.25)], "errors": []}

    snapshot = await fetch_target_order(
        order_id="manual-1",
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        now=NOW,
        history_fn=fake_history,
    )

    assert snapshot.remaining_quantity == "1.25"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("orders", [[], [_order_row(), _order_row()]])
async def test_fetch_rejects_zero_or_multiple_target_matches(orders):
    async def fake_history(**_kwargs):
        return {"orders": orders, "errors": []}

    with pytest.raises(OrderProposalError, match="not found uniquely"):
        await fetch_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market="crypto",
            account_mode="upbit",
            now=NOW,
            history_fn=fake_history,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_fails_closed_when_broker_history_reports_errors():
    async def fake_history(**_kwargs):
        return {
            "orders": [_order_row()],
            "errors": [{"market": "crypto", "error": "upstream unavailable"}],
        }

    with pytest.raises(OrderProposalError, match="upstream unavailable"):
        await fetch_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market="crypto",
            account_mode="upbit",
            now=NOW,
            history_fn=fake_history,
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_mode", "market"),
    [("kis_mock", "equity_kr"), ("kis_live", "crypto"), ("upbit", "equity_us")],
)
async def test_fetch_rejects_unsupported_target_tuple(account_mode, market):
    with pytest.raises(OrderProposalError, match="lookup unsupported"):
        await fetch_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market=market,
            account_mode=account_mode,
            now=NOW,
            history_fn=lambda **_kwargs: pytest.fail("history must not be called"),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_routes_live_target_and_returns_broker_result_without_confirmation():
    captured = {}
    broker_result = {"success": True, "order_id": "manual-1", "cancelled_at": ""}

    def fake_cancel(**kwargs):
        captured.update(kwargs)
        return broker_result

    result = await cancel_target_order(
        order_id="manual-1",
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        cancel_fn=fake_cancel,
    )

    assert result == broker_result
    assert captured == {
        "order_id": "manual-1",
        "symbol": "KRW-AVAX",
        "market": "crypto",
        "is_mock": False,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_returns_broker_rejection_for_the_confirmation_gate_to_handle():
    async def fake_cancel(**_kwargs):
        return {"success": False, "order_id": "manual-1", "error": "already filled"}

    assert await cancel_target_order(
        order_id="manual-1",
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        cancel_fn=fake_cancel,
    ) == {"success": False, "order_id": "manual-1", "error": "already filled"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_returns_cancelled_evidence_only_when_broker_reports_cancelled_order():
    async def fake_history(**_kwargs):
        return {"orders": [_order_row(status="cancelled", remaining_qty=0)], "errors": []}

    snapshot = await fetch_target_order(
        order_id="manual-1",
        symbol="KRW-AVAX",
        market="crypto",
        account_mode="upbit",
        now=NOW,
        history_fn=fake_history,
    )

    assert snapshot.status == "cancelled"
    assert snapshot.remaining_quantity == "0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_rejects_unsupported_target_tuple():
    with pytest.raises(OrderProposalError, match="cancel unsupported"):
        await cancel_target_order(
            order_id="manual-1",
            symbol="KRW-AVAX",
            market="crypto",
            account_mode="kis_live",
            cancel_fn=lambda **_kwargs: pytest.fail("cancel must not be called"),
        )
