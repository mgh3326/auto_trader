"""#1244 — route_request output per MCP profile, frozen against main.

``route_request_profile_golden.json`` holds, for every profile that registers
``route_request`` (gates on and off), the sha256 of the exact JSON response
(key order preserved) for every intent x market x purpose case. It was
generated from main before #1244 changed any route code (recaptured on main 49d56cc0b in round 3), by
running this module as a script. The test then pins every profile except
``h3-crypto-paper`` to those bytes; on ``h3-crypto-paper`` only the crypto
buy/sell routes may move (to the paper-execution contract) and every other
h3 case stays byte-identical too.

Regenerate only for an intentional, reviewed route change:
``ROUTE_REQUEST_GOLDEN_WRITE=1 uv run pytest tests/mcp_server/test_route_request_profile_golden.py -k regenerate``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import register_all_tools
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = pytest.mark.unit

GOLDEN_PATH = Path(__file__).with_name("route_request_profile_golden.json")
INTENTS = ("market_brief", "profit_taking", "buy_analysis", "discovery")
MARKETS = ("kr", "us", "crypto")
PURPOSES = (None, "account_cleanup")
H3_PROFILE = McpProfile.H3_CRYPTO_PAPER.value
H3_US_PROFILE = McpProfile.H3_US_PAPER.value
# The only cases #1244 lets move: buy/sell of each paper profile's own market.
H3_MOVED_CASES: dict[str, frozenset[str]] = {
    H3_PROFILE: frozenset({"profit_taking|crypto|-", "buy_analysis|crypto|-"}),
    H3_US_PROFILE: frozenset({"profit_taking|us|-", "buy_analysis|us|-"}),
}
# sha256 of the route_request tool description registered by main (f24eb3a7f
# route_request_registration.py, loaded from git and registered once; the
# file is unchanged on main through 49d56cc0b).
MAIN_DESCRIPTION_SHA256 = (
    "4058f1378548e422054db997f8a2ef34545c9529f6eedb2909bd1ccdca1e7739"
)


class _ListingRecorder(RegistrationRecorder):
    """Registration recorder that also answers route_request's registry read."""

    def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]


def case_key(intent: str, market: str, purpose: str | None) -> str:
    return f"{intent}|{market}|{purpose or '-'}"


def response_bytes(response: dict[str, Any]) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8")


