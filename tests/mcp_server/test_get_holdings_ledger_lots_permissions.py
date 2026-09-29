"""Task #963 (operator ruling: option B) — no permission surface change.

The ledger lots block rides on ``get_holdings``, which is already a live-kr core
tool, so live.yaml, the kr lane allowlist and the robin harness argv list need
no edit. The #678 harness denial of ``kis_live_get_order_history`` is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.live_profile_registration import (
    LIVE_GROUP_CORE,
    LIVE_GROUP_EMERGENCY,
    LIVE_GROUP_EXTENSION,
    LIVE_MANIFEST_PATH,
    LIVE_PROFILES,
    load_live_manifest,
)
from app.mcp_server.tooling.route_request_lanes import (
    HARNESS_DENIED_TOOL_BASIS,
    HARNESS_DENIED_TOOLS,
)
from tests.mcp_server._registration_recorder import RegistrationRecorder

pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[2]
DENIED = "kis_live_get_order_history"


def _all_manifest_names() -> dict[str, set[str]]:
    manifest = load_live_manifest(LIVE_MANIFEST_PATH)
    return {
        profile.value: {e.name for e in manifest.spec_for(profile).selected_entries()}
        for profile in LIVE_PROFILES
    }


def test_get_holdings_is_already_core_on_live_kr_and_live_us() -> None:
    manifest = load_live_manifest(LIVE_MANIFEST_PATH)
    for profile in (McpProfile.LIVE_KR, McpProfile.LIVE_US):
        core = {
            e.name for e in manifest.spec_for(profile).group_entries(LIVE_GROUP_CORE)
        }
        assert "get_holdings" in core, profile.value


def test_no_new_tool_name_reaches_any_live_profile() -> None:
    for profile, names in _all_manifest_names().items():
        offenders = sorted(n for n in names if "ledger_lots" in n or "kis_lots" in n)
        assert offenders == [], f"{profile}: {offenders}"


def test_live_group_counts_are_unchanged_by_task_963() -> None:
    manifest = load_live_manifest(LIVE_MANIFEST_PATH)
    spec = manifest.spec_for(McpProfile.LIVE_KR)
    counts = {
        group: len(spec.group_entries(group))
        for group in (LIVE_GROUP_CORE, LIVE_GROUP_EXTENSION, LIVE_GROUP_EMERGENCY)
    }
    assert counts == {"core": 15, "extension": 10, "emergency": 9}


def test_get_holdings_is_on_the_kr_lane_allowlist() -> None:
    lines = (REPO_ROOT / "config/mcp_lane_allowlists/kr.txt").read_text().splitlines()
    tools = {line.split("\t")[0] for line in lines if line and not line.startswith("#")}
    assert "get_holdings" in tools
    assert not {t for t in tools if "ledger_lots" in t or "kis_lots" in t}
    assert DENIED not in tools


def test_live_kr_registered_get_holdings_accepts_the_opt_in_flag() -> None:
    import inspect

    recorder = RegistrationRecorder()
    register_all_tools(cast(Any, recorder), profile=McpProfile.LIVE_KR)
    parameters = inspect.signature(recorder.tools["get_holdings"]).parameters
    assert parameters["include_ledger_lots"].default is False
    assert DENIED not in recorder.tools


def test_678_kis_live_get_order_history_stays_harness_denied() -> None:
    assert DENIED in HARNESS_DENIED_TOOLS
    assert HARNESS_DENIED_TOOL_BASIS == "live_session_harness_denied_678"
    for profile, names in _all_manifest_names().items():
        assert DENIED not in names, f"{profile}: #678 denial must hold"
    # Not registered on any live profile either (manifest equality is separately
    # pinned, this is the direct statement of the #678 boundary).
    for profile in LIVE_PROFILES:
        recorder = RegistrationRecorder()
        register_all_tools(cast(Any, recorder), profile=profile)
        assert DENIED not in recorder.tools, profile.value
