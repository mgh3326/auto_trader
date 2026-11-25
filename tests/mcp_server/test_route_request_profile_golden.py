"""#1244 — route_request output per MCP profile, frozen against main.

``route_request_profile_golden.json`` holds, for every profile that registers
``route_request`` (gates on and off), the sha256 of the exact JSON response
(key order preserved) for every intent x market x purpose case. It was
generated from main before #1244 changed any route code (recaptured on main 49d56cc0b in round 3), by
running this module as a script. The test then pins every profile to those
bytes. hk 6456 retired the two H3 paper profiles and their paper-execution
route contract, so their golden cases are gone and no profile's route may move.

hk 1040 (operator 10-16 dr-1040-1 A) moves exactly one tool on kr/us routes:
``toss_get_orderable_cash`` leaves ``blocked_actions`` for ``allowed_tools``
(route_request_lanes.MARKET_EXTRA_ALLOWED).  The golden is not regenerated for
it: the test undoes that single move (``_undo_1040``) and still requires the
main bytes, and pins which cases moved.

hk 3717 adds ``kis_live_get_daily_orders`` to ``HARNESS_DENIED_TOOLS``; it shows
up only as one extra ``blocked_actions`` entry plus one ``harness_denied_tools`` key on DEFAULT, which the test undoes
(``_undo_3717``) rather than regenerating the golden.

Regenerate only for an intentional, reviewed route change:
``ROUTE_REQUEST_GOLDEN_WRITE=1 uv run pytest tests/mcp_server/test_route_request_profile_golden.py -k regenerate``.
"""

from __future__ import annotations

import asyncio
import copy
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


_1040_TOOL = "toss_get_orderable_cash"
_1040_MARKETS = ("kr", "us")


def _undo_1040(
    responses: dict[str, dict[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, set[str]]]:
    """Move toss_get_orderable_cash back from allowed_tools to blocked_actions
    on kr/us cases; return the undone responses and the moved cases."""
    undone: dict[str, dict[str, dict[str, Any]]] = {}
    moved: dict[str, set[str]] = {}
    for profile, cases in responses.items():
        undone[profile] = {}
        moved[profile] = set()
        for case, response in cases.items():
            market = case.split("|")[1]
            if market in _1040_MARKETS and _1040_TOOL in response.get(
                "allowed_tools", []
            ):
                response = copy.deepcopy(response)
                response["allowed_tools"].remove(_1040_TOOL)
                response["blocked_actions"] = sorted(
                    [*response["blocked_actions"], _1040_TOOL]
                )
                moved[profile].add(case)
            undone[profile][case] = response
    return undone, moved


_3717_TOOL = "kis_live_get_daily_orders"


def _undo_3717(
    responses: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """hk 3717: the new harness-denied tool is bucketed in ``blocked_actions`` on
    every profile that registers it (DEFAULT) and listed in ``harness_denied_tools``,
    exactly like the existing denied tools; drop those two entries so the main bytes are still required."""
    undone: dict[str, dict[str, dict[str, Any]]] = {}
    for profile, cases in responses.items():
        undone[profile] = {}
        for case, response in cases.items():
            denied = response.get("harness_denied_tools", {})
            if _3717_TOOL in response.get("blocked_actions", []) or (
                _3717_TOOL in denied
            ):
                response = copy.deepcopy(response)
                if _3717_TOOL in response["blocked_actions"]:
                    response["blocked_actions"].remove(_3717_TOOL)
                response.get("harness_denied_tools", {}).pop(_3717_TOOL, None)
            undone[profile][case] = response
    return undone


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
def test_route_request_is_byte_identical_to_main(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    key = "gates_enabled" if gates_enabled else "gates_disabled"
    expected = _golden()[key]
    responses = collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
    undone, moved = _undo_1040(responses)
    undone = _undo_3717(undone)
    # hk 1040: no kr/us route on any profile still blocks the tool, and at
    # least one moved; with the move undone every byte (crypto included) must
    # still be main's.
    for profile, cases in responses.items():
        for case, response in cases.items():
            if case.split("|")[1] in _1040_MARKETS:
                assert _1040_TOOL not in response.get("blocked_actions", []), (
                    profile,
                    case,
                )
    assert any(moved.values()), "hk 1040 moved no route; the supplement is dead"
    actual = _digests(undone)
    assert set(actual) == set(expected), "profiles registering route_request changed"
    for profile, cases in expected.items():
        assert set(actual[profile]) == set(cases)
        for case, digest in cases.items():
            assert actual[profile][case] == digest, (
                f"{profile} ({key}) {case}: route_request output drifted from main"
            )


@pytest.mark.skipif(
    os.environ.get("ROUTE_REQUEST_GOLDEN_WRITE") != "1",
    reason="writes the golden; opt-in only",
)
def test_regenerate_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regenerate exactly what the byte-identity test compares: the hk 1040 and
    # hk 3717 moves undone.
    golden: dict[str, Any] = {
        "generated_from": "main (pre-#1244 route code)",
        "encoding": "sha256 of json.dumps(response, ensure_ascii=False), key order kept",
    }
    for gates_enabled in (True, False):
        key = "gates_enabled" if gates_enabled else "gates_disabled"
        responses = collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
        undone, _moved = _undo_1040(responses)
        golden[key] = _digests(_undo_3717(undone))
    GOLDEN_PATH.write_text(
        json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_route_request_description_is_main_on_every_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptions = collect_route_descriptions(monkeypatch)
    assert McpProfile.DEFAULT.value in descriptions
    for profile, description in descriptions.items():
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        assert digest == MAIN_DESCRIPTION_SHA256, f"{profile}: description drifted"
