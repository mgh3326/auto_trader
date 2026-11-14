"""Hermetic contract tests for the KIS live shadow witness."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.mcp_server.tooling import order_execution
from app.services.brokers.kis import domestic_orders
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


class _DomesticSettings:
    kis_account_no = "1234567890"
    kis_access_token = "test-token"


class _DomesticTokenManager:
    async def clear_token(self) -> None:
        return None


class _DomesticParent:
    _settings = _DomesticSettings()
    _hdr_base: dict[str, str] = {}
    _token_manager = _DomesticTokenManager()

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.bodies: list[dict[str, object]] = []

    async def _ensure_token(self) -> None:
        return None

    def _kis_url(self, path: str) -> str:
        return f"http://kis.invalid{path}"

    async def _request_with_rate_limit(
        self, *_: object, **kwargs: object
    ) -> dict[str, object]:
        self.bodies.append(kwargs["json_body"])
        return self.response


def _witness() -> witness.LiveShadowWitness:
    return witness.LiveShadowWitness("http://127.0.0.1:8080", _intent())


_REJECTION: dict[str, object] = {
    "rt_cd": "1",
    "msg_cd": "40240000",
    "msg1": "주문가능금액을 초과",
}


def _echo_requests(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [request for request in requests if request.url.path.endswith("/echo")]


def _invalid_echo_events(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage() == "kis_live_witness_echo_invalid"
    ]


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
    assert list(bodies[1]) == ["ODNO", "rt_cd", "msg_cd", "msg1", "received_at"]
    assert bodies[1]["ODNO"] == "000123"
    assert bodies[1]["rt_cd"] == "0"
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
@pytest.mark.parametrize(
    "response",
    [
        _REJECTION,
        {**_REJECTION, "output": None},
        {**_REJECTION, "output": {}},
        {**_REJECTION, "output": {"ODNO": None}},
        {**_REJECTION, "output": {"ODNO": 7}},
        {**_REJECTION, "output": {"ODNO": ""}},
    ],
    ids=[
        "absent-output",
        "null-output",
        "empty-output",
        "null-odno",
        "int-odno",
        "empty-odno",
    ],
)
async def test_broker_answered_rejection_is_echoed_without_order_number(
    response: dict[str, object],
) -> None:
    requests: list[httpx.Request] = []
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    client.start()
    client.capture_raw_echo(response)
    await _settle(client, attempts=300)

    echoes = _echo_requests(requests)
    assert len(echoes) == 1
    body = json.loads(echoes[0].content)
    assert list(body) == ["ODNO", "rt_cd", "msg_cd", "msg1", "received_at"]
    assert body["ODNO"] == ""
    assert body["rt_cd"] == "1"
    assert body["msg_cd"] == "40240000"
    assert body["msg1"] == "주문가능금액을 초과"
    schema = json.loads(
        (FIXTURES / "kis_live_shadow_witness_v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate({"echo": body})


@pytest.mark.asyncio
async def test_accepted_echo_without_order_number_is_dropped(caplog) -> None:
    requests: list[httpx.Request] = []
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    client.start()
    with caplog.at_level(logging.WARNING, logger=witness.logger.name):
        client.capture_raw_echo(
            {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "ok", "output": {}}
        )
    await _settle(client)
    assert [e.invalid_fields for e in _invalid_echo_events(caplog)] == ["ODNO"]
    assert _echo_requests(requests) == []


@pytest.mark.asyncio
async def test_invalid_echo_names_fields_but_never_values(caplog) -> None:
    requests: list[httpx.Request] = []
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    client.start()
    with caplog.at_level(logging.WARNING, logger=witness.logger.name):
        client.capture_raw_echo(
            {
                "rt_cd": "1",
                "msg_cd": 40240000,
                "msg1": "SECRET-BROKER-MESSAGE",
                "output": {"ODNO": "000123"},
            }
        )
    await _settle(client)
    assert [e.invalid_fields for e in _invalid_echo_events(caplog)] == ["msg_cd"]
    assert _echo_requests(requests) == []
    assert "40240000" not in caplog.text
    assert "SECRET-BROKER-MESSAGE" not in caplog.text


@pytest.mark.asyncio
async def test_non_string_rt_cd_never_substitutes_an_order_number(caplog) -> None:
    requests: list[httpx.Request] = []
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    client.start()
    with caplog.at_level(logging.WARNING, logger=witness.logger.name):
        client.capture_raw_echo({"rt_cd": None, "msg_cd": "m", "msg1": "v"})
    await _settle(client)
    assert [e.invalid_fields for e in _invalid_echo_events(caplog)] == ["ODNO,rt_cd"]
    assert _echo_requests(requests) == []


@pytest.mark.asyncio
async def test_schema_invalid_echo_is_dropped_with_named_field(caplog) -> None:
    requests: list[httpx.Request] = []
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    client._intent_task = asyncio.create_task(asyncio.sleep(0, result=_receipt()))
    with caplog.at_level(logging.WARNING, logger=witness.logger.name):
        await client._post_echo(
            {
                "ODNO": "",
                "rt_cd": "1",
                "msg_cd": "40240000",
                "msg1": "exceeded",
                "received_at": "not-a-timestamp",
            }
        )
    await client._intent_task
    assert [e.invalid_fields for e in _invalid_echo_events(caplog)] == ["received_at"]
    assert _echo_requests(requests) == []


@pytest.mark.asyncio
async def test_domestic_rejection_still_raises_and_echoes_without_odno(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )
    requests: list[httpx.Request] = []
    active = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )
    active.start()
    parent = _DomesticParent(
        {"rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가능금액을 초과"}
    )
    token = witness.activate(active)
    try:
        with pytest.raises(RuntimeError, match="40240000 주문가능금액을 초과"):
            await domestic_orders.DomesticOrderClient(parent).order_korea_stock(
                "005930", "buy", 2, 70000, is_mock=False
            )
    finally:
        witness.deactivate(token)
    await _settle(active, attempts=300)
    echoes = _echo_requests(requests)
    assert len(echoes) == 1
    assert json.loads(echoes[0].content)["ODNO"] == ""
    assert len(parent.bodies) == 1


@pytest.mark.asyncio
async def test_invalid_intent_schema_never_reaches_transport() -> None:
    requests: list[httpx.Request] = []
    invalid = _intent()
    invalid["side"] = "hold"
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", invalid, transport=_transport(requests)
    )
    client.start()
    await _settle(client)
    assert requests == []


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
async def test_slow_default_client_construction_cannot_delay_yielding_broker(
    monkeypatch,
) -> None:
    requests: list[httpx.Request] = []
    original = witness.LiveShadowWitness._make_client

    def slow_constructor(client: witness.LiveShadowWitness) -> httpx.AsyncClient:
        time.sleep(0.05)
        return original(client)

    monkeypatch.setattr(witness.LiveShadowWitness, "_make_client", slow_constructor)
    client = witness.LiveShadowWitness(
        "http://127.0.0.1:8080", _intent(), transport=_transport(requests)
    )

    async def yielding_broker() -> None:
        await asyncio.sleep(0)

    started = time.perf_counter()
    client.start()
    await yielding_broker()
    assert time.perf_counter() - started < 0.01
    await _settle(client)
    assert len(requests) == 1


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
async def test_raising_witness_logging_filter_preserves_provider_rejection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )
    parent = _DomesticParent(
        {"rt_cd": "1", "msg_cd": "40240000", "msg1": "주문가능금액을 초과"}
    )

    class RaisingFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            raise RuntimeError("logging filter exploded")

    raising_filter = RaisingFilter()
    witness.logger.addFilter(raising_filter)
    token = witness.activate(_witness())
    try:
        with pytest.raises(RuntimeError, match="40240000 주문가능금액을 초과"):
            await domestic_orders.DomesticOrderClient(parent).order_korea_stock(
                "005930", "buy", 2, 70000, is_mock=False
            )
    finally:
        witness.deactivate(token)
        witness.logger.removeFilter(raising_filter)
    assert len(parent.bodies) == 1


@pytest.mark.asyncio
async def test_schedule_and_sentry_failure_preserve_accepted_domestic_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        witness.sentry_sdk,
        "add_breadcrumb",
        lambda **_: (_ for _ in ()).throw(RuntimeError("sentry unavailable")),
    )

    def no_task(coroutine: object) -> object:
        coroutine.close()  # type: ignore[attr-defined]
        raise RuntimeError("no task slots")

    monkeypatch.setattr(asyncio, "create_task", no_task)
    active = _witness()
    active.start()
    parent = _DomesticParent(
        {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "ok",
            "output": {"ODNO": "9", "ORD_TMD": "1"},
        }
    )
    token = witness.activate(active)
    try:
        result = await domestic_orders.DomesticOrderClient(parent).order_korea_stock(
            "005930", "buy", 2, 70000, is_mock=False
        )
    finally:
        witness.deactivate(token)
    assert result == {
        "odno": "9",
        "ord_tmd": "1",
        "msg": "ok",
        "rt_cd": "0",
        "msg_cd": "MCA00000",
    }
    assert len(parent.bodies) == 1


@pytest.mark.asyncio
async def test_raw_echo_capture_failure_preserves_accepted_domestic_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        domestic_orders, "is_nxt_eligible", AsyncMock(return_value=False)
    )

    class RaisingWitness:
        def capture_raw_echo(self, _: dict[str, object]) -> None:
            raise RuntimeError("raw echo capture failed")

    parent = _DomesticParent(
        {
            "rt_cd": "0",
            "msg_cd": "MCA00000",
            "msg1": "ok",
            "output": {"ODNO": "10", "ORD_TMD": "2"},
        }
    )
    token = witness.activate(RaisingWitness())
    try:
        result = await domestic_orders.DomesticOrderClient(parent).order_korea_stock(
            "005930", "buy", 2, 70000, is_mock=False
        )
    finally:
        witness.deactivate(token)
    assert result == {
        "odno": "10",
        "ord_tmd": "2",
        "msg": "ok",
        "rt_cd": "0",
        "msg_cd": "MCA00000",
    }
    assert len(parent.bodies) == 1


@pytest.mark.asyncio
async def test_setup_failure_with_raising_witness_logger_still_calls_broker(
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

    class RaisingFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            raise RuntimeError("logging filter exploded")

    raising_filter = RaisingFilter()
    witness.logger.addFilter(raising_filter)
    try:
        result = await order_execution._execute_kr_order(
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity=2,
            price=70000,
            idempotency_key="command-1",
        )
    finally:
        witness.logger.removeFilter(raising_filter)
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
