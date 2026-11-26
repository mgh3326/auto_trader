from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx

import pytest

from app.services.brokers.upbit import client
from scripts import upbit_order_check as check

pytestmark = pytest.mark.usefixtures("allow_external_providers")


def _chance() -> dict:
    return {
        "market": {
            "bid": {"min_total": "5000"},
            "ask": {"min_total": "5000"},
            "max_total": "1000000000",
        },
        "bid_fee": "0.0005",
        "maker_bid_fee": "0.0001",
        "bid_account": {"currency": "KRW", "balance": "100000", "locked": "500"},
        "ask_account": {"currency": "ETH", "balance": "1.5", "locked": "0.2"},
    }


def test_chance_parsing_uses_side_minimum_and_unlocked_balance() -> None:
    errors, shown = check._local_verdict(
        _chance(), "KRW-ETH", "bid", check.Decimal("2500"), check.Decimal("2")
    )

    assert errors == []
    assert shown == {
        "KRW": {"balance": "100000", "locked": "500"},
        "ETH": {"balance": "1.5", "locked": "0.2"},
    }


@pytest.mark.parametrize(
    ("price", "volume", "expected"),
    [("2500.5", "2", "price_tick_invalid"), ("2500", "1", "min_total_not_met")],
)
def test_tick_and_min_total_refuse_locally(price, volume, expected) -> None:
    errors, _ = check._local_verdict(
        _chance(), "KRW-ETH", "bid", check.Decimal(price), check.Decimal(volume)
    )
    assert expected in errors


def test_bid_fee_is_included_in_required_quote_balance() -> None:
    chance = _chance()
    chance["bid_account"]["balance"] = "10000"

    errors, _ = check._local_verdict(
        chance, "KRW-ETH", "bid", check.Decimal("5000"), check.Decimal("2")
    )

    assert "available_balance_insufficient" in errors


def test_market_max_total_refuses_locally() -> None:
    chance = _chance()
    chance["market"]["max_total"] = "5000"

    errors, _ = check._local_verdict(
        chance, "KRW-ETH", "bid", check.Decimal("2500"), check.Decimal("2.1")
    )

    assert "max_total_exceeded" in errors


@pytest.mark.asyncio
async def test_non_krw_market_is_rejected_before_chance_request(monkeypatch, capsys) -> None:
    chance = AsyncMock(return_value=_chance())
    monkeypatch.setattr(
        check,
        "_upbit_client",
        lambda: SimpleNamespace(order_chance=chance, order_test=AsyncMock()),
    )
    args = argparse.Namespace(
        market="USDT-ETH", side="bid", price="100.001", volume="1", confirm_test=False
    )

    assert await check._run(args) == 2
    chance.assert_not_awaited()
    assert "unavailable:ValueError" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_name", "expected"),
    [
        (400, "invalid_volume", "rejected:invalid_volume"),
        (403, "market_offline", "rejected:market_offline"),
        (403, "out_of_scope", "unavailable:HTTPStatusError"),
        (401, "invalid_access_key", "unavailable:HTTPStatusError"),
    ],
)
async def test_order_test_only_reports_validation_errors_as_rejected(
    monkeypatch, capsys, status, error_name, expected
) -> None:
    response = httpx.Response(
        status,
        json={"error": {"name": error_name}},
        request=httpx.Request("POST", "https://api.upbit.com/v1/orders/test"),
    )
    failure = httpx.HTTPStatusError("request failed", request=response.request, response=response)
    post = AsyncMock(side_effect=failure)
    monkeypatch.setattr(
        check,
        "_upbit_client",
        lambda: SimpleNamespace(order_chance=AsyncMock(return_value=_chance()), order_test=post),
    )
    monkeypatch.setenv("UPBIT_ORDER_TEST_ENABLED", "true")
    args = argparse.Namespace(
        market="KRW-ETH", side="bid", price="2500", volume="2", confirm_test=True
    )

    assert await check._run(args) == 1
    assert f'"verdict": "{expected}"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_auth_retry_builds_a_fresh_nonce_for_each_send(monkeypatch) -> None:
    payloads = []
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(
                200, json={}, request=httpx.Request("GET", url)
            )

    async def fake_retry(limiter, send_fn, *, url, **kwargs):
        captured.update(retry_options=kwargs)
        await send_fn()
        await send_fn()
        return {}

    monkeypatch.setattr(client, "get_limiter", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(client, "_retry_with_backoff", fake_retry)
    monkeypatch.setattr(
        client.jwt,
        "encode",
        lambda payload, key: payloads.append(payload.copy()) or f'token-{len(payloads)}',
    )

    await client._request_with_auth(
        "GET", "https://api.upbit.com/v1/orders/chance", query_params={"market": "KRW-ETH"}
    )

    assert len(payloads) == 2
    assert payloads[0]["nonce"] != payloads[1]["nonce"]
    assert captured["client_options"]["follow_redirects"] is False
    assert captured["retry_options"]["retry_request_errors"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("price", "volume"), [("2500.5", "2"), ("2500", "1")])
async def test_local_refusal_prevents_order_test_network(
    monkeypatch, price, volume
) -> None:
    post = AsyncMock()
    monkeypatch.setattr(
        check,
        "_upbit_client",
        lambda: SimpleNamespace(
            order_chance=AsyncMock(return_value=_chance()), order_test=post
        ),
    )
    monkeypatch.setenv("UPBIT_ORDER_TEST_ENABLED", "true")
    args = argparse.Namespace(
        market="KRW-ETH", side="bid", price=price, volume=volume, confirm_test=True
    )

    assert await check._run(args) == 1
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_test_rejects_mutated_path_before_request(monkeypatch) -> None:
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        client, "UPBIT_ORDER_TEST_URL", "https://api.upbit.com/v1/orders"
    )
    monkeypatch.setenv("UPBIT_ORDER_TEST_ENABLED", "true")
    monkeypatch.setattr(client, "_request_with_auth", forbidden)
    with pytest.raises(ValueError, match="path"):
        await client.order_test("KRW-ETH", "bid", "limit", "2500", "1")
    assert called is False


