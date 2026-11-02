"""ROB-1461 Acceptance 1 and 2: the route's gates, one test per invariant row."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.routers import execution_intent_gateway as route
from app.services.execution_intent_gateway.decisions import (
    MOCK_ACCOUNTS,
    Refusal,
)
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.kis_mock_runner.control import KillMode
from scripts.run_execution_intent_gateway import is_loopback, main
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    NOW,
    FakeBroker,
    FakeKillSwitch,
    MemoryGatewayStore,
    intent_snapshot,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "route-test-token"
TOKEN_HEADER = "X-Execution-Intent-Gateway-Token"
CONFIRM_HEADER = "X-Execution-Intent-Confirm"
CHILDREN = "/execution-intents/children"


class Harness:
    def __init__(self, *, account: str = "kis_mock") -> None:
        self.intent = intent_snapshot(account=account)
        self.store = MemoryGatewayStore(self.intent)
        self.broker = FakeBroker()
        self.kill = FakeKillSwitch(KillMode.ACTIVE)
        self.gateway = IntentGateway(
            store=self.store,
            forwarders={"kis_mock": self.broker},
            kill_switch=self.kill,
            environment=ENABLED,
            clock=lambda: NOW,
        )
        self.app = create_app()
        self.app.dependency_overrides[route.get_intent_gateway] = lambda: self.gateway
        self.client = TestClient(self.app)

    def body(self, key: str = "child-0001", **overrides: Any) -> dict[str, Any]:
        return {
            "action": "submit",
            "intent_id": str(self.intent.intent_id),
            "idempotency_key": key,
            "quantity": 1,
            "limit_price_krw": 70_000,
            **overrides,
        }

    def post(
        self,
        body: Any,
        *,
        token: str | None = TOKEN,
        confirm: str | None = "<key>",
        content_type: str = "application/json",
    ) -> Any:
        headers = {"Content-Type": content_type}
        if token is not None:
            headers[TOKEN_HEADER] = token
        if confirm == "<key>":
            headers[CONFIRM_HEADER] = body["idempotency_key"]
        elif confirm is not None:
            headers[CONFIRM_HEADER] = confirm
        return self.client.post(CHILDREN, content=json.dumps(body), headers=headers)


@pytest.fixture(autouse=True)
def _route_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN_HEADER", TOKEN_HEADER)


# Invariant 1: the live account is unreachable through the route.


def test_non_mock_intent_is_refused_before_any_broker_call() -> None:
    h = Harness(account="kis_live")
    answer = h.post(h.body())
    assert answer.status_code == 200
    assert answer.json()["status"] == "refused"
    assert answer.json()["reason"] == Refusal.ACCOUNT_NOT_MOCK.value
    assert h.broker.calls == []


@pytest.mark.parametrize(
    "extra",
    [{"account": "kis_live"}, {"is_mock": False}, {"market": "kr"}, {"broker": "kis"}],
)
def test_a_body_cannot_name_an_account(extra: dict[str, Any]) -> None:
    h = Harness()
    answer = h.post(h.body(**extra))
    assert answer.status_code == 422
    assert h.store.children == []
    assert h.broker.calls == []


def test_the_served_gateway_holds_only_mock_forwarders() -> None:
    gateway = route.get_intent_gateway()
    assert set(gateway._forwarders) == {"kis_mock"} == set(MOCK_ACCOUNTS)


def test_the_gateway_cannot_be_built_with_a_live_forwarder() -> None:
    with pytest.raises(ValueError):
        IntentGateway(
            store=MemoryGatewayStore(),
            forwarders={"kis_live": FakeBroker()},
            kill_switch=FakeKillSwitch(),
            environment=ENABLED,
        )


def test_no_route_creates_an_intent() -> None:
    methods = {
        (method, path.path)
        for path in create_app().routes
        for method in getattr(path, "methods", set())
    }
    assert methods == {
        ("POST", "/execution-intents/children"),
        ("GET", "/execution-intents/children/{idempotency_key}"),
        ("GET", "/execution-intents/intents/{intent_id}"),
    }


# Invariant 2: the route is off unless its own setting is on, and only on loopback.


@pytest.mark.parametrize("setting", [False, None, "true", 1])
def test_route_off_answers_404_with_nothing_touched(
    setting: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", setting)
    h = Harness()

    def must_not_build() -> None:
        raise AssertionError("route off must not build a gateway")

    h.app.dependency_overrides[route.get_intent_gateway] = must_not_build
    h.app.dependency_overrides[route.get_gateway_reader] = must_not_build
    h.app.dependency_overrides[route.get_evidence_reader] = must_not_build

    assert h.post(h.body()).status_code == 404
    headers = {TOKEN_HEADER: TOKEN}
    assert (
        h.client.get(
            "/execution-intents/children/child-0001", headers=headers
        ).status_code
        == 404
    )
    assert (
        h.client.get(
            f"/execution-intents/intents/{h.intent.intent_id}", headers=headers
        ).status_code
        == 404
    )
    assert h.broker.calls == []
    assert h.kill.reads == 0


def test_route_setting_defaults_to_off_and_token_to_empty() -> None:
    fields = type(settings).model_fields
    assert fields["EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED"].default is False
    assert fields["EXECUTION_INTENT_GATEWAY_TOKEN"].default == ""


def test_e1_env_gate_still_applies_with_the_route_on() -> None:
    h = Harness()
    h.gateway = IntentGateway(
        store=h.store,
        forwarders={"kis_mock": h.broker},
        kill_switch=h.kill,
        environment={},
        clock=lambda: NOW,
    )
    answer = h.post(h.body())
    assert answer.json()["reason"] == Refusal.GATEWAY_DISABLED.value
    assert h.broker.calls == []


def test_public_app_does_not_mount_the_route() -> None:
    tree = ast.parse((ROOT / "app/main.py").read_text("utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    assert "execution_intent_gateway" not in names
    assert "execution_intent_gateway" not in (ROOT / "app/main.py").read_text("utf-8")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.0.0.2"])
def test_loopback_hosts_are_allowed(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "10.0.0.5", "100.122.100.56", "example.com", ""]
)
def test_the_server_refuses_a_non_loopback_host(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a non-loopback bind must not start")

    monkeypatch.setattr(uvicorn, "run", must_not_run)
    assert main(["--host", host]) == 2


def test_the_server_serves_the_factory_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run as a script, sys.path holds scripts/, so the app needs ``app_dir``."""
    import uvicorn

    captured: dict[str, Any] = {}

    def run(target: str, **kwargs: Any) -> None:
        captured.update(kwargs, target=target)

    monkeypatch.setattr(uvicorn, "run", run)
    assert main([]) == 0
    assert captured["target"] == "app.execution_intent_gateway_server:create_app"
    assert captured["factory"] is True
    assert captured["app_dir"] == str(ROOT)
    assert captured["host"] == "127.0.0.1"


