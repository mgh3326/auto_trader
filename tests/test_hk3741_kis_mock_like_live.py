from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.mcp_server.tooling import (
    kis_mock_ledger,
    order_execution,
    orders_kis_variants,
    orders_modify_cancel,
    orders_registration,
)
from app.services.brokers import edge_client
from app.services.kis_mock_runner import singleton
from app.services.order_send_intent_service import DuplicateOrderIntent
from tests._mcp_tooling_support import DummyMCP
from tests.services.mock_integration.test_kis_coordination_adapter import (
    _domestic_client,
    _execute_kwargs,
)


def _mock_ledger_order(**overrides: Any) -> dict[str, Any]:
    return {
        "ledger_id": 1,
        "symbol": "005930",
        "side": "buy",
        "quantity": 2.0,
        "remaining_quantity": 2.0,
        "price": 70000.0,
        "krx_fwdg_ord_orgno": "00950",
        "instrument_type": "equity_kr",
        "lifecycle_state": "accepted",
        "edge_command_id": None,
        **overrides,
    }


@pytest.fixture
def order_path(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    events: list[str] = []
    reservations: dict[tuple[str, str], tuple[int, str | None]] = {}
    next_row_id = 0

    @asynccontextmanager
    async def session():
        yield object()

    class MemoryIntents:
        def __init__(self, db: object) -> None:
            pass

        async def reserve(
            self,
            *,
            account_scope: str,
            idempotency_key: str,
            side: str | None = None,
            **kwargs: Any,
        ) -> int:
            nonlocal next_row_id
            key = (account_scope, idempotency_key)
            if key in reservations:
                raise DuplicateOrderIntent
            next_row_id += 1
            reservations[key] = (next_row_id, side)
            events.append(f"reserve:{idempotency_key}")
            return next_row_id

        async def release_if_matches(
            self,
            *,
            account_scope: str,
            idempotency_key: str,
            row_id: int,
            side: str | None,
        ) -> int:
            key = (account_scope, idempotency_key)
            if reservations.get(key) != (row_id, side):
                return 0
            del reservations[key]
            return 1

    async def signal(*args: Any, **kwargs: Any) -> None:
        events.append("signal_committed")

    async def send(**kwargs: Any) -> dict[str, Any]:
        events.append("broker_send")
        return {"odno": "0000117058", "rt_cd": "0", "msg1": "ok"}

    monkeypatch.setattr(order_execution, "_order_session_factory", lambda: session)
    monkeypatch.setattr(order_execution, "OrderSendIntentService", MemoryIntents)
    monkeypatch.setattr(order_execution, "record_signal", signal)
    monkeypatch.setattr(order_execution, "mark_signal_outcome", AsyncMock())
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())
    monkeypatch.setattr(order_execution, "_execute_order", send)
    monkeypatch.setattr(
        kis_mock_ledger, "_fetch_kis_mock_baseline_qty", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        kis_mock_ledger,
        "_record_kis_mock_order",
        AsyncMock(return_value={"success": True, "order_no": "0000117058"}),
    )
    monkeypatch.setattr(order_execution, "get_kis_mock_edge_url", lambda: None)
    singleton.set_kis_mock_coordination_route_provider(None)
    return {"events": events, "reservations": reservations}


@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_kis_mock_same_idempotency_key_is_reserved_before_single_send(
    order_path: dict[str, Any], market_type: str
) -> None:
    kwargs = _execute_kwargs(
        market_type=market_type, mirror_cohort=None, idempotency_key="client-key"
    )
    first = await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(**kwargs)

    assert first["success"] is True
    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert (
        order_path["events"].index("signal_committed")
        < order_path["events"].index("reserve:client-key")
        < order_path["events"].index("broker_send")
    )
    assert ("kis_mock", "client-key") in order_path["reservations"]


@pytest.mark.asyncio
async def test_kis_mock_mirror_keeps_correlation_and_idempotency_reservations(
    order_path: dict[str, Any],
) -> None:
    kwargs = _execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    first = await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(
        **{**kwargs, "idempotency_key": "other-key"}
    )

    assert first["success"] is True
    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert ("kis_mock", "client-key") in order_path["reservations"]
    assert ("kis_mock", "mirror-key") in order_path["reservations"]


@pytest.mark.asyncio
async def test_mirror_duplicate_client_key_releases_only_its_new_correlation(
    order_path: dict[str, Any],
) -> None:
    order_path["reservations"][("kis_mock", "client-key")] = (999, "buy")
    result = await order_execution._execute_and_record(
        **_execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    )
    assert result["success"] is False
    assert order_path["events"].count("broker_send") == 0
    assert order_path["reservations"] == {("kis_mock", "client-key"): (999, "buy")}


