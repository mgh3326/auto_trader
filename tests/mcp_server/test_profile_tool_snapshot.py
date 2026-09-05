"""Frozen actual-registration inventories, initially 14 profiles / 228 tools.

The JSON is reviewed data, never recalculated from runtime allowlists during
tests. Update only the changed profile when an intentional surface change lands.
The initial commit retains the full audit baseline in Git history.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp_server.profiles import McpProfile
from tests.mcp_server._registration_recorder import collect_profile_tools

pytestmark = pytest.mark.unit
SNAPSHOT_PATH = Path(__file__).with_name("profile_tool_snapshot.json")


@pytest.mark.parametrize("gates_enabled", [True, False], ids=["gates-on", "gates-off"])
def test_profile_tool_snapshot(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    assert SNAPSHOT_PATH.is_file(), "profile tool snapshot is missing"
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    key = "gates_enabled" if gates_enabled else "gates_disabled"
    expected = snapshot[key]
    actual = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    profiles = {profile.value for profile in McpProfile}
    assert set(expected) == profiles, "snapshot must cover every MCP profile exactly"
    assert set(actual) == profiles, "registrar inventory omitted an MCP profile"
    for profile in sorted(profiles):
        assert expected[profile] == sorted(set(expected[profile])), (
            f"{profile}: snapshot must contain sorted, unique tool names"
        )
        missing = sorted(set(expected[profile]) - set(actual[profile]))
        added = sorted(set(actual[profile]) - set(expected[profile]))
        assert actual[profile] == expected[profile], (
            f"{profile} ({key}): MCP registration snapshot changed; "
            f"missing={missing}, added={added}"
        )
