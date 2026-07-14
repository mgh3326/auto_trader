from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
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


def _toss_order(**overrides):
    values = {
        "order_id": "broker-1",
        "client_order_id": None,
        "status": "FILLED",
        "symbol": "005930",
        "side": "BUY",
        "quantity": Decimal("1.00000000"),
        "price": Decimal("100.00"),
        "ordered_at": NOW.isoformat(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _toss_rung(**overrides):
    values = {
        "rung_index": 0,
        "idempotency_key": "tosprop-legacy-1",
        "broker_order_id": None,
        "side": "buy",
        "quantity": Decimal("1"),
        "limit_price": Decimal("100.0000"),
        "created_at": NOW,
        "updated_at": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_proves_absence_across_open_and_closed():
    from app.services.order_proposals import broker_gateway

    calls = []

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(orders=[], has_next=False, next_cursor=None)

    rung = _toss_rung(created_at=NOW - timedelta(days=2))
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"
    assert "OPEN" in evidence[0].lookup_scope
    assert "CLOSED" in evidence[0].lookup_scope
    assert [call["status"] for call in calls] == ["OPEN", "CLOSED"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("broker_state", ["OPEN", "FILLED"])
async def test_operator_void_toss_scan_exposes_found_broker_state(broker_state):
    from app.services.order_proposals import broker_gateway

    found = _toss_order(
        status=broker_state,
        quantity=Decimal("1.00000000"),
        price=Decimal("100.00"),
    )

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = (
                [found]
                if kwargs["status"] == ("OPEN" if broker_state == "OPEN" else "CLOSED")
                else []
            )
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "found"
    assert evidence[0].broker_order_id == "broker-1"
    assert evidence[0].broker_state == broker_state


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_fails_closed_on_timeout():
    from app.services.order_proposals import broker_gateway

    class FakeTossClient:
        async def list_orders(self, **_kwargs):
            raise httpx.ReadTimeout("")

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "ReadTimeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_proves_composite_absence_without_client_id():
    from app.services.order_proposals import broker_gateway

    unrelated_order = _toss_order(
        order_id="unrelated-order",
        symbol="000660",
        status="OPEN",
    )

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [unrelated_order] if kwargs["status"] == "OPEN" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"
    assert "combination_matches=0" in evidence[0].lookup_scope


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["start", "end"])
async def test_operator_void_toss_scan_includes_composite_window_boundaries(boundary):
    from app.services.order_proposals import broker_gateway

    rung = _toss_rung()
    valid_until = NOW + timedelta(hours=4)
    window_start = rung.created_at - timedelta(hours=24)
    window_end = max(valid_until, rung.updated_at) + timedelta(hours=24)
    ordered_at = window_start if boundary == "start" else window_end
    found = _toss_order(ordered_at=ordered_at.isoformat())

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [found] if kwargs["status"] == "CLOSED" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=window_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "found"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_start", "after_end"])
async def test_operator_void_toss_scan_excludes_orders_outside_composite_window(
    boundary,
):
    from app.services.order_proposals import broker_gateway

    rung = _toss_rung()
    valid_until = NOW + timedelta(hours=4)
    window_start = rung.created_at - timedelta(hours=24)
    window_end = max(valid_until, rung.updated_at) + timedelta(hours=24)
    ordered_at = (
        window_start - timedelta(microseconds=1)
        if boundary == "before_start"
        else window_end + timedelta(microseconds=1)
    )
    outside_order = _toss_order(ordered_at=ordered_at.isoformat())

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            orders = [outside_order] if kwargs["status"] == "CLOSED" else []
            return SimpleNamespace(orders=orders, has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=window_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "absent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_uses_kst_dates_and_attempt_anchor():
    from app.core.timezone import KST
    from app.services.order_proposals import broker_gateway

    created_at = datetime(2026, 7, 10, 16, 30, tzinfo=UTC)
    updated_at = datetime(2026, 7, 11, 16, 30, tzinfo=UTC)
    valid_until = datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
    expected_start = created_at - timedelta(hours=24)
    expected_end = max(valid_until, updated_at) + timedelta(hours=24)
    calls = []

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(orders=[], has_next=False, next_cursor=None)

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung(created_at=created_at, updated_at=updated_at)],
        now=expected_end + timedelta(days=3),
        valid_until=valid_until,
        toss_client=FakeTossClient(),
    )

    expected_dates = {
        "from_date": expected_start.astimezone(KST).date().isoformat(),
        "to_date": expected_end.astimezone(KST).date().isoformat(),
    }
    assert evidence[0].outcome == "absent"
    assert [call["status"] for call in calls] == ["OPEN", "CLOSED"]
    assert [
        {key: call[key] for key in ("from_date", "to_date")} for call in calls
    ] == [expected_dates, expected_dates]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_toss_scan_fails_closed_at_closed_page_cap():
    from app.services.order_proposals import broker_gateway

    closed_pages = 0

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            nonlocal closed_pages
            if kwargs["status"] == "OPEN":
                return SimpleNamespace(orders=[], has_next=False, next_cursor=None)
            closed_pages += 1
            return SimpleNamespace(
                orders=[],
                has_next=True,
                next_cursor=f"cursor-{closed_pages}",
            )

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[_toss_rung()],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert closed_pages == broker_gateway._TOSS_CLOSED_PAGE_CAP
    assert evidence[0].outcome == "unknown"
    assert evidence[0].reason == "CLOSED order scan page cap reached"
    assert all(item.outcome != "absent" for item in evidence.values())


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "cursor-1"])
async def test_operator_void_toss_scan_fails_closed_on_invalid_pagination(cursor):
    from app.services.order_proposals import broker_gateway

    class FakeTossClient:
        async def list_orders(self, **kwargs):
            if kwargs["status"] == "OPEN":
                return SimpleNamespace(orders=[], has_next=False, next_cursor=None)
            return SimpleNamespace(orders=[], has_next=True, next_cursor=cursor)

    rung = _toss_rung()
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="toss_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        toss_client=FakeTossClient(),
    )

    assert evidence[0].outcome == "unknown"
    assert "cursor" in (evidence[0].reason or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_kis_history_distinguishes_absent_and_filled():
    from app.services.order_proposals import broker_gateway

    rung = SimpleNamespace(
        rung_index=0,
        idempotency_key=None,
        broker_order_id="kis-order-1",
        created_at=NOW - timedelta(days=1),
    )

    async def absent_history(**_kwargs):
        return {"orders": [], "errors": []}

    absent = await broker_gateway.fetch_operator_void_evidence(
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        history_fn=absent_history,
    )

    async def filled_history(**_kwargs):
        return {
            "orders": [{"order_id": "kis-order-1", "status": "filled"}],
            "errors": [],
        }

    found = await broker_gateway.fetch_operator_void_evidence(
        account_mode="kis_live",
        market="equity_kr",
        symbol="005930",
        rungs=[rung],
        now=NOW,
        history_fn=filled_history,
    )

    assert absent[0].outcome == "absent"
    assert found[0].outcome == "found"
    assert found[0].broker_state == "filled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_kis_us_empty_history_is_not_absence_proof():
    from app.services.order_proposals import broker_gateway

    rung = SimpleNamespace(
        rung_index=0,
        idempotency_key=None,
        broker_order_id="kis-us-order-1",
        created_at=NOW - timedelta(days=1),
    )

    async def empty_history(**_kwargs):
        return {"orders": [], "errors": [], "truncated": False}

    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="kis_live",
        market="equity_us",
        symbol="AAPL",
        rungs=[rung],
        now=NOW,
        history_fn=empty_history,
    )

    assert evidence[0].outcome == "unknown"
    assert "cannot be proven complete" in (evidence[0].reason or "")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_result", "reason_fragment"),
    [
        ({"orders": [], "errors": [], "truncated": True}, "truncated"),
        (
            {
                "orders": [],
                "errors": [{"market": "equity_us", "error": "page 2 failed"}],
                "truncated": False,
            },
            "page 2 failed",
        ),
    ],
)
async def test_operator_void_kis_history_fails_closed_when_incomplete(
    history_result, reason_fragment
):
    from app.services.order_proposals import broker_gateway

    captured = {}

    async def incomplete_history(**kwargs):
        captured.update(kwargs)
        return history_result

    rung = SimpleNamespace(
        rung_index=0,
        idempotency_key=None,
        broker_order_id="kis-order-1",
        created_at=NOW - timedelta(days=1),
    )
    evidence = await broker_gateway.fetch_operator_void_evidence(
        account_mode="kis_live",
        market="equity_us",
        symbol="AAPL",
        rungs=[rung],
        now=NOW,
        history_fn=incomplete_history,
    )

    assert captured["limit"] == -1
    assert evidence[0].outcome == "unknown"
    assert reason_fragment in (evidence[0].reason or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_operator_void_upbit_identifier_distinguishes_absent_and_open():
    from app.services.order_proposals import broker_gateway

    rung = SimpleNamespace(
        rung_index=0,
        idempotency_key="oprop-legacy-1",
        broker_order_id=None,
        created_at=NOW,
    )
    absent = await broker_gateway.fetch_operator_void_evidence(
        account_mode="upbit",
        market="crypto",
        symbol="KRW-BTC",
        rungs=[rung],
        now=NOW,
        upbit_identifier_lookup_fn=AsyncMock(side_effect=_http_status_error(404)),
    )
    found = await broker_gateway.fetch_operator_void_evidence(
        account_mode="upbit",
        market="crypto",
        symbol="KRW-BTC",
        rungs=[rung],
        now=NOW,
        upbit_identifier_lookup_fn=AsyncMock(
            return_value={"uuid": "upbit-order-1", "state": "wait"}
        ),
    )

    assert absent[0].outcome == "absent"
    assert found[0].outcome == "found"
    assert found[0].broker_state == "wait"
