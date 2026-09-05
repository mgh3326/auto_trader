"""Hermetic contract tests for the KIS live shadow witness."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.mcp_server.tooling import order_execution
from app.services.brokers.kis import live_shadow_witness as witness
from scripts import kis_live_witness_reconcile as reconcile

FIXTURES = Path("tests/fixtures/broker_edge")


def _intent(command_id: str = "command-1") -> dict[str, str]:
    return {
        "schema_version": "execution-command/v1",
        "command_id": command_id,
        "account_scope": "kis_live",
        "side": "buy",
        "stock_code": "005930",
        "quantity": "2",
        "price": "70000",
        "order_type": "limit",
        "issued_at": "2026-09-05T00:00:00Z",
    }


def _receipt(command_id: str = "command-1") -> dict[str, str]:
    return {
        "witness_id": "witness-1",
        "command_id": command_id,
        "recorded_at": "2026-09-05T00:00:01Z",
        "mode": "shadow",
    }


async def _settle(client: witness.LiveShadowWitness, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if not client._tasks:
            return
        await asyncio.sleep(0.01)
    pytest.fail("witness tasks did not settle")


def _transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/commands":
            return httpx.Response(200, json=_receipt())
        assert request.url.path == "/v1/commands/command-1/echo"
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_intent_and_raw_echo_are_immutable_and_schema_valid() -> None:
    requests: list[httpx.Request] = []
    original = _intent()
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", original, transport=_transport(requests)
    )
    client.start()
    original["price"] = "1"
    raw_response: dict[str, Any] = {
        "output": {"ODNO": "000123"},
        "rt_cd": "0",
        "msg_cd": "M0000",
        "msg1": "accepted",
    }
    client.capture_raw_echo(raw_response)
    raw_response["output"]["ODNO"] = "mutated"
    await _settle(client, attempts=300)

    bodies = [json.loads(request.content) for request in requests]
    assert bodies[0]["price"] == "70000"
    assert bodies[1]["ODNO"] == "000123"
    assert all(request.headers.get("authorization") is None for request in requests)

    schema = json.loads(
        (FIXTURES / "kis_live_shadow_witness_v1.schema.json").read_text()
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validator.validate({"intent": bodies[0]})
    validator.validate({"echo": bodies[1]})


@pytest.mark.asyncio
async def test_invalid_receipt_skips_echo_without_raising() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"mode": "shadow"})

    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=httpx.MockTransport(handler)
    )
    client.start()
    client.capture_raw_echo(
        {"output": {"ODNO": "000123"}, "rt_cd": "0", "msg_cd": "M0000", "msg1": "ok"}
    )
    await _settle(client)
    assert [request.url.path for request in requests] == ["/v1/commands"]


@pytest.mark.asyncio
async def test_background_timeout_is_bounded_and_fail_open() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_receipt())

    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=httpx.MockTransport(handler)
    )
    started = time.perf_counter()
    client.start()
    assert time.perf_counter() - started < 0.01
    await _settle(client)
    assert time.perf_counter() - started < 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 500])
async def test_edge_error_is_background_fail_open(status: int) -> None:
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080",
        _intent(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": "x"})
        ),
    )
    started = time.perf_counter()
    client.start()
    assert time.perf_counter() - started < 0.01
    await _settle(client)


@pytest.mark.asyncio
async def test_echo_waits_at_most_two_seconds_for_intent_task() -> None:
    client = witness.LiveShadowWitness("http://127.0.0.1:8080", _intent())

    async def delayed_receipt() -> object:
        await asyncio.sleep(5)
        return _receipt()

    client._intent_task = asyncio.create_task(delayed_receipt())
    started = time.perf_counter()
    client.capture_raw_echo(
        {"output": {"ODNO": "000123"}, "rt_cd": "0", "msg_cd": "M0000", "msg1": "ok"}
    )
    await _settle(client, attempts=300)
    assert 1.9 < time.perf_counter() - started < 2.5
    client._intent_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await client._intent_task


def test_default_off_invalid_config_and_non_limit_never_schedule(monkeypatch) -> None:
    monkeypatch.delenv("KIS_LIVE_SHADOW_WITNESS_ENABLED", raising=False)
    assert (
        witness.start_kis_live_shadow_witness(
            command_id="command-1",
            side="buy",
            stock_code="005930",
            quantity=2,
            price=70000,
            kis_order_code="00",
        )
        is None
    )
    monkeypatch.setenv("KIS_LIVE_SHADOW_WITNESS_ENABLED", "true")
    monkeypatch.setenv("EDGE_WITNESS_URL", "https://not-loopback.example")
    assert witness.witness_base_url() is None
    assert (
        witness.start_kis_live_shadow_witness(
            command_id="command-1",
            side="buy",
            stock_code="005930",
            quantity=2,
            price=70000,
            kis_order_code="00",
        )
        is None
    )
    monkeypatch.setenv("EDGE_WITNESS_URL", "http://127.0.0.1:8080")
    assert (
        witness.start_kis_live_shadow_witness(
            command_id="command-1",
            side="hold",
            stock_code="005930",
            quantity=2,
            price=70000,
            kis_order_code="00",
        )
        is None
    )
    assert (
        witness.start_kis_live_shadow_witness(
            command_id="command-1",
            side="buy",
            stock_code="005930",
            quantity=2,
            price=0,
            kis_order_code="01",
        )
        is None
    )


@pytest.mark.asyncio
async def test_live_broker_arguments_are_identical_with_witness_off_or_on(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeKIS:
        async def order_korea_stock(self, **kwargs: object) -> dict[str, str]:
            calls.append(kwargs)
            return {"rt_cd": "0", "odno": "KIS-1"}

    monkeypatch.setattr(order_execution, "_create_kis_client", lambda **_: FakeKIS())
    monkeypatch.setattr(
        order_execution, "get_kr_security_type", AsyncMock(return_value=None)
    )
    monkeypatch.delenv("KIS_LIVE_SHADOW_WITNESS_ENABLED", raising=False)
    off = await order_execution._execute_kr_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=2,
        price=70000,
        idempotency_key="command-1",
    )
    monkeypatch.setenv("KIS_LIVE_SHADOW_WITNESS_ENABLED", "true")
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        order_execution,
        "start_kis_live_shadow_witness",
        lambda **kwargs: observed.append(kwargs) or None,
    )
    on = await order_execution._execute_kr_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=2,
        price=70000,
        idempotency_key="command-1",
    )
    assert off == on == {"rt_cd": "0", "odno": "KIS-1"}
    assert calls[0] == calls[1]
    assert observed[0]["kis_order_code"] == "00"


@pytest.mark.asyncio
async def test_witness_setup_failure_does_not_block_live_broker_call(
    monkeypatch,
) -> None:
    broker = AsyncMock(return_value={"rt_cd": "0", "odno": "KIS-1"})
    fake_kis = type("FakeKIS", (), {"order_korea_stock": broker})()
    monkeypatch.setattr(order_execution, "_create_kis_client", lambda **_: fake_kis)
    monkeypatch.setattr(
        order_execution, "get_kr_security_type", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        order_execution,
        "start_kis_live_shadow_witness",
        lambda **_: (_ for _ in ()).throw(RuntimeError("witness unavailable")),
    )
    result = await order_execution._execute_kr_order(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=2,
        price=70000,
        idempotency_key="command-1",
    )
    assert result == {"rt_cd": "0", "odno": "KIS-1"}
    broker.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (json.loads((FIXTURES / "missing-echo-empty.json").read_text()), []),
        (
            json.loads((FIXTURES / "missing-echo-one.json").read_text()),
            json.loads((FIXTURES / "missing-echo-one.json").read_text())["witnesses"],
        ),
    ],
)
async def test_reconcile_supports_actual_null_and_array_shapes(
    monkeypatch, payload: dict[str, object], expected: list[dict[str, str]]
) -> None:
    monkeypatch.setenv("EDGE_WITNESS_URL", "http://127.0.0.1:8080")
    requests: list[httpx.Request] = []
    result = await witness.fetch_missing_echoes(
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json=payload)
            )
        )
    )
    assert result == expected
    assert dict(requests[0].url.params) == {"scope": "kis_live", "missing_echo": "true"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"witnesses": {}}, {"witnesses": [{"mode": "shadow"}]}]
)
async def test_reconcile_refuses_malformed_or_error_as_empty(
    monkeypatch, payload: dict[str, object]
) -> None:
    monkeypatch.setenv("EDGE_WITNESS_URL", "http://127.0.0.1:8080")
    with pytest.raises(witness.WitnessReconcileError):
        await witness.fetch_missing_echoes(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        )
    with pytest.raises(witness.WitnessReconcileError):
        await witness.fetch_missing_echoes(
            transport=httpx.MockTransport(lambda _: httpx.Response(503, json={}))
        )


@pytest.mark.parametrize(
    ("result", "expected_exit"),
    [([], 0), ([_receipt()], 1)],
)
def test_reconcile_cli_exit_for_empty_and_missing(
    monkeypatch, capsys, result: list[dict[str, str]], expected_exit: int
) -> None:
    async def fake_fetch() -> list[dict[str, str]]:
        return result

    monkeypatch.setattr(reconcile, "fetch_missing_echoes", fake_fetch)
    assert reconcile.main() == expected_exit
    assert f"missing_echo_count={len(result)}" in capsys.readouterr().out


def test_reconcile_cli_query_error_is_nonzero(monkeypatch, capsys) -> None:
    async def fake_fetch() -> list[dict[str, str]]:
        raise witness.WitnessReconcileError("query_failed")

    monkeypatch.setattr(reconcile, "fetch_missing_echoes", fake_fetch)
    assert reconcile.main() == 2
    assert "reconcile failed" in capsys.readouterr().out
