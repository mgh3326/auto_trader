"""#1244 — route_request output per MCP profile, frozen against main.

``route_request_profile_golden.json`` holds, for every profile that registers
``route_request`` (gates on and off), the sha256 of the exact JSON response
(key order preserved) for every intent x market x purpose case. It was
generated from main (f24eb3a7f) before #1244 changed any route code, by
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
# The only cases #1244 lets move: crypto buy/sell on the h3-crypto-paper profile.
H3_MOVED_CASES = frozenset({"profit_taking|crypto|-", "buy_analysis|crypto|-"})


class _ListingRecorder(RegistrationRecorder):
    """Registration recorder that also answers route_request's registry read."""

    def list_tools(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.tools]


def case_key(intent: str, market: str, purpose: str | None) -> str:
    return f"{intent}|{market}|{purpose or '-'}"


def response_bytes(response: dict[str, Any]) -> bytes:
    return json.dumps(response, ensure_ascii=False).encode("utf-8")


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
    assert H3_PROFILE in actual, "h3-crypto-paper must register route_request"
    for profile, cases in expected.items():
        assert set(actual[profile]) == set(cases)
        for case, digest in cases.items():
            if profile == H3_PROFILE and case in H3_MOVED_CASES:
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
        "generated_from": "main f24eb3a7f (pre-#1244 route code)",
        "encoding": "sha256 of json.dumps(response, ensure_ascii=False), key order kept",
    }
    for gates_enabled in (True, False):
        key = "gates_enabled" if gates_enabled else "gates_disabled"
        golden[key] = _digests(
            collect_route_responses(monkeypatch, gates_enabled=gates_enabled)
        )
    GOLDEN_PATH.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8")
