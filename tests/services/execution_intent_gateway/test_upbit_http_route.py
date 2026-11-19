"""hk 5441 — the HTTP route for an ``upbit_live`` intent (design §3.3, §5 R12).

The route keeps its gates (404 when off, token, confirm header) and serves the
new account through the same ``POST /children``. The only new route is a
read-only list.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.routers import execution_intent_gateway as route
from app.routers.execution_intent_gateway import CONFIRM_HEADER
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.execution_intent_gateway.upbit_gateway import UpbitGatewayPath
from app.services.execution_intent_gateway.upbit_store import intent_json
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    FakeBroker,
    FakeKillSwitch,
    MemoryGatewayStore,
    intent_snapshot,
)
from tests.services.execution_intent_gateway.upbit_support import (
    LIVE_ENV,
    NOW,
    FakeLiveForwarder,
    FakeShadowForwarder,
    MemoryUpbitStore,
    facts,
    upbit_intent,
)

pytestmark = pytest.mark.unit

TOKEN = "t" * 32
TOKEN_HEADER = "X-Execution-Intent-Gateway-Token"
CHILDREN = "/execution-intents/children"
INTENTS = "/execution-intents/intents"


class Harness:
    def __init__(self) -> None:
        self.kis_intent = intent_snapshot(expires_at=NOW + timedelta(hours=1))
        self.intent = upbit_intent()
        self.upbit_store = MemoryUpbitStore(self.intent, facts=facts())
        self.kis_store = MemoryGatewayStore(self.kis_intent)
        # One journal table holds both accounts; the in-memory stores are two.
        kis_intent = self.kis_intent
        upbit_account_of = self.upbit_store.account_of

        async def account_of(intent_id: Any) -> str | None:
            if intent_id == kis_intent.intent_id:
                return kis_intent.account
            return await upbit_account_of(intent_id)

        self.upbit_store.account_of = account_of  # type: ignore[method-assign]
        self.kis_broker = FakeBroker()
        self.live = FakeLiveForwarder()

        async def avg(_symbol: str) -> Decimal | None:
            return Decimal("2500")

        async def cooldown(_symbol: str) -> bool:
            return False

        upbit = UpbitGatewayPath(
            store=self.upbit_store,
            kill_switch=FakeKillSwitch(),
            shadow_forwarder=FakeShadowForwarder(),
            live_forwarder=self.live,
            avg_price_reader=avg,
            cooldown_reader=cooldown,
            environment=LIVE_ENV,
            clock=lambda: NOW,
        )
        self.gateway = IntentGateway(
            store=self.kis_store,
            forwarders={"kis_mock": self.kis_broker},
            kill_switch=FakeKillSwitch(),
            environment=ENABLED,
            clock=lambda: NOW,
            upbit=upbit,
        )
        self.listed: list[dict[str, Any]] = [intent_json(self.intent)]
        self.list_error: Exception | None = None
        harness = self

        class _ListStore:
            async def list_open_intents(self, account: str, *, now: Any) -> list[dict]:
                if harness.list_error is not None:
                    raise harness.list_error
                return harness.listed

        self.app = create_app()
        self.app.dependency_overrides[route.get_intent_gateway] = lambda: self.gateway
        self.app.dependency_overrides[route.get_upbit_store] = _ListStore
        self.client = TestClient(self.app)

    def headers(self, key: str | None = None) -> dict[str, str]:
        headers = {TOKEN_HEADER: TOKEN, "Content-Type": "application/json"}
        if key is not None:
            headers[CONFIRM_HEADER] = key
        return headers

    def post(self, body: dict[str, Any]) -> Any:
        return self.client.post(
            CHILDREN,
            content=json.dumps(body),
            headers=self.headers(body["idempotency_key"]),
        )

    def upbit_body(self, key: str = "child-0001", **overrides: Any) -> dict[str, Any]:
        return {
            "action": "submit",
            "intent_id": str(self.intent.intent_id),
            "idempotency_key": key,
            "quantity_decimal": "10",
            "limit_price_krw": 3_000,
            **overrides,
        }


@pytest.fixture(autouse=True)
def _route_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN_HEADER", TOKEN_HEADER)


def test_an_upbit_submit_goes_through_the_same_route_once() -> None:
    h = Harness()
    first = h.post(h.upbit_body()).json()
    again = h.post(h.upbit_body()).json()
    assert first["status"] == "accepted" and again["replayed"] is True
    assert h.live.calls == [("submit", "child-0001")]
    assert h.kis_broker.calls == []


def test_an_upbit_child_without_the_confirm_header_is_refused_before_any_read() -> None:
    h = Harness()
    answer = h.client.post(
        CHILDREN, content=json.dumps(h.upbit_body()), headers=h.headers("another-key")
    ).json()
    assert answer["reason"] == "confirm_required"
    assert h.upbit_store.children == [] and h.live.calls == []


def test_a_kis_shaped_child_against_an_upbit_intent_decides_nothing() -> None:
    h = Harness()
    body = h.upbit_body()
    body.pop("quantity_decimal")
    body["quantity"] = 10
    answer = h.post(body).json()
    assert answer["reason"] == "child_shape_invalid_for_account"
    assert h.upbit_store.children == [] and h.live.calls == []


def test_an_upbit_shaped_child_against_a_kis_intent_decides_nothing() -> None:
    h = Harness()
    answer = h.post(h.upbit_body(intent_id=str(h.kis_intent.intent_id))).json()
    assert answer["reason"] == "child_shape_invalid_for_account"
    assert h.kis_store.children == [] and h.kis_broker.calls == []


def test_an_amend_against_an_upbit_intent_is_422() -> None:
    h = Harness()
    body = {
        "action": "amend",
        "intent_id": str(h.intent.intent_id),
        "idempotency_key": "child-0002",
        "target_idempotency_key": "child-0001",
        "quantity": 1,
        "limit_price_krw": 3_000,
    }
    answer = h.post(body)
    assert answer.status_code == 422
    assert answer.json() == {"detail": "amend_unsupported_for_account"}
    assert h.upbit_store.children == [] and h.live.calls == []


def test_the_kis_mock_path_is_unchanged_with_the_upbit_path_configured() -> None:
    h = Harness()
    body = {
        "action": "submit",
        "intent_id": str(h.kis_intent.intent_id),
        "idempotency_key": "child-0001",
        "quantity": 1,
        "limit_price_krw": 70_000,
    }
    answer = h.post(body).json()
    assert answer["status"] == "accepted"
    assert h.kis_broker.calls == [("submit", "child-0001")] and h.live.calls == []


@pytest.mark.parametrize(
    "body_overrides",
    [
        {"quantity_decimal": "0.123456789"},
        {"quantity_decimal": 10},
        {"quantity_decimal": "10", "account": "upbit_live"},
        {"quantity_decimal": "10", "limit_price_krw": 3000.5},
    ],
)
def test_an_invalid_upbit_body_is_422_and_decides_nothing(body_overrides) -> None:
    h = Harness()
    answer = h.post(h.upbit_body(**body_overrides))
    assert answer.status_code == 422
    assert answer.json() == {"detail": "invalid_child_request"}
    assert h.upbit_store.children == []


# ------------------------------------------------------------ GET /intents (R12)


def test_the_list_returns_open_upbit_intents_without_account_or_mandate_terms() -> None:
    h = Harness()
    answer = h.client.get(
        INTENTS, params={"account": "upbit_live", "open": "true"}, headers=h.headers()
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["account"] == "upbit_live"
    [listed] = body["intents"]
    assert listed["intent_id"] == str(h.intent.intent_id)
    assert (
        listed["max_quantity_decimal"] == "160" and listed["execution_mode"] == "live"
    )
    assert not {"mandate_id", "sleeve_id", "strategy_code_sha", "account_ref"} & set(
        listed
    )


@pytest.mark.parametrize(
    "params",
    [
        {"account": "kis_mock", "open": "true"},
        {"account": "upbit_live", "open": "false"},
        {"open": "true"},
    ],
)
def test_only_open_upbit_intents_can_be_listed(params: dict[str, str]) -> None:
    h = Harness()
    answer = h.client.get(INTENTS, params=params, headers=h.headers())
    assert answer.status_code == 422


def test_the_list_needs_the_token_and_the_route_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = Harness()
    params = {"account": "upbit_live", "open": "true"}
    assert h.client.get(INTENTS, params=params).status_code == 401
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", False)
    assert h.client.get(INTENTS, params=params, headers=h.headers()).status_code == 404


def test_a_failed_list_read_is_503_not_an_empty_list() -> None:
    h = Harness()
    h.list_error = RuntimeError("database unavailable")
    answer = h.client.get(
        INTENTS, params={"account": "upbit_live", "open": "true"}, headers=h.headers()
    )
    assert answer.status_code == 503 and answer.json() == {
        "detail": "query_unavailable"
    }


def test_the_list_route_has_no_write_method() -> None:
    methods = {
        method
        for item in create_app().routes
        if getattr(item, "path", None) == INTENTS
        for method in getattr(item, "methods", set())
    }
    assert methods == {"GET"}


# ------------------------------------------------- review round 1 (PR 2486)


def _kis_shaped(h: Harness, **overrides: Any) -> dict[str, Any]:
    body = h.upbit_body(**overrides)
    body.pop("quantity_decimal")
    body.setdefault("quantity", 1)
    return body


def test_a_kis_shaped_child_above_the_kis_price_ceiling_is_still_the_shape_refusal() -> (
    None
):
    """Design §3.3: common fields, then the intent, then the account's field set.

    150,000,000 KRW is a valid Upbit BTC price and above the kis_mock ceiling.
    The executor holds on any non-200, so this must be the definite refusal.
    """
    h = Harness()
    answer = h.post(_kis_shaped(h, limit_price_krw=150_000_000))

    assert answer.status_code == 200
    assert answer.json()["status"] == "refused"
    assert answer.json()["reason"] == "child_shape_invalid_for_account"
    assert answer.json()["child_id"] is None
    assert h.upbit_store.children == [] and h.live.calls == []


def test_the_same_invalid_body_for_a_kis_intent_or_no_intent_is_422_as_before() -> None:
    h = Harness()
    for intent_id in (str(h.kis_intent.intent_id), str(uuid4())):
        answer = h.post(
            _kis_shaped(h, intent_id=intent_id, limit_price_krw=150_000_000)
        )
        assert answer.status_code == 422
        assert answer.json() == {"detail": "invalid_child_request"}
    assert h.kis_store.children == [] and h.kis_broker.calls == []


def test_a_misshaped_child_reads_nothing_without_the_confirm_header() -> None:
    h = Harness()
    read: list[Any] = []
    account_of = h.upbit_store.account_of

    async def counting(intent_id: Any) -> str | None:
        read.append(intent_id)
        return await account_of(intent_id)

    h.upbit_store.account_of = counting  # type: ignore[method-assign]
    answer = h.client.post(
        CHILDREN,
        content=json.dumps(_kis_shaped(h, limit_price_krw=150_000_000)),
        headers=h.headers("another-key"),
    )
    assert answer.status_code == 422 and read == []