@pytest.mark.asyncio
async def test_order_test_uses_one_shot_signed_endpoint(monkeypatch) -> None:
    captured = {}

    async def fake_request(method, url, body_params=None, **kwargs):
        captured.update(method=method, url=url, body=body_params)
        return {"uuid": "validation-only"}

    monkeypatch.setattr(client, "_request_with_auth", fake_request)
    monkeypatch.setenv("UPBIT_ORDER_TEST_ENABLED", "true")
    result = await client.order_test("KRW-ETH", "bid", "limit", "2500", "1")
    assert result == {"uuid": "validation-only"}
    assert captured["url"] == "https://api.upbit.com/v1/orders/test"
    assert captured["method"] == "POST"


@pytest.mark.asyncio
async def test_order_test_disables_transport_retries(monkeypatch) -> None:
    from unittest.mock import MagicMock

    captured = {}

    async def fake_retry(limiter, send_fn, *, url, **kwargs):
        captured.update(url=url, **kwargs)
        return {"ok": True}

    monkeypatch.setattr(client, "_retry_with_backoff", fake_retry)
    monkeypatch.setattr(client, "get_limiter", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(client.jwt, "encode", lambda *args, **kwargs: "token")
    monkeypatch.setenv("UPBIT_ORDER_TEST_ENABLED", "true")

    await client.order_test("KRW-ETH", "bid", "limit", "2500", "1")

    assert captured["max_retries"] == 0
    assert captured["retry_request_errors"] is False


@pytest.mark.asyncio
async def test_order_test_client_gate_is_default_off(monkeypatch) -> None:
    called = False

    async def forbidden(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(client, "_request_with_auth", forbidden)
    monkeypatch.delenv("UPBIT_ORDER_TEST_ENABLED", raising=False)
    with pytest.raises(PermissionError, match="disabled"):
        await client.order_test("KRW-ETH", "bid", "limit", "2500", "1")
    assert called is False


@pytest.mark.asyncio
async def test_gate_off_means_no_post(monkeypatch, capsys) -> None:
    chance = AsyncMock(return_value=_chance())
    post = AsyncMock()
    monkeypatch.setattr(
        check,
        "_upbit_client",
        lambda: SimpleNamespace(order_chance=chance, order_test=post),
    )
    monkeypatch.delenv("UPBIT_ORDER_TEST_ENABLED", raising=False)
    args = argparse.Namespace(
        market="KRW-ETH", side="bid", price="2500", volume="2", confirm_test=True
    )

    assert await check._run(args) == 1
    assert chance.await_count == 1
    post.assert_not_awaited()
    assert "OrderTestDisabled" in capsys.readouterr().out
