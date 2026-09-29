from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.brokers.binance.h5.client import (
    H5BrokerTruthUnavailable,
    H5DemoClient,
)


def _client(body):
    calls = []

    def dispatch(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=body)

    client = H5DemoClient(api_key="FAKE", api_secret="FAKE")
    client._client = httpx.AsyncClient(
        base_url="https://demo-fapi.binance.com",
        transport=httpx.MockTransport(dispatch),
    )
    return client, calls


@pytest.mark.parametrize(
    "body", [None, {}, {"code": -1000}, [], [{"symbol": "BTCUSDT"}]]
)
def test_incomplete_positions_are_not_treated_as_flat(body):
    client, _ = _client(body)
    with pytest.raises(H5BrokerTruthUnavailable):
        asyncio.run(client.get_all_positions())


def test_empty_open_orders_are_valid_but_nonarray_response_is_blocked():
    clean, _ = _client([])
    assert asyncio.run(clean.get_all_open_orders()).orders == []
    malformed, _ = _client({})
    with pytest.raises(H5BrokerTruthUnavailable):
        asyncio.run(malformed.get_all_open_orders())


@pytest.mark.parametrize("body", [{}, {"dualSidePosition": "false"}, []])
def test_missing_or_coerced_position_mode_is_blocked(body):
    client, _ = _client(body)
    with pytest.raises(H5BrokerTruthUnavailable):
        asyncio.run(client.get_position_mode())


def test_order_evidence_cannot_use_request_identity_as_broker_echo():
    body = {
        "orderId": 847,
        "side": "BUY",
        "type": "MARKET",
        "status": "FILLED",
        "origQty": "1",
        "executedQty": "1",
        "avgPrice": "100",
        "reduceOnly": False,
        "positionSide": "BOTH",
    }
    client, _ = _client(body)
    with pytest.raises(H5BrokerTruthUnavailable, match="actual broker echo"):
        asyncio.run(client.get_order(symbol="BTCUSDT", client_order_id="h5-one"))


@pytest.mark.parametrize(
    "changes",
    [{"symbol": "XRPUSDT"}, {"client_order_id": "dfc-order"}, {"confirm": False}],
)
def test_non_h5_mutation_or_missing_confirm_has_zero_dispatch(monkeypatch, changes):
    monkeypatch.setenv("BINANCE_H5_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    client, calls = _client({})
    args = {"symbol": "BTCUSDT", "client_order_id": "h5-one", "confirm": True}
    args.update(changes)
    with pytest.raises(ValueError):
        asyncio.run(client.submit_order(**args))
    assert calls == []


def test_disabled_h5_gate_and_inherited_mutations_have_zero_dispatch(monkeypatch):
    monkeypatch.delenv("BINANCE_H5_DEMO_ENABLED", raising=False)
    client, calls = _client({})
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(
            client.submit_order(
                symbol="BTCUSDT", client_order_id="h5-one", confirm=True
            )
        )
    for method in (client.set_leverage, client.cancel_order, client.order_test):
        with pytest.raises(ValueError):
            asyncio.run(method())
    assert calls == []
