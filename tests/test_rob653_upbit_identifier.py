# tests/test_rob653_upbit_identifier.py
import pytest

import app.services.brokers.upbit.orders as uorders


@pytest.mark.asyncio
async def test_place_buy_order_uses_supplied_identifier(monkeypatch):
    captured = {}

    async def _fake_request(method, url, body_params=None, query_params=None):
        captured["body"] = body_params
        return {"uuid": "x"}

    monkeypatch.setattr(uorders._client, "_request_with_auth", _fake_request)
    await uorders.place_buy_order(
        "KRW-BTC", "1000", "0.5", "limit", identifier="p6a-content"
    )
    assert captured["body"]["identifier"] == "p6a-content"


@pytest.mark.asyncio
async def test_place_buy_order_defaults_to_uuid_when_none(monkeypatch):
    captured = {}

    async def _fake_request(method, url, body_params=None, query_params=None):
        captured["body"] = body_params
        return {"uuid": "x"}

    monkeypatch.setattr(uorders._client, "_request_with_auth", _fake_request)
    await uorders.place_buy_order("KRW-BTC", "1000", "0.5", "limit")
    ident = captured["body"]["identifier"]
    assert ident and ident != "p6a-content"  # uuid4 fallback preserved
