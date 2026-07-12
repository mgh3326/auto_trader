"""Registry-diff guard for route_request lane classification (ROB-649).

Forces every DEFAULT-profile tool into exactly one of two disjoint buckets
(READ_ONLY_ADVISORY_TOOLS vs MUTATION_TOOLS): a new unclassified tool makes the
partition non-total and fails CI (the silent-drift guard the issue requires,
motivated by the trade_profile tools that sat unregistered for months). Lane
membership is a cross-cutting label — each lane tool is itself either read-only
or a mutation tool — so it is validated separately against the playbook.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    LANE_SEQUENCES,
    MUTATION_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
    lane_tool_names,
)
from tests._mcp_tooling_support import DummyMCP

_PLAYBOOK_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "playbooks"
    / "trading-decision-playbook.md"
)
_YAML_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _default_tools() -> set[str]:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    return set(mcp.tools.keys())


def _collect_tool_refs(node: Any) -> list[str]:
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


def _playbook_lane_tools() -> dict[str, set[str]]:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    per_lane: dict[str, set[str]] = {}
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            for lane, body in parsed["lanes"].items():
                per_lane.setdefault(lane, set()).update(_collect_tool_refs(body))
    return per_lane


def test_buckets_are_disjoint():
    assert READ_ONLY_ADVISORY_TOOLS.isdisjoint(MUTATION_TOOLS)


def test_every_default_tool_is_classified():
    default = _default_tools()
    unclassified = default - ALL_KNOWN_TOOLS
    assert not unclassified, (
        "new DEFAULT-profile tool(s) not assigned to a route_request bucket "
        "(add to READ_ONLY_ADVISORY_TOOLS or the appropriate mutation set): "
        f"{sorted(unclassified)}"
    )


def test_read_only_bucket_has_no_phantom_tools():
    # A classified read-only tool that no longer registers = rename/removal drift.
    # Tolerate flag-gated read-only tools that are absent at default settings.
    default = _default_tools()
    _FLAG_GATED_OR_OPTIONAL: set[str] = {
        "analysis_bundle_create",
        "analysis_bundle_get",
    }
    phantom = READ_ONLY_ADVISORY_TOOLS - default - _FLAG_GATED_OR_OPTIONAL
    assert not phantom, (
        f"READ_ONLY_ADVISORY_TOOLS references unregistered tools: {sorted(phantom)}"
    )


def test_partition_is_total_at_default_settings():
    default = _default_tools()
    assert default == (READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS) & default


def test_lane_sequences_match_playbook():
    playbook = _playbook_lane_tools()
    for lane in LANE_SEQUENCES:
        assert lane in playbook, f"lane {lane!r} missing from playbook"
        assert lane_tool_names(lane) == playbook[lane], (
            f"lane {lane!r} drifted from playbook: "
            f"code={sorted(lane_tool_names(lane))} playbook={sorted(playbook[lane])}"
        )


def test_lane_tools_registered_in_default():
    default = _default_tools()
    for lane in LANE_SEQUENCES:
        missing = lane_tool_names(lane) - default
        assert not missing, (
            f"lane {lane!r} references unregistered tools: {sorted(missing)}"
        )
