from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
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
        return {
            "orders": [_order_row(status="cancelled", remaining_qty=0)],
            "errors": [],
        }

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


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.upbit.com/v1/order")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        "broker lookup failed", request=request, response=response
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_submit_evidence_returns_found_order():
    from app.services.order_proposals.broker_gateway import (
        SubmitEvidence,
        fetch_submit_evidence,
    )

    found = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="upbit",
        market="crypto",
        lookup_fn=AsyncMock(return_value={"uuid": "35bee07f-full", "state": "wait"}),
    )

    assert found == SubmitEvidence("found", "35bee07f-full", "wait", None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_submit_evidence_treats_only_404_as_absent():
    from app.services.order_proposals.broker_gateway import fetch_submit_evidence

    absent = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="upbit",
        market="crypto",
        lookup_fn=AsyncMock(side_effect=_http_status_error(404)),
    )
    unknown = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="upbit",
        market="crypto",
        lookup_fn=AsyncMock(side_effect=_http_status_error(403)),
    )

    assert absent.outcome == "absent"
    assert absent.reason is None
    assert unknown.outcome == "unknown"
    assert unknown.reason == "broker lookup failed"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_result",
    [
        {"uuid": "", "state": "wait"},
        {"uuid": "35bee07f-full", "state": " "},
    ],
)
async def test_fetch_submit_evidence_treats_incomplete_broker_results_as_unknown(
    lookup_result,
):
    from app.services.order_proposals.broker_gateway import fetch_submit_evidence

    evidence = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="upbit",
        market="crypto",
        lookup_fn=AsyncMock(return_value=lookup_result),
    )

    assert evidence.outcome == "unknown"
    assert evidence.reason == "broker lookup returned incomplete order evidence"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_submit_evidence_treats_read_timeout_as_unknown():
    from app.services.order_proposals.broker_gateway import fetch_submit_evidence

    evidence = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="upbit",
        market="crypto",
        lookup_fn=AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    assert evidence.outcome == "unknown"
    assert evidence.reason == "ReadTimeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_submit_evidence_returns_unknown_for_unsupported_tuple():
    from app.services.order_proposals.broker_gateway import fetch_submit_evidence

    lookup = AsyncMock()
    evidence = await fetch_submit_evidence(
        identifier="oprop-fixed",
        account_mode="kis_live",
        market="crypto",
        lookup_fn=lookup,
    )

    assert evidence.outcome == "unknown"
    assert evidence.reason == "submit evidence lookup unsupported for kis_live/crypto"
    lookup.assert_not_awaited()