@pytest.mark.asyncio
async def test_kis_mock_kr_timeout_keeps_idempotency_reservation_and_never_resends(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def timeout(**kwargs: Any) -> None:
        order_path["events"].append("broker_send")
        raise httpx.ReadTimeout("test timeout")

    monkeypatch.setattr(order_execution, "_execute_order", timeout)
    kwargs = _execute_kwargs(mirror_cohort=None, idempotency_key="timeout-key")
    with pytest.raises(order_execution.OrderSendOutcomeUnknown):
        await order_execution._execute_and_record(**kwargs)
    second = await order_execution._execute_and_record(**kwargs)

    assert second["success"] is False
    assert order_path["events"].count("broker_send") == 1
    assert ("kis_mock", "timeout-key") in order_path["reservations"]


@pytest.mark.asyncio
async def test_kis_mock_kr_without_provider_reaches_broker(
    order_path: dict[str, Any],
) -> None:
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None)
    )
    assert result["success"] is True
    assert order_path["events"].count("broker_send") == 1


@pytest.mark.asyncio
async def test_kis_mock_attribution_required_before_reservation_or_send(
    order_path: dict[str, Any],
) -> None:
    result = await order_execution._execute_and_record(
        **_execute_kwargs(
            strategy=None,
            correlation_id=None,
            mirror_cohort=None,
            idempotency_key="client-key",
        )
    )
    assert result.get("error_code") == "attribution_required"
    assert order_path["events"] == []


@pytest.mark.asyncio
async def test_kis_mock_signal_failure_blocks_before_reservation_or_send(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        order_execution,
        "record_signal",
        AsyncMock(side_effect=RuntimeError("test failure")),
    )
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None, idempotency_key="client-key")
    )
    assert result["error_code"] == "signal_record_unavailable"
    assert order_path["events"] == []


@pytest.mark.asyncio
async def test_kis_mock_domestic_place_needs_no_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def no_lease(**kwargs: Any):
        raise AssertionError("unexpected writer lease")
        yield

    monkeypatch.setattr(singleton, "enforce_kis_mock_mutation_writer", no_lease)
    client, parent = _domestic_client()
    await client.order_korea_stock("005930", "buy", 1, 70000, True)
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_cancel_without_capability_reaches_domestic_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order()
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(kis_mock_ledger, "mark_kis_mock_order_cancelled", AsyncMock())
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is True
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_cancel_unknown_remainder_refuses_before_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None)
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(edge_client, "get_kis_mock_edge_url", lambda: None)

    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_cancel_unknown_remainder_cannot_use_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None, edge_command_id="edge-1")
    send = AsyncMock()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        edge_client, "get_kis_mock_edge_url", lambda: "http://127.0.0.1:9999"
    )
    monkeypatch.setattr(edge_client, "cancel_kis_mock_command", send)
    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_kis_mock_cancel_terminal_ledger_row_never_resends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(lifecycle_state="cancelled")
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    result = await orders_modify_cancel._cancel_kis_mock_domestic(
        "0000117058", "005930"
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_modify_without_capability_reaches_domestic_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order()
    client, parent = _domestic_client()
    original_request = parent._request_with_rate_limit

    async def modify_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original_request(*args, **kwargs)
        return {"rt_cd": "0", "output": {"ODNO": "0001002"}}

    monkeypatch.setattr(parent, "_request_with_rate_limit", modify_response)
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(kis_mock_ledger, "update_kis_mock_order_terms", AsyncMock())
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_kr_security_type_or_none", AsyncMock(return_value=None)
    )
    result = await orders_modify_cancel._modify_kis_mock_domestic(
        "0000117058", "005930", "equity_kr", 70000, 1, False
    )
    assert result["success"] is True
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_kis_mock_modify_unknown_remainder_refuses_before_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _mock_ledger_order(remaining_quantity=None)
    client, parent = _domestic_client()
    monkeypatch.setattr(
        kis_mock_ledger, "resolve_mock_order_for_cancel", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda **kwargs: client
    )
    result = await orders_modify_cancel._modify_kis_mock_domestic(
        "0000117058", "005930", "equity_kr", 70000, 1, False
    )
    assert result["success"] is False
    assert parent.requests == []


