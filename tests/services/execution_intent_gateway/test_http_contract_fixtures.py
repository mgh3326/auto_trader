"""ROB-1461 Acceptance 4: the route answers fillwire's recorded requests exactly.

The fixtures are unedited copies from fillwire (see
``tests/fixtures/fillwire_e1/SOURCE.md``). fillwire recorded them by calling
``IntentGateway`` directly and serves them to its executor. This test sends the
same requests over HTTP and compares the answers, so the two sides of the binding
cannot drift apart unnoticed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.execution_intent_gateway_server import create_app
from app.routers import execution_intent_gateway as route
from app.schemas.execution_intent import ExecutionIntentV1
from app.services.execution_intent_gateway.decisions import IntentSnapshot
from app.services.execution_intent_gateway.gateway import IntentGateway
from app.services.kis_mock_runner.control import KillMode
from tests.services.execution_intent_gateway.support import (
    ENABLED,
    FakeBroker,
    FakeKillSwitch,
    MemoryGatewayStore,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/fillwire_e1"
RECORDED = json.loads((FIXTURES / "e1_recorded.json").read_text("utf-8"))
SCENARIOS = RECORDED["scenarios"]
TOKEN = "fixture-token"
#: fillwire's ``Outcome`` decodes with DisallowUnknownFields, so this set is exact.
OUTCOME_FIELDS = {
    "status",
    "reason",
    "child_id",
    "broker_order_id",
    "result",
    "replayed",
    "outcome_not_recorded",
}


def _snapshot(raw: dict[str, Any], account: str | None) -> IntentSnapshot:
    intent = ExecutionIntentV1.model_validate(raw)
    return IntentSnapshot(
        intent_id=intent.intent_id,
        strategy=intent.strategy,
        account=account or intent.account,
        venue=intent.venue,
        symbol=intent.symbol,
        side=intent.side,
        max_quantity=intent.max_quantity,
        max_notional_krw=intent.max_notional_krw,
        limit_price_krw=intent.limit_price_krw,
        max_slippage_bps=intent.max_slippage_bps,
        position_at_authorization=intent.inventory.position_at_authorization,
        min_position_after=intent.inventory.min_position_after,
        max_position_after=intent.inventory.max_position_after,
        expires_at=intent.expires_at,
    )


def _post(client: TestClient, request: dict[str, Any], *, confirm: bool) -> Any:
    headers = {
        "Content-Type": "application/json",
        "X-Execution-Intent-Gateway-Token": TOKEN,
    }
    if confirm:
        headers["X-Execution-Intent-Confirm"] = request["idempotency_key"]
    return client.post(
        "/execution-intents/children",
        content=json.dumps(request),
        headers=headers,
    )


def test_fixtures_are_the_recorded_copies() -> None:
    source = (FIXTURES / "SOURCE.md").read_text("utf-8")
    assert "9bf7604c0110a2591b36d6d97cd2b3eae8e7f55e" in source
    for name in ("e1_scenarios.json", "e1_recorded.json"):
        digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert digest in source, name


def test_every_refusal_kind_fillwire_expects_is_in_the_fixtures() -> None:
    reasons = {
        step["response"]["reason"]
        for scenario in SCENARIOS
        for step in scenario["steps"]
    }
    assert {
        "notional_cap_exceeded",
        "quantity_cap_exceeded",
        "authorization_expired",
        "submissions_killed",
        "confirm_required",
        "gateway_disabled",
        "account_not_mock",
    } <= reasons


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["name"])
def test_route_answers_the_recorded_scenario(
    scenario: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_INTENT_GATEWAY_TOKEN", TOKEN)

    e1 = scenario["e1"]
    intent = _snapshot(scenario["intent"], e1.get("store_account"))
    store = (
        MemoryGatewayStore(intent)
        if e1.get("store_intent", True)
        else MemoryGatewayStore()
    )
    broker = FakeBroker()
    kill = FakeKillSwitch(KillMode.ACTIVE)
    now = datetime.fromisoformat(
        e1.get("now", "2026-11-02T00:00:30Z").replace("Z", "+00:00")
    )
    gateway = IntentGateway(
        store=store,
        forwarders=dict.fromkeys(e1.get("forwarders", ["kis_mock"]), broker),
        kill_switch=kill,
        environment=ENABLED if e1.get("enabled", True) else {},
        clock=lambda: now,
    )
    app = create_app()
    app.dependency_overrides[route.get_intent_gateway] = lambda: gateway

    with TestClient(app) as client:
        for prior in e1.get("prior", []):
            answer = _post(client, prior, confirm=True)
            assert answer.status_code == 200
            assert answer.json()["status"] == "accepted"
        for step in scenario["steps"]:
            kill.mode = KillMode(step.get("kill_mode", "ACTIVE"))
            broker.accepted = step.get("broker") != "fail"
            broker.raises = (
                RuntimeError("broker") if step.get("broker") == "raise" else None
            )
            store.finish_error = (
                RuntimeError("db") if step.get("finish_error") else None
            )

            answer = _post(client, step["request"], confirm=step.get("confirm", True))

            assert answer.status_code == 200, step["request"]
            assert answer.headers["content-type"] == "application/json"
            assert set(answer.json()) == OUTCOME_FIELDS
            assert answer.json() == step["response"]

    assert [list(call) for call in broker.calls] == scenario["broker_calls"]
