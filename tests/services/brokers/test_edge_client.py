"""KIS mock broker-edge command-port contract tests."""

from __future__ import annotations

import json
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.mcp_server.tooling import order_execution, orders_modify_cancel
from app.services.brokers.edge_client import (
    BrokerEdgeNotCreated,
    BrokerEdgeOutcomeUnknown,
    cancel_kis_mock_command,
)
from app.services.brokers.kis.send_outcome import (
    OrderSendDisposition,
    OrderSendOutcomeTracker,
)


class _FakeEdgeServer:
    """A loopback HTTP server that records the exact edge command envelope."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: list[tuple[int, dict[str, Any]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                content_length = int(self.headers.get("content-length", "0"))
                raw_body = self.rfile.read(content_length)
                payload = json.loads(raw_body) if raw_body else None
                owner.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": payload,
                    }
                )
                status, response_payload = owner.responses.pop(0)
                encoded = json.dumps(response_payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def add_receipt(
        self,
        *,
        disposition: str,
        command_id: str,
        status: int = 200,
        broker_order_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": "execution-receipt/v1",
            "command_id": command_id,
            "disposition": disposition,
            "recorded_at": "2026-09-01T12:00:00Z",
        }
        if broker_order_id is not None:
            payload["broker_order_id"] = broker_order_id
        if error_code is not None:
            payload["error_code"] = error_code
        self.responses.append((status, payload))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def fake_edge() -> Generator[_FakeEdgeServer]:
    server = _FakeEdgeServer()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.asyncio
async def test_unconfigured_mock_keeps_existing_kis_call_byte_for_byte(
    monkeypatch,
) -> None:
    """The opt-in URL must not alter the old KIS mock call contract when absent."""
    monkeypatch.delenv("KIS_MOCK_EDGE_URL", raising=False)
    kis = Mock()
    kis.order_korea_stock = AsyncMock(return_value={"rt_cd": "0", "odno": "KIS-1"})
    create_kis = Mock(return_value=kis)
    monkeypatch.setattr(order_execution, "_create_kis_client", create_kis)

    result = await order_execution._execute_kr_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=2,
        price=70000,
        is_mock=True,
        idempotency_key="ignored-when-edge-is-off",
    )

    assert result == {"rt_cd": "0", "odno": "KIS-1"}
    create_kis.assert_called_once_with(is_mock=True)
    kis.order_korea_stock.assert_awaited_once_with(
        stock_code="005930",
        order_type="buy",
        quantity=2,
        price=70000,
        is_mock=True,
    )


@pytest.mark.asyncio
async def test_edge_accepted_posts_versioned_command_and_adopts_broker_order_id(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    command_id = "edge-accepted-001"
    fake_edge.add_receipt(
        disposition="ACCEPTED",
        command_id=command_id,
        broker_order_id="EDGE-ORDER-1",
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    create_kis = Mock()
    monkeypatch.setattr(order_execution, "_create_kis_client", create_kis)
    outcome = OrderSendOutcomeTracker()

    result = await order_execution._execute_kr_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=2,
        price=70000,
        is_mock=True,
        idempotency_key=command_id,
        send_outcome=outcome,
    )

    assert result["rt_cd"] == "0"
    assert result["odno"] == "EDGE-ORDER-1"
    assert result["broker_order_id"] == "EDGE-ORDER-1"
    assert outcome.disposition is OrderSendDisposition.ACCEPTED
    create_kis.assert_not_called()

    request = fake_edge.requests[0]
    assert request["path"] == "/v1/commands"
    assert request["headers"].get("Authorization") is None
    assert request["body"] == {
        "schema_version": "execution-command/v1",
        "command_id": command_id,
        "account_scope": "kis_mock",
        "side": "buy",
        "stock_code": "005930",
        "quantity": "2",
        "price": "70000",
        "order_type": "limit",
        "issued_at": request["body"]["issued_at"],
    }
    assert request["body"]["issued_at"].endswith("Z")


@pytest.mark.asyncio
async def test_us_edge_place_maps_accepted_and_uses_us_scope(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    command_id = "edge-us-accepted-001"
    fake_edge.add_receipt(
        disposition="ACCEPTED",
        command_id=command_id,
        broker_order_id="EDGE-US-ORDER-1",
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    create_kis = Mock()
    monkeypatch.setattr(order_execution, "_create_kis_client", create_kis)
    monkeypatch.setattr(
        order_execution,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=AssertionError("edge must not resolve KIS exchange")),
    )

    result = await order_execution._execute_us_order(
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=2,
        price=123.45,
        is_mock=True,
        idempotency_key=command_id,
    )

    assert result["rt_cd"] == "0"
    assert result["odno"] == "EDGE-US-ORDER-1"
    create_kis.assert_not_called()
    request = fake_edge.requests[0]
    assert request["path"] == "/v1/commands"
    assert request["body"] == {
        "schema_version": "execution-command/v1",
        "command_id": command_id,
        "account_scope": "kis_mock_us",
        "side": "buy",
        "stock_code": "AAPL",
        "quantity": "2",
        "price": "123.45",
        "order_type": "limit",
        "issued_at": request["body"]["issued_at"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "error_code", "expected_exception"),
    [
        ("NOT_CREATED", "place_disabled", BrokerEdgeNotCreated),
        ("UNKNOWN", "broker_timeout", order_execution.OrderSendOutcomeUnknown),
    ],
)
async def test_us_edge_place_preserves_nonaccepted_dispositions(
    monkeypatch,
    fake_edge: _FakeEdgeServer,
    disposition: str,
    error_code: str,
    expected_exception: type[Exception],
) -> None:
    command_id = f"edge-us-{disposition.lower()}-001"
    fake_edge.add_receipt(
        disposition=disposition,
        command_id=command_id,
        error_code=error_code,
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    monkeypatch.setattr(
        order_execution,
        "_create_kis_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("KIS fallback")),
    )

    with pytest.raises(expected_exception, match=error_code):
        await order_execution._execute_us_order(
            symbol="AAPL",
            side="buy",
            order_type="limit",
            quantity=2,
            price=123.45,
            is_mock=True,
            idempotency_key=command_id,
        )


@pytest.mark.asyncio
async def test_unconfigured_us_mock_keeps_existing_kis_direct_call(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KIS_MOCK_EDGE_URL", raising=False)
    kis = Mock()
    kis.buy_overseas_stock = AsyncMock(return_value={"rt_cd": "0", "odno": "KIS-US-1"})
    create_kis = Mock(return_value=kis)
    monkeypatch.setattr(order_execution, "_create_kis_client", create_kis)
    monkeypatch.setattr(
        order_execution, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    result = await order_execution._execute_us_order(
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=2,
        price=123.45,
        is_mock=True,
    )

    assert result == {"rt_cd": "0", "odno": "KIS-US-1"}
    create_kis.assert_called_once_with(is_mock=True)
    kis.buy_overseas_stock.assert_awaited_once_with(
        symbol="AAPL",
        exchange_code="NASD",
        quantity=2,
        price=123.45,
        is_mock=True,
    )


@pytest.mark.asyncio
async def test_us_edge_unsupported_market_order_fails_closed_without_kis_fallback(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    monkeypatch.setattr(
        order_execution,
        "_create_kis_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("KIS fallback")),
    )

    with pytest.raises(BrokerEdgeNotCreated, match="edge_unsupported_order_type"):
        await order_execution._execute_us_order(
            symbol="AAPL",
            side="buy",
            order_type="market",
            quantity=2,
            price=123.45,
            is_mock=True,
            idempotency_key="edge-us-market-001",
        )

    assert fake_edge.requests == []


def _patch_us_mock_cancel_resolution(monkeypatch, *, command_id: str) -> AsyncMock:
    """Use the existing ledger service boundary without making this HTTP test DB-bound."""
    from app.mcp_server.tooling import kis_mock_ledger
    from app.services.kis_mock_runner import singleton

    resolve = AsyncMock(
        return_value={
            "ledger_id": 42,
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 2.0,
            "price": 123.45,
            "krx_fwdg_ord_orgno": None,
            "instrument_type": "equity_us",
            "lifecycle_state": "accepted",
            "edge_command_id": command_id,
            "claim_account_scope": None,
            "claim_idempotency_key": None,
            "claim_row_id": None,
        }
    )
    marked = AsyncMock()
    monkeypatch.setattr(kis_mock_ledger, "resolve_mock_order_for_cancel", resolve)
    monkeypatch.setattr(kis_mock_ledger, "mark_kis_mock_order_cancelled", marked)
    monkeypatch.setattr(
        singleton, "verify_kis_mock_followup_capability", lambda *a, **k: None
    )
    monkeypatch.setattr(
        orders_modify_cancel,
        "_create_kis_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("KIS fallback")),
    )
    return marked


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "status", "expected_success"),
    [
        ("CANCELLED", 200, True),
        ("NOT_FOUND", 404, False),
    ],
)
async def test_us_edge_cancel_maps_terminal_dispositions(
    monkeypatch,
    fake_edge: _FakeEdgeServer,
    disposition: str,
    status: int,
    expected_success: bool,
) -> None:
    command_id = f"edge-us-cancel-{disposition.lower()}-001"
    fake_edge.add_receipt(
        disposition=disposition,
        command_id=command_id,
        status=status,
        error_code="command_not_found" if disposition == "NOT_FOUND" else None,
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    marked = _patch_us_mock_cancel_resolution(monkeypatch, command_id=command_id)

    result = await orders_modify_cancel._cancel_kis_overseas(
        "EDGE-US-ORDER-1", "AAPL", is_mock=True
    )

    assert result["success"] is expected_success
    assert fake_edge.requests[0]["path"] == f"/v1/commands/{command_id}/cancel"
    if expected_success:
        marked.assert_awaited_once()
    else:
        assert result["reconciliation_required"] is True
        marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_us_edge_cancel_unknown_is_never_mapped_to_success(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    command_id = "edge-us-cancel-unknown-001"
    fake_edge.add_receipt(
        disposition="UNKNOWN",
        command_id=command_id,
        error_code="broker_timeout",
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    marked = _patch_us_mock_cancel_resolution(monkeypatch, command_id=command_id)

    result = await orders_modify_cancel._cancel_kis_overseas(
        "EDGE-US-ORDER-1", "AAPL", is_mock=True
    )

    assert result["success"] is False
    assert result["outcome_unknown"] is True
    assert result["error_code"] == "unknown_pending_reconcile"
    assert result["edge_error_code"] == "broker_timeout"
    marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_us_mock_cancel_keeps_existing_unsupported_result(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KIS_MOCK_EDGE_URL", raising=False)

    result = await orders_modify_cancel._cancel_kis_overseas(
        "KIS-US-1", "AAPL", is_mock=True
    )

    assert result["success"] is False
    assert result["mock_unsupported"] is True
    assert "pending-orders inquiry" in result["error"]


@pytest.mark.asyncio
async def test_edge_cancelled_maps_to_broker_confirmed_cancel(
    fake_edge: _FakeEdgeServer,
) -> None:
    command_id = "edge-cancelled-001"
    fake_edge.add_receipt(disposition="CANCELLED", command_id=command_id)

    result = await cancel_kis_mock_command(
        base_url=fake_edge.url,
        command_id=command_id,
    )

    assert result == {
        "success": True,
        "broker_cancel_confirmed": True,
        "edge_command_id": command_id,
    }
    assert fake_edge.requests[0]["path"] == f"/v1/commands/{command_id}/cancel"
    assert fake_edge.requests[0]["body"] is None


@pytest.mark.asyncio
async def test_edge_cancel_not_found_requires_reconciliation(
    fake_edge: _FakeEdgeServer,
) -> None:
    command_id = "edge-not-found-001"
    fake_edge.add_receipt(
        disposition="NOT_FOUND",
        command_id=command_id,
        status=404,
        error_code="command_not_found",
    )

    result = await cancel_kis_mock_command(
        base_url=fake_edge.url,
        command_id=command_id,
    )

    assert result["success"] is False
    assert result["broker_cancel_confirmed"] is False
    assert result["reason_code"] == "edge_not_found_may_be_terminal"
    assert result["error_code"] == "command_not_found"
    assert result["reconciliation_required"] is True


@pytest.mark.asyncio
async def test_edge_cancel_unknown_is_never_mapped_to_success_mutant_guard(
    fake_edge: _FakeEdgeServer,
) -> None:
    command_id = "edge-cancel-unknown-001"
    fake_edge.add_receipt(
        disposition="UNKNOWN",
        command_id=command_id,
        error_code="broker_timeout",
    )

    with pytest.raises(BrokerEdgeOutcomeUnknown, match="broker_timeout"):
        await cancel_kis_mock_command(
            base_url=fake_edge.url,
            command_id=command_id,
        )


def _patch_place_to_send_only(monkeypatch) -> None:
    monkeypatch.setattr(
        order_execution,
        "_resolve_market_type",
        lambda _symbol, _market: ("equity_kr", "005930"),
    )
    monkeypatch.setattr(
        order_execution,
        "_fetch_current_price",
        AsyncMock(return_value=70000.0),
    )
    monkeypatch.setattr(
        order_execution,
        "_resolve_buy_quantity",
        lambda **kwargs: (kwargs["quantity"], kwargs["price"]),
    )
    monkeypatch.setattr(
        order_execution,
        "_build_preview",
        AsyncMock(
            return_value={
                "estimated_value": 70000.0,
                "quantity": 1,
                "price": 70000.0,
            }
        ),
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution,
        "evaluate_sector_concentration",
        AsyncMock(return_value={"verdict": "ok"}),
    )
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())

    async def execute_and_record(**kwargs: Any) -> dict[str, Any]:
        return await order_execution._execute_order(
            symbol=kwargs["normalized_symbol"],
            side=kwargs["side"],
            order_type=kwargs["order_type"],
            quantity=kwargs["order_quantity"],
            price=kwargs["price"],
            market_type=kwargs["market_type"],
            is_mock=kwargs["is_mock"],
            idempotency_key=kwargs["idempotency_key"],
            pre_send_hook=kwargs["pre_send_hook"],
            send_outcome=kwargs["send_outcome"],
        )

    monkeypatch.setattr(order_execution, "_execute_and_record", execute_and_record)


async def _place_edge_order(client_order_id: str) -> dict[str, Any]:
    return await order_execution._place_order_impl(
        symbol="005930",
        side="buy",
        market="kr",
        order_type="limit",
        quantity=1,
        price=70000.0,
        dry_run=False,
        is_mock=True,
        client_order_id=client_order_id,
    )


@pytest.mark.asyncio
async def test_edge_not_created_surfaces_its_error_code(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    command_id = "edge-not-created-001"
    fake_edge.add_receipt(
        disposition="NOT_CREATED",
        command_id=command_id,
        status=400,
        error_code="place_disabled",
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    _patch_place_to_send_only(monkeypatch)

    result = await _place_edge_order(command_id)

    assert result["success"] is False
    assert result["error_code"] == "place_disabled"
    assert "outcome_unknown" not in result


@pytest.mark.asyncio
async def test_edge_unknown_is_never_mapped_to_success(
    monkeypatch, fake_edge: _FakeEdgeServer
) -> None:
    command_id = "edge-unknown-001"
    fake_edge.add_receipt(
        disposition="UNKNOWN",
        command_id=command_id,
        error_code="broker_timeout",
    )
    monkeypatch.setenv("KIS_MOCK_EDGE_URL", fake_edge.url)
    _patch_place_to_send_only(monkeypatch)

    result = await _place_edge_order(command_id)

    assert result["success"] is False
    assert result["outcome_unknown"] is True
    assert result["error_code"] == "unknown_pending_reconcile"
    assert result["edge_error_code"] == "broker_timeout"
    assert "재전송하지 말고" in result["error"]
