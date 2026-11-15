"""hk 4732 (3797 T-P5) — `kiwoom_mock_place_order` MCP responses are a golden.

The place sequence moved into ``app/services/kiwoom_mock_place.py`` so the
order_proposals submit adapter can call it too. The MCP tool must stay a
byte-identical facade. Every scenario below was captured from the pre-change
implementation (0bca498a1, the rebase base) and from this head; the two dumps
were identical.

No broker, no database: a stub order client, a stub preflight and the
in-memory recorders stand in for all three.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.test_mcp_kiwoom_order_variants import (
    DummyMCP,
    FakeKiwoomPlaceRecorder,
    FakeKiwoomSignalRecorder,
    _patch_preflight_success,
)

pytestmark = pytest.mark.unit

FROZEN_NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # 2026-11-16 10:00 KST
ACK = {"return_code": 0, "return_msg": "정상", "ord_no": "0000111222"}
REJECT = {"return_code": 20, "return_msg": "주문가능금액 부족"}
NO_ORDER = {"return_code": 0, "return_msg": "정상"}

# Captured from 0bca498a1 (pre-extraction; minus this ticket) — see the module docstring.
GOLDEN: dict[str, dict[str, Any]] = {
    "preview": {
        "events": [],
        "response": {
            "account_mode": "kiwoom_mock",
            "dry_run": True,
            "estimated_evidence": {"type": "estimated"},
            "exchange": "KRX",
            "preflight_checks": [],
            "preflight_warnings": [],
            "preview": True,
            "price": 70000,
            "quantity": 3,
            "side": "buy",
            "source": "kiwoom",
            "success": True,
            "symbol": "005930",
        },
    },
    "accepted": {
        "events": ["reserve", "post", "record:accepted"],
        "response": {
            "account_mode": "kiwoom_mock",
            "broker_response": {
                "ord_no": "0000111222",
                "return_code": 0,
                "return_msg": "정상",
            },
            "client_order_id": "kwoomm-feed3f60634f74d6",
            "correlation_id": "live:kiwoom_mock:62e168555d44e248",
            "dry_run": False,
            "estimated_evidence": {"type": "estimated"},
            "exchange": "KRX",
            "ledger_id": 1,
            "ledger_recorded": True,
            "ledger_status": "accepted",
            "ord_no": "0000111222",
            "preflight_checks": [],
            "preflight_warnings": [],
            "price": 70000,
            "quantity": 3,
            "return_code": 0,
            "return_msg": "정상",
            "side": "buy",
            "source": "kiwoom",
            "success": True,
            "symbol": "005930",
        },
    },
    "sell_accepted_rung2": {
        "events": ["reserve", "post", "record:accepted"],
        "response": {
            "account_mode": "kiwoom_mock",
            "broker_response": {
                "ord_no": "0000111222",
                "return_code": 0,
                "return_msg": "정상",
            },
            "client_order_id": "kwoomm-54aaa6b611ae46f1",
            "correlation_id": "live:kiwoom_mock:83d3126c0af0acf5",
            "dry_run": False,
            "estimated_evidence": {"type": "estimated"},
            "exchange": "KRX",
            "ledger_id": 1,
            "ledger_recorded": True,
            "ledger_status": "accepted",
            "ord_no": "0000111222",
            "preflight_checks": [],
            "preflight_warnings": [],
            "price": 70000,
            "quantity": 5,
            "return_code": 0,
            "return_msg": "정상",
            "side": "sell",
            "source": "kiwoom",
            "success": True,
            "symbol": "005930",
        },
    },
    "rejected": {
        "events": ["reserve", "post", "record:rejected"],
        "response": {
            "account_mode": "kiwoom_mock",
            "broker_response": {
                "return_code": 20,
                "return_msg": "주문가능금액 부족",
            },
            "client_order_id": "kwoomm-6598b9a690116019",
            "correlation_id": "live:kiwoom_mock:189b68d012431ff6",
            "dry_run": False,
            "estimated_evidence": {"type": "estimated"},
            "exchange": "KRX",
            "ledger_id": 1,
            "ledger_recorded": True,
            "ledger_status": "rejected",
            "preflight_checks": [],
            "preflight_warnings": [],
            "price": 70000,
            "quantity": 1,
            "return_code": 20,
            "return_msg": "주문가능금액 부족",
            "side": "buy",
            "source": "kiwoom",
            "success": False,
            "symbol": "005930",
        },
    },
    "uncertain": {
        "events": ["reserve", "post", "record:uncertain"],
        "response": {
            "account_mode": "kiwoom_mock",
            "broker_response": {"return_code": 0, "return_msg": "정상"},
            "client_order_id": "kwoomm-6598b9a690116019",
            "correlation_id": "live:kiwoom_mock:189b68d012431ff6",
            "dry_run": False,
            "estimated_evidence": {"type": "estimated"},
            "exchange": "KRX",
            "ledger_id": 1,
            "ledger_recorded": True,
            "ledger_status": "uncertain",
            "preflight_checks": [],
            "preflight_warnings": [],
            "price": 70000,
            "quantity": 1,
            "reconcile_required": True,
            "retry_allowed": False,
            "return_code": 0,
            "return_msg": "정상",
            "side": "buy",
            "source": "kiwoom",
            "status": "acceptance_uncertain",
            "success": False,
            "symbol": "005930",
        },
    },
    "attribution_required": {
        "events": [],
        "response": {
            "account_mode": "kiwoom_mock",
            "dispatch_started": False,
            "dry_run": False,
            "error": (
                "kiwoom_mock order is unattributed; refusing to send "
                "(missing=['strategy'])"
            ),
            "error_code": "attribution_required",
            "exchange": "KRX",
            "missing_attribution": ["strategy"],
            "price": 70000,
            "quantity": 1,
            "reconcile_required": False,
            "side": "buy",
            "source": "kiwoom",
            "status": "not_submitted",
            "success": False,
            "symbol": "005930",
        },
    },
    "no_confirm": {
        "events": [],
        "response": {
            "account_mode": "kiwoom_mock",
            "error": "kiwoom_mock_place_order requires confirm=True when dry_run=False.",
            "source": "kiwoom",
            "success": False,
        },
    },
    "zero_quantity": {
        "events": [],
        "response": {
            "account_mode": "kiwoom_mock",
            "error": "kiwoom_mock requires quantity > 0; got 0",
            "source": "kiwoom",
            "success": False,
        },
    },
}

CASES: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
    "preview": ([], {"symbol": "005930", "side": "buy", "quantity": 3, "price": 70000}),
    "accepted": (
        [ACK],
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 3,
            "price": 70000,
            "strategy": "unit-test",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "sell_accepted_rung2": (
        [ACK],
        {
            "symbol": "005930",
            "side": "sell",
            "quantity": 5,
            "price": 70000,
            "strategy": "unit-test",
            "dry_run": False,
            "confirm": True,
            "rung": 2,
        },
    ),
    "rejected": (
        [REJECT],
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "strategy": "unit-test",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "uncertain": (
        [NO_ORDER],
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "strategy": "unit-test",
            "dry_run": False,
            "confirm": True,
        },
    ),
    "attribution_required": (
        [],
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "dry_run": False,
            "confirm": True,
        },
    ),
    "no_confirm": (
        [],
        {
            "symbol": "005930",
            "side": "buy",
            "quantity": 1,
            "price": 70000,
            "dry_run": False,
        },
    ),
    "zero_quantity": (
        [],
        {"symbol": "005930", "side": "buy", "quantity": 0, "price": 70000},
    ),
}


class _StubOrderClient:
    """Counts broker POSTs and appends the ``post`` event to the recorder."""

    def __init__(
        self, recorder: FakeKiwoomPlaceRecorder, behaviour: list[dict[str, Any]]
    ) -> None:
        self.recorder = recorder
        self.behaviour = behaviour
        self.posts: list[dict[str, Any]] = []

    def factory(self):
        stub = self

        class _OrderClient:
            def __init__(self, _client):
                pass

            async def _post(self, method: str, **kwargs):
                stub.posts.append({"method": method, **kwargs})
                stub.recorder.events.append("post")
                step = stub.behaviour[min(len(stub.posts), len(stub.behaviour)) - 1]
                return step

            async def place_buy_order(self, **kwargs):
                return await self._post("buy", **kwargs)

            async def place_sell_order(self, **kwargs):
                return await self._post("sell", **kwargs)

        return _OrderClient


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(GOLDEN))
async def test_place_response_matches_the_frozen_golden(case, monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    behaviour, kwargs = CASES[case]
    recorder = FakeKiwoomPlaceRecorder()
    signal_recorder = FakeKiwoomSignalRecorder()
    stub = _StubOrderClient(recorder, behaviour)

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "_new_place_recorder", lambda: recorder)
    monkeypatch.setattr(mod, "_new_signal_recorder", lambda: signal_recorder)
    monkeypatch.setattr(mod, "_new_kiwoom_mock_client", lambda: object())
    monkeypatch.setattr(mod, "KiwoomDomesticOrderClient", stub.factory())
    monkeypatch.setattr(mod, "_utc_now", lambda: FROZEN_NOW)
    _patch_preflight_success(monkeypatch, mod)
    mcp = DummyMCP()
    mod.register(mcp)

    response = await mcp.tools["kiwoom_mock_place_order"](**kwargs)

    normalised = json.loads(json.dumps(response, sort_keys=True, default=str))
    assert normalised == GOLDEN[case]["response"]
    assert recorder.events == GOLDEN[case]["events"]