@pytest.mark.asyncio
async def test_kis_mock_wire_pins_client_and_host_before_token_and_each_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, parent = _domestic_client()
    await client.order_korea_stock("005930", "buy", 1, 70000, True)
    hook = parent.requests[0]["pre_send_hook"]
    assert hook is not None
    parent._settings_view.kis_base_url = "https://openapi.koreainvestment.com:9443"
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await hook()
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await client.order_korea_stock("005930", "buy", 1, 70000, True)
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_mirror_proven_not_sent_releases_both_exact_reservations(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(**kwargs: Any) -> None:
        raise order_execution.PreSendFreshnessError(("expired",))

    monkeypatch.setattr(order_execution, "_execute_order", blocked)
    result = await order_execution._execute_and_record(
        **_execute_kwargs(idempotency_key="client-key", correlation_id="mirror-key")
    )
    assert result["pre_send_blocked"] is True
    assert order_path["reservations"] == {}


@pytest.mark.asyncio
async def test_mock_pre_send_release_does_not_delete_replacement_row(
    order_path: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def replaced(**kwargs: Any) -> None:
        order_path["reservations"][("kis_mock", "client-key")] = (999, "buy")
        raise order_execution.PreSendFreshnessError(("expired",))

    monkeypatch.setattr(order_execution, "_execute_order", replaced)
    result = await order_execution._execute_and_record(
        **_execute_kwargs(mirror_cohort=None, idempotency_key="client-key")
    )
    assert result["pre_send_blocked"] is True
    assert order_path["reservations"][("kis_mock", "client-key")] == (999, "buy")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "kis_mock_place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_variant",
        ),
        (
            "kis_mock_cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "_cancel_order_variant",
        ),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "_modify_order_variant",
        ),
    ],
)
@pytest.mark.parametrize(
    ("dry_run", "confirm"), [(True, False), (True, True), (False, False)]
)
async def test_dedicated_mock_mutations_refuse_without_both_gates(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    delegate: str,
    dry_run: bool,
    confirm: bool,
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(orders_kis_variants, delegate, call)
    result = await mcp.tools[name](**args, dry_run=dry_run, confirm=confirm)
    assert result["success"] is False
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "kis_mock_place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_variant",
        ),
        (
            "kis_mock_cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "_cancel_order_variant",
        ),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "_modify_order_variant",
        ),
    ],
)
async def test_dedicated_mock_mutations_delegate_only_after_both_gates(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any], delegate: str
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(orders_kis_variants, delegate, call)
    result = await mcp.tools[name](**args, dry_run=False, confirm=True)
    assert result["success"] is True
    call.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args", "delegate", "recommended"),
    [
        (
            "place_order",
            {"symbol": "005930", "side": "buy", "price": 70000},
            "_place_order_impl",
            "kis_mock_place_order",
        ),
        (
            "cancel_order",
            {"order_id": "ORDER-1", "market": "kr"},
            "cancel_order_impl",
            "kis_mock_cancel_order",
        ),
        (
            "modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
            "modify_order_impl",
            "kis_mock_modify_order",
        ),
    ],
)
async def test_generic_kr_mock_mutations_redirect_before_downstream(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    delegate: str,
    recommended: str,
) -> None:
    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    owner = order_execution if delegate == "_place_order_impl" else orders_registration
    monkeypatch.setattr(owner, delegate, call)
    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    result = await mcp.tools[name](**args, account_mode="kis_mock")
    assert result["error_code"] == "kis_mock_use_dedicated_tool"
    assert recommended in result["error"]
    call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("kis_mock_place_order", {"symbol": "005930", "side": "buy", "price": 70000}),
        ("kis_mock_cancel_order", {"order_id": "ORDER-1", "market": "kr"}),
        (
            "kis_mock_modify_order",
            {"order_id": "ORDER-1", "symbol": "005930", "new_price": 70000},
        ),
    ],
)
async def test_dedicated_mock_config_gate_still_refuses_before_io(
    monkeypatch: pytest.MonkeyPatch, name: str, args: dict[str, Any]
) -> None:
    mcp = DummyMCP()
    orders_kis_variants.register_kis_mock_order_tools(mcp)
    monkeypatch.setattr(
        orders_kis_variants, "validate_kis_mock_config", lambda: ["KIS_MOCK_ENABLED"]
    )
    place = AsyncMock()
    cancel = AsyncMock()
    modify = AsyncMock()
    monkeypatch.setattr(order_execution, "_place_order_impl", place)
    monkeypatch.setattr(orders_kis_variants, "cancel_order_impl", cancel)
    monkeypatch.setattr(orders_kis_variants, "modify_order_impl", modify)
    result = await mcp.tools[name](**args, dry_run=False, confirm=True)
    assert result["success"] is False
    assert "KIS_MOCK_ENABLED" in result["error"]
    place.assert_not_awaited()
    cancel.assert_not_awaited()
    modify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("account_mode", ["kis_live", "kis_mock"])
@pytest.mark.parametrize(
    ("name", "args", "delegate"),
    [
        (
            "place_order",
            {"symbol": "AAPL", "side": "buy", "price": 120},
            "_place_order_impl",
        ),
        (
            "cancel_order",
            {"order_id": "ORDER-1", "symbol": "AAPL", "market": "us"},
            "cancel_order_impl",
        ),
        (
            "modify_order",
            {"order_id": "ORDER-1", "symbol": "AAPL", "market": "us", "new_price": 120},
            "modify_order_impl",
        ),
    ],
)
async def test_generic_us_mock_and_live_still_delegate(
    monkeypatch: pytest.MonkeyPatch,
    account_mode: str,
    name: str,
    args: dict[str, Any],
    delegate: str,
) -> None:
    mcp = DummyMCP()
    orders_registration.register_order_tools(mcp)
    call = AsyncMock(return_value={"success": True})
    owner = order_execution if delegate == "_place_order_impl" else orders_registration
    monkeypatch.setattr(owner, delegate, call)
    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    result = await mcp.tools[name](**args, account_mode=account_mode)
    assert result["success"] is True
    assert call.await_args.kwargs["is_mock"] is (account_mode == "kis_mock")