# Invariant 3: token and the per-action confirm header are required.


@pytest.mark.parametrize(
    ("token", "status"), [(None, 401), ("wrong", 401), ("", 401), (TOKEN + "x", 401)]
)
def test_missing_or_wrong_token_is_refused(token: str | None, status: int) -> None:
    h = Harness()
    assert h.post(h.body(), token=token).status_code == status
    assert h.store.children == []
    assert h.broker.calls == []


@pytest.mark.parametrize("configured", ["", None])
def test_unset_token_is_refused_for_everyone(
    configured: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", configured)
    h = Harness()
    assert h.post(h.body(), token="").status_code == 403
    assert h.broker.calls == []


def test_query_needs_the_token_too() -> None:
    h = Harness()
    assert h.client.get("/execution-intents/children/child-0001").status_code == 401
    assert (
        h.client.get(f"/execution-intents/intents/{h.intent.intent_id}").status_code
        == 401
    )


@pytest.mark.parametrize("confirm", [None, "", "other-key-01", "child-0001 "])
def test_missing_or_different_confirm_is_refused_by_e1(confirm: str | None) -> None:
    h = Harness()
    answer = h.post(h.body(), confirm=confirm)
    assert answer.status_code == 200
    assert answer.json()["status"] == "refused"
    assert answer.json()["reason"] == Refusal.CONFIRM_REQUIRED.value
    assert h.store.children == []
    assert h.broker.calls == []


def test_matching_confirm_sends_once() -> None:
    h = Harness()
    answer = h.post(h.body())
    assert answer.status_code == 200
    assert answer.json()["status"] == "accepted"
    assert answer.json()["broker_order_id"] == "1001"
    assert h.broker.calls == [("submit", "child-0001")]


# Invariant 4: an idempotent replay returns the stored outcome.


def test_replay_returns_the_stored_outcome_without_a_second_send() -> None:
    h = Harness()
    first = h.post(h.body()).json()
    second = h.post(h.body()).json()
    assert second == {**first, "replayed": True}
    assert h.broker.calls == [("submit", "child-0001")]


def test_replay_of_a_refusal_returns_the_refusal() -> None:
    h = Harness()
    h.kill.mode = KillMode.GLOBAL_FREEZE
    assert h.post(h.body()).json()["reason"] == Refusal.SUBMISSIONS_KILLED.value
    h.kill.mode = KillMode.ACTIVE
    replay = h.post(h.body()).json()
    assert replay["reason"] == Refusal.SUBMISSIONS_KILLED.value
    assert replay["replayed"] is True
    assert h.broker.calls == []


def test_same_key_with_a_different_body_is_a_conflict() -> None:
    h = Harness()
    h.post(h.body())
    answer = h.post(h.body(quantity=2)).json()
    assert answer["status"] == "conflict"
    assert h.broker.calls == [("submit", "child-0001")]


# Invariant 6: the kill switch is honoured.


def test_kill_refuses_a_submit_and_still_allows_a_cancel() -> None:
    h = Harness()
    h.post(h.body())
    h.kill.mode = KillMode.GLOBAL_FREEZE
    assert (
        h.post(h.body("child-0002")).json()["reason"]
        == Refusal.SUBMISSIONS_KILLED.value
    )
    cancel = {
        "action": "cancel",
        "intent_id": str(h.intent.intent_id),
        "idempotency_key": "child-0003",
        "target_idempotency_key": "child-0001",
    }
    assert h.post(cancel).json()["status"] == "accepted"
    assert h.broker.calls == [("submit", "child-0001"), ("cancel", "1001")]


def test_a_failed_kill_read_counts_as_frozen() -> None:
    h = Harness()
    h.kill.read_error = RuntimeError("db down")
    assert h.post(h.body()).json()["reason"] == Refusal.SUBMISSIONS_KILLED.value
    assert h.broker.calls == []


# Invariant 7: caps are unchanged, and the route adds no path around them.


def test_the_route_cannot_exceed_the_cumulative_caps() -> None:
    h = Harness()
    assert h.post(h.body(quantity=10)).json()["status"] == "accepted"
    answer = h.post(h.body("child-0002", quantity=1)).json()
    assert answer["reason"] == Refusal.QUANTITY_CAP_EXCEEDED.value
    assert h.broker.calls == [("submit", "child-0001")]


def test_the_route_calls_only_handle_for_writes() -> None:
    source = (ROOT / "app/routers/execution_intent_gateway.py").read_text("utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called & {"reserve", "finish", "store_intent", "_caps_refusal", "decide"}


# Transport edges: all fail closed, and fillwire reads any non-200 as unknown.


@pytest.mark.parametrize(
    "body",
    [
        {"action": "submit"},
        {"action": "liquidate"},
        [],
        "text",
        None,
    ],
)
def test_invalid_body_is_422_and_does_not_echo(body: Any) -> None:
    h = Harness()
    answer = h.client.post(
        CHILDREN,
        content=json.dumps(body),
        headers={"Content-Type": "application/json", TOKEN_HEADER: TOKEN},
    )
    assert answer.status_code == 422
    assert answer.json() == {"detail": "invalid_child_request"}
    assert h.store.children == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"quantity": 1.0},
        {"quantity": "1"},
        {"quantity": True},
        {"limit_price_krw": 1.5},
    ],
)
def test_inexact_numbers_are_refused(overrides: dict[str, Any]) -> None:
    h = Harness()
    assert h.post(h.body(**overrides)).status_code == 422
    assert h.broker.calls == []


def test_malformed_json_is_422() -> None:
    h = Harness()
    answer = h.client.post(
        CHILDREN,
        content=b"{not json",
        headers={"Content-Type": "application/json", TOKEN_HEADER: TOKEN},
    )
    assert answer.status_code == 422


def test_non_json_content_type_is_415() -> None:
    h = Harness()
    assert h.post(h.body(), content_type="text/plain").status_code == 415
    assert h.broker.calls == []


def test_oversized_body_is_413() -> None:
    h = Harness()
    answer = h.client.post(
        CHILDREN,
        content=b"x" * 5_000,
        headers={"Content-Type": "application/json", TOKEN_HEADER: TOKEN},
    )
    assert answer.status_code == 413


def test_a_raised_handle_is_503_with_a_fixed_body() -> None:
    h = Harness()

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("secret detail")

    h.store.reserve = boom  # type: ignore[method-assign]
    answer = h.post(h.body())
    assert answer.status_code == 503
    assert answer.json() == {"detail": "gateway_unavailable"}
    assert h.broker.calls == []