def collect_route_descriptions(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Every profile's registered route_request description (gates on)."""
    with monkeypatch.context() as gate_patch:
        for name in type(settings).model_fields:
            if name.lower().endswith("enabled"):
                gate_patch.setattr(settings, name, True)
        descriptions: dict[str, str] = {}
        for profile in McpProfile:
            recorder = _ListingRecorder()
            register_all_tools(cast(Any, recorder), profile=profile)
            if "route_request" in recorder.options:
                descriptions[profile.value] = recorder.options["route_request"][
                    "description"
                ]
        return descriptions


def collect_route_responses(
    monkeypatch: pytest.MonkeyPatch, *, gates_enabled: bool
) -> dict[str, dict[str, dict[str, Any]]]:
    """Every profile's real route_request, called for every case."""
    with monkeypatch.context() as gate_patch:
        for name in type(settings).model_fields:
            if name.lower().endswith("enabled"):
                gate_patch.setattr(settings, name, gates_enabled)
        responses: dict[str, dict[str, dict[str, Any]]] = {}
        for profile in McpProfile:
            recorder = _ListingRecorder()
            register_all_tools(cast(Any, recorder), profile=profile)
            route = recorder.tools.get("route_request")
            if route is None:
                continue
            responses[profile.value] = {
                case_key(intent, market, purpose): asyncio.run(
                    route(intent=intent, market=market, purpose=purpose)
                )
                for intent in INTENTS
                for market in MARKETS
                for purpose in PURPOSES
            }
        return responses


def _digests(responses: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    return {
        profile: {
            case: hashlib.sha256(response_bytes(response)).hexdigest()
            for case, response in sorted(cases.items())
        }
        for profile, cases in sorted(responses.items())
    }


def _golden() -> dict[str, Any]:
    assert GOLDEN_PATH.is_file(), "route_request profile golden is missing"
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_route_request_is_byte_identical_to_main_off_the_h3_routes(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    key = "gates_enabled" if gates_enabled else "gates_disabled"
    expected = _golden()[key]
    actual = _digests(collect_route_responses(monkeypatch, gates_enabled=gates_enabled))
    assert set(actual) == set(expected), "profiles registering route_request changed"
    assert set(H3_MOVED_CASES) <= set(actual), "h3 profiles must register route_request"
    for profile, cases in expected.items():
        assert set(actual[profile]) == set(cases)
        for case, digest in cases.items():
            if case in H3_MOVED_CASES.get(profile, frozenset()):
                assert actual[profile][case] != digest, (
                    f"{profile} {case} must no longer be the proposal-led response"
                )
                continue
            assert actual[profile][case] == digest, (
                f"{profile} ({key}) {case}: route_request output drifted from main"
            )


@pytest.mark.skipif(
    os.environ.get("ROUTE_REQUEST_GOLDEN_WRITE") != "1",
    reason="writes the golden; opt-in only",
)
def test_regenerate_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    golden: dict[str, Any] = {
        "generated_from": "main (pre-#1244 route code)",
        "encoding": "sha256 of json.dumps(response, ensure_ascii=False), key order kept",
    }
    for gates_enabled in (True, False):
        key = "gates_enabled" if gates_enabled else "gates_disabled"
        golden[key] = _digests(
            collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
        )
    GOLDEN_PATH.write_text(
        json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_route_request_description_is_main_off_the_h3_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mcp_server.tooling.route_request_registration import (
        ALPACA_PAPER_SURFACE_DESCRIPTION,
        PAPER_SURFACE_DESCRIPTION,
    )

    suffixes = {
        H3_PROFILE: PAPER_SURFACE_DESCRIPTION,
        H3_US_PROFILE: ALPACA_PAPER_SURFACE_DESCRIPTION,
    }
    descriptions = collect_route_descriptions(monkeypatch)
    assert set(suffixes) <= set(descriptions)
    for profile, description in descriptions.items():
        if profile in suffixes:
            continue
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        assert digest == MAIN_DESCRIPTION_SHA256, f"{profile}: description drifted"
    for profile, suffix in suffixes.items():
        base = descriptions[profile].removesuffix(suffix)
        assert base != descriptions[profile], f"{profile} must carry its suffix"
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
        assert digest == MAIN_DESCRIPTION_SHA256


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
@pytest.mark.parametrize("intent", ["profit_taking", "buy_analysis"])
def test_h3_crypto_buy_sell_report_the_paper_contract_not_degraded(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool, intent: str
) -> None:
    from app.mcp_server.tooling.h3_crypto_paper_registration import (
        H3_CRYPTO_PAPER_TOOL_NAMES,
    )
    from app.mcp_server.tooling.route_request_lanes import (
        HARD_CONSTRAINTS,
        PAPER_EXECUTION_HARD_CONSTRAINTS,
        PAPER_EXECUTION_TOOLS,
        PROPOSAL_CHANNEL_HARD_CONSTRAINTS,
    )

    responses = collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
    out = responses[H3_PROFILE][case_key(intent, "crypto", None)]
    # The runner's bootstrap_health reads exactly these three top-level keys.
    assert out["success"] is True
    assert out["degraded"] is False
    assert "error" not in out
    assert out["intent"] == intent
    contract = out["route_contract"]
    assert contract == {
        "version": "paper-execution-v1",
        "state": "ready",
        "execution_mode": "paper_simulator",
        "execution_ready": True,
        "proposal_tool": None,
        "approval_channel": "runner_intent_guard",
        "human_approval_required": False,
        "preview_owner": "runner_decision",
        "reconcile_requirement": "paper_reconcile",
        "execution_tools": ["paper_cancel_pending_order", "paper_place_limit_order"],
        "required_tools": [
            "paper_cancel_pending_order",
            "paper_list_pending_orders",
            "paper_place_limit_order",
            "paper_reconcile_orders",
        ],
        "missing_required_tools": [],
        "foreign_execution_tools": [],
    }
    # Everything the route names is on the h3 allowlist; the only order tools
    # it allows are the two paper simulator tools.
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert named <= H3_CRYPTO_PAPER_TOOL_NAMES
    assert set(contract["required_tools"]) <= H3_CRYPTO_PAPER_TOOL_NAMES
    assert PAPER_EXECUTION_TOOLS <= set(out["allowed_tools"])
    assert out["blocked_actions"] == []
    # Exactly the proposal-channel lines are replaced by the paper lines; the
    # remaining lane constraints (loss guard scope included) are kept verbatim.
    lane = "sell" if intent == "profit_taking" else "buy"
    replaced = PROPOSAL_CHANNEL_HARD_CONSTRAINTS[lane]
    assert replaced <= set(HARD_CONSTRAINTS[lane]), "replaced lines drifted"
    assert out["hard_constraints"] == [
        c for c in HARD_CONSTRAINTS[lane] if c not in replaced
    ] + list(PAPER_EXECUTION_HARD_CONSTRAINTS)


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
@pytest.mark.parametrize("intent", ["profit_taking", "buy_analysis"])
def test_h3_us_buy_sell_report_the_alpaca_paper_contract_not_degraded(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool, intent: str
) -> None:
    from app.mcp_server.tooling.h3_us_paper_registration import (
        H3_US_PAPER_TOOL_NAMES,
    )

    responses = collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
    out = responses[H3_US_PROFILE][case_key(intent, "us", None)]
    assert out["success"] is True
    assert out["degraded"] is False
    assert "error" not in out
    assert out["intent"] == intent
    contract = out["route_contract"]
    assert contract == {
        "version": "paper-execution-v1",
        "state": "ready",
        "execution_mode": "alpaca_paper",
        "execution_ready": True,
        "proposal_tool": None,
        "approval_channel": "runner_intent_guard",
        "human_approval_required": False,
        "preview_owner": "runner_decision",
        "reconcile_requirement": "paper_reconcile",
        "execution_tools": ["alpaca_paper_cancel_order", "alpaca_paper_submit_order"],
        "required_tools": [
            "alpaca_paper_cancel_order",
            "alpaca_paper_get_order",
            "alpaca_paper_list_orders",
            "alpaca_paper_list_positions",
            "alpaca_paper_submit_order",
            "market_quote_snapshot_ensure",
        ],
        "missing_required_tools": [],
        "foreign_execution_tools": [],
    }
    named = set(out["allowed_tools"]) | {
        s["tool"] for s in out["standard_tool_sequence"]
    }
    assert named <= H3_US_PAPER_TOOL_NAMES
    assert {"alpaca_paper_submit_order", "alpaca_paper_cancel_order"} <= set(
        out["allowed_tools"]
    )
    assert out["blocked_actions"] == []


# Literal text oracles (#1244 r3, CodeRabbit on route_request_lanes.py and
# tester r2 NICE): written here, not read from the production constants.
BROKER_RECONCILE_LINE = (
    "accepted/resting is not a fill; broker evidence reconcile is required"
)
RUNNER_APPROVAL_LINE = (
    "each paper order call must equal a runner-approved intent; the runner's "
    "per-call guard owns approval and the session never improvises an order"
)
PAPER_FILL_LINES = {
    H3_PROFILE: "accepted/resting is not a fill; confirm a fill only from the "
    "paper account's own order read: paper_reconcile_orders, then an "
    "all-status paper_list_pending_orders read",
    H3_US_PROFILE: "accepted/resting is not a fill; confirm a fill only from "
    "the Alpaca paper account's own order read (alpaca_paper_get_order / "
    "alpaca_paper_list_orders) and alpaca_paper_list_positions",
}
PAPER_MARKETS = {H3_PROFILE: "crypto", H3_US_PROFILE: "us"}


@pytest.mark.parametrize("profile", sorted(PAPER_MARKETS))
@pytest.mark.parametrize("intent", ["profit_taking", "buy_analysis"])
def test_paper_routes_carry_the_paper_fill_line_not_the_broker_one(
    monkeypatch: pytest.MonkeyPatch, profile: str, intent: str
) -> None:
    responses = collect_route_responses(monkeypatch, gates_enabled=True)
    out = responses[profile][case_key(intent, PAPER_MARKETS[profile], None)]
    constraints = out["hard_constraints"]
    assert BROKER_RECONCILE_LINE not in constraints
    assert constraints.count(PAPER_FILL_LINES[profile]) == 1
    assert constraints.count(RUNNER_APPROVAL_LINE) == 1
    assert not any("order_proposal_create only" in c for c in constraints)
    # The proposal-led routes keep the broker line (golden keeps the bytes).
    default = responses[McpProfile.DEFAULT.value][
        case_key(intent, PAPER_MARKETS[profile], None)
    ]
    assert BROKER_RECONCILE_LINE in default["hard_constraints"]
