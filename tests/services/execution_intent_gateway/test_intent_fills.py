"""ROB-1461: committed fill projection and the loopback HTTP contract."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.models.execution_ledger import ExecutionLedger
from app.routers import execution_intent_gateway as route
from app.services.execution_intent_gateway.decisions import ChildSnapshot, ChildState
from app.services.execution_intent_gateway.query import intent_fill_orders
from tests.services.execution_intent_gateway.support import (
    NOW,
    MemoryGatewayStore,
    intent_snapshot,
)


def _submit(key, order_id):
    return ChildSnapshot(
        1,
        key,
        "digest",
        "submit",
        None,
        10,
        70000,
        ChildState.ACCEPTED,
        None,
        order_id,
        None,
        NOW,
    )


def fill(row_id=1, order_id="00123", **changes):
    values = {
        "id": row_id,
        "broker": "kis",
        "account_mode": "mock",
        "venue": "krx",
        "instrument_type": "equity_kr",
        "symbol": "005930",
        "raw_symbol": "005930",
        "side": "buy",
        "broker_order_id": order_id,
        "fill_seq": row_id,
        "filled_qty": Decimal("2"),
        "filled_price": Decimal("70000.12500000"),
        "filled_notional": Decimal("140000"),
        "currency": "KRW",
        "source": "websocket",
        "filled_at": NOW,
        "created_at": NOW + timedelta(seconds=1),
        "quarantined_at": None,
        "raw_payload_json": {"account_no": "12345678901"},
    }
    return ExecutionLedger(**(values | changes))


def children():
    return [replace(_submit("own-order-01", "000123"), created_at=NOW)]


def test_only_journaled_order_days_and_effective_rows_are_projected():
    rows = [
        fill(1),
        fill(2, filled_qty=Decimal("3")),
        fill(3, "999"),
        fill(4, filled_at=NOW - timedelta(days=1)),
        fill(5, quarantined_at=NOW),
    ]
    orders = intent_fill_orders(children(), rows)
    assert len(orders) == 1
    order = orders[0]
    assert order["broker_order_id"] == "000123"
    assert order["cumulative_qty"] == sum(f["qty"] for f in order["fills"]) == 5
    assert [f["fill_id"] for f in order["fills"]] == ["1", "2"]
    assert order["fills"][0] == {
        "fill_id": "1",
        "side": "buy",
        "qty": 2,
        "price": "70000.12500000",
        "filled_at": NOW.isoformat(),
        "committed_at": (NOW + timedelta(seconds=1)).isoformat(),
    }


def test_authority_supersedes_websocket_but_another_day_does_not():
    rows = [fill(1), fill(2, source="reconciler", filled_qty=Decimal("4"))]
    order = intent_fill_orders(children(), rows)[0]
    assert [f["fill_id"] for f in order["fills"]] == ["2"]
    assert order["cumulative_qty"] == 4
    rows[1].filled_at = NOW - timedelta(days=1)
    assert intent_fill_orders(children(), rows)[0]["cumulative_qty"] == 2


def test_cancel_does_not_advance_the_order_day_or_double_count():
    child = children()[0]
    cancel = replace(
        child, child_id=2, action="cancel", created_at=NOW + timedelta(days=1)
    )
    assert (
        intent_fill_orders(
            [child, cancel], [fill(1), fill(2, filled_at=cancel.created_at)]
        )[0]["cumulative_qty"]
        == 2
    )
    assert (
        intent_fill_orders([child, replace(child, child_id=3)], [fill()])[0][
            "cumulative_qty"
        ]
        == 2
    )
    assert intent_fill_orders([replace(child, broker_order_id=None)], [fill()]) == []
    assert intent_fill_orders([child], [])[0]["fills"] == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", "test-token")
    monkeypatch.setattr(
        settings,
        "EXECUTION_INTENT_GATEWAY_TOKEN_HEADER",
        "X-Execution-Intent-Gateway-Token",
    )
    app = create_app()
    intent = intent_snapshot()
    store = MemoryGatewayStore(intent)
    store.children = children()
    store._intent_of_child[1] = intent.intent_id
    app.dependency_overrides[route.get_gateway_reader] = lambda: store

    class Reader:
        async def read_fills(self, found, journal):
            assert found == intent
            assert tuple(journal) == tuple(children())
            return intent_fill_orders(journal, [fill()])

    app.dependency_overrides[route.get_fills_reader] = Reader
    with TestClient(app) as http:
        yield http, app, f"/execution-intents/intents/{intent.intent_id}/fills"


def test_response_is_closed_and_never_exposes_accounts(client):
    http, _, path = client
    answer = http.get(path, headers={"X-Execution-Intent-Gateway-Token": "test-token"})
    assert answer.status_code == 200
    assert set(answer.json()) == {"intent_id", "orders"}
    assert set(answer.json()["orders"][0]) == {
        "broker_order_id",
        "cumulative_qty",
        "fills",
    }
    assert "12345678901" not in answer.text
    assert "account" not in answer.text


@pytest.mark.parametrize("token", [None, "wrong"])
def test_token_is_required(client, token):
    http, app, path = client

    def trap():
        raise AssertionError("must not reach DB")

    app.dependency_overrides[route.get_gateway_reader] = trap
    headers = {} if token is None else {"X-Execution-Intent-Gateway-Token": token}
    headers["Cookie"] = "session=not-a-token"
    assert http.get(path, headers=headers).status_code == 401


def test_unset_token_is_403(client, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", "")
    assert client[0].get(client[2]).status_code == 403


def test_route_off_precedes_token_and_db(client, monkeypatch):
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", False)
    http, app, path = client

    def trap():
        raise AssertionError("must not reach DB")

    app.dependency_overrides[route.get_gateway_reader] = trap
    app.dependency_overrides[route.get_fills_reader] = trap
    assert http.get(path).status_code == 404


def test_unknown_intent_is_404(client):
    http, _, _ = client
    answer = http.get(
        f"/execution-intents/intents/{uuid4()}/fills",
        headers={"X-Execution-Intent-Gateway-Token": "test-token"},
    )
    assert answer.status_code == 404


def test_db_failure_is_sanitized_503(client):
    http, app, path = client

    class BrokenReader:
        async def read_fills(self, *args):
            raise RuntimeError("account secret")

    app.dependency_overrides[route.get_fills_reader] = BrokenReader
    answer = http.get(path, headers={"X-Execution-Intent-Gateway-Token": "test-token"})
    assert answer.status_code == 503
    assert answer.json() == {"detail": "query_unavailable"}
