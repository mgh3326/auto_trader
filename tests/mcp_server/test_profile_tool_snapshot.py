"""Per-profile registered-tool snapshot.

`tests/mcp_server/data/mcp_profile_tool_snapshot.json` pins exactly which tools
each `McpProfile` registers. Any change to the MCP surface — a dropped dead
tool, a new tool, a profile-branch edit — must move the snapshot in the same
commit, so "which tools does profile X expose?" stays reviewable in a diff
instead of being derivable only by running the server.

Measured like the audit: the real `register_all_tools` executed against an
in-memory recorder with every feature gate enabled. No server/client/broker/DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.mcp_server.profiles import McpProfile
from scripts.mcp_tool_usage_audit import collect_registry

SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "data" / "mcp_profile_tool_snapshot.json"
)


@pytest.fixture(scope="module")
def registered() -> dict[str, list[str]]:
    registry = collect_registry()
    by_profile: dict[str, list[str]] = {}
    for tool, entry in registry.items():
        for profile in entry["profiles"]:
            by_profile.setdefault(profile, []).append(tool)
    return {profile: sorted(tools) for profile, tools in sorted(by_profile.items())}


@pytest.fixture(scope="module")
def snapshot() -> dict[str, list[str]]:
    return json.loads(SNAPSHOT_PATH.read_text())


def test_snapshot_covers_exactly_the_declared_profiles(
    snapshot: dict[str, list[str]],
) -> None:
    assert set(snapshot) == {p.value for p in McpProfile}, (
        "snapshot profile keys do not match McpProfile; "
        f"snapshot={sorted(snapshot)} enum={sorted(p.value for p in McpProfile)}"
    )


def test_registered_profiles_match_snapshot(
    registered: dict[str, list[str]], snapshot: dict[str, list[str]]
) -> None:
    assert set(registered) == set(snapshot), (
        "profiles that register at least one tool changed; "
        f"registered={sorted(registered)} snapshot={sorted(snapshot)}"
    )


@pytest.mark.parametrize("profile", sorted(p.value for p in McpProfile))
def test_profile_tool_set_matches_snapshot(
    profile: str, registered: dict[str, list[str]], snapshot: dict[str, list[str]]
) -> None:
    """Fails naming the exact tools that appeared/disappeared for this profile."""
    now = set(registered.get(profile, []))
    pinned = set(snapshot.get(profile, []))
    added = sorted(now - pinned)
    removed = sorted(pinned - now)
    assert not added and not removed, (
        f"profile {profile!r} tool set drifted from the committed snapshot "
        f"(regenerate tests/mcp_server/data/mcp_profile_tool_snapshot.json in "
        f"the same commit): added={added} removed={removed}"
    )


def test_snapshot_is_sorted_and_deduplicated(snapshot: dict[str, list[str]]) -> None:
    for profile, tools in snapshot.items():
        assert tools == sorted(set(tools)), (
            f"snapshot for profile {profile!r} must be sorted and unique"
        )
