"""Drift guard for docs/playbooks/trading-decision-playbook.md (ROB-643).

The playbook embeds machine-readable ```yaml ``` blocks whose ``lanes:`` define
the standard per-lane MCP tool sequence (the lane-definition source for
ROB-649 ``route_request``). This test parses those blocks, collects every
``tool:`` reference, and asserts each one still exists in the DEFAULT MCP
profile — so the playbook cannot silently drift away from the live tool
registry.

Reuses the ``DummyMCP`` + ``register_all_tools`` pattern from
``tests/test_mcp_profiles.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_PLAYBOOK_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "playbooks"
    / "trading-decision-playbook.md"
)

_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _default_profile_tools() -> set[str]:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    return set(mcp.tools.keys())


def _collect_tool_refs(node: Any) -> list[str]:
    """Recursively collect every value stored under a ``tool:`` key."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tool" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_tool_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_tool_refs(item))
    return found


def _playbook_tool_refs() -> list[str]:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    refs: list[str] = []
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        refs.extend(_collect_tool_refs(parsed))
    return refs


def test_playbook_file_exists() -> None:
    assert _PLAYBOOK_PATH.is_file(), f"missing playbook: {_PLAYBOOK_PATH}"


def test_playbook_yaml_blocks_are_parseable_and_nonempty() -> None:
    # Guards against a silent parse regression that would make the drift check
    # vacuously pass (zero tools collected).
    refs = _playbook_tool_refs()
    assert len(refs) >= 10, (
        f"expected the playbook lanes to reference >=10 tools, found {len(refs)}: "
        f"{sorted(set(refs))}"
    )


def test_playbook_tools_exist_in_default_profile() -> None:
    registry = _default_profile_tools()
    refs = set(_playbook_tool_refs())
    missing = sorted(refs - registry)
    assert not missing, (
        "playbook references tools absent from the DEFAULT MCP profile "
        f"(rename/removal drift): {missing}"
    )


def test_playbook_covers_core_lanes() -> None:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    lane_names: set[str] = set()
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            lane_names.update(parsed["lanes"].keys())
    assert {"bootstrap", "buy", "sell", "discovery"} <= lane_names, (
        f"playbook must define bootstrap/buy/sell/discovery lanes; found {sorted(lane_names)}"
    )
