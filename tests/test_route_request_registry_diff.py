"""Registry and ordered-playbook drift guards for route_request.

Forces every DEFAULT-profile tool into exactly one of two disjoint buckets
(READ_ONLY_ADVISORY_TOOLS vs MUTATION_TOOLS): a new unclassified tool makes the
partition non-total and fails CI (the silent-drift guard the issue requires,
motivated by the trade_profile tools that sat unregistered for months). Lane
membership is validated in order against the playbook. ROB-1045 also partitions
the legacy mutation bucket into explicit action classes so a new direct broker
mutation cannot silently become route-allowed.
"""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path
from typing import Any, cast

import yaml

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    ALPACA_PAPER_AUTOMATED_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_preview import ALPACA_PAPER_PREVIEW_TOOL_NAMES
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
)
from app.mcp_server.tooling.order_proposal_tools import ORDER_PROPOSAL_TOOL_NAMES
from app.mcp_server.tooling.orders_kiwoom_us_variants import (
    KIWOOM_MOCK_US_MUTATION_TOOL_NAMES,
    KIWOOM_MOCK_US_READ_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    DIRECT_BROKER_MUTATION_TOOLS,
    LANE_SEQUENCES,
    MUTATION_TOOLS,
    ORDER_PROPOSAL_READ_TOOLS,
    PREVIEW_REVALIDATION_TOOLS,
    PROPOSAL_LED_TOOLS,
    PROPOSAL_LIFECYCLE_TOOLS,
    READ_ONLY_ADVISORY_TOOLS,
    RECONCILE_TOOLS,
    RESERVE_NET_CONSUMER_TOOLS,
    STATUS_HELPER_TOOLS,
    ordered_lane_tool_names,
)
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES
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


def _playbook_lane_tools() -> dict[str, list[str]]:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    per_lane: dict[str, list[str]] = {}
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            for lane, body in parsed["lanes"].items():
                per_lane.setdefault(lane, []).extend(_collect_tool_refs(body))
    return per_lane


def test_buckets_are_disjoint():
    assert READ_ONLY_ADVISORY_TOOLS.isdisjoint(MUTATION_TOOLS)
    assert KIWOOM_MOCK_US_READ_TOOL_NAMES <= READ_ONLY_ADVISORY_TOOLS
    assert KIWOOM_MOCK_US_MUTATION_TOOL_NAMES <= MUTATION_TOOLS
    assert "discover_buy_candidates_fanout" in READ_ONLY_ADVISORY_TOOLS
    assert "discover_buy_candidates_fanout" not in MUTATION_TOOLS
    assert "evaluate_buy_gate_ab_shadow" in READ_ONLY_ADVISORY_TOOLS
    assert "evaluate_buy_gate_ab_shadow" not in MUTATION_TOOLS


def test_mutation_action_taxonomy_is_disjoint_and_total():
    action_classes = (
        DIRECT_BROKER_MUTATION_TOOLS,
        PROPOSAL_LED_TOOLS,
        PROPOSAL_LIFECYCLE_TOOLS,
        RESERVE_NET_CONSUMER_TOOLS,
        PREVIEW_REVALIDATION_TOOLS,
        RECONCILE_TOOLS,
        STATUS_HELPER_TOOLS,
    )
    for left, right in combinations(action_classes, 2):
        assert left.isdisjoint(right)
    assert frozenset().union(*action_classes) == MUTATION_TOOLS

    direct_name_candidates = {
        name
        for name in MUTATION_TOOLS
        if any(
            marker in name
            for marker in (
                "place_order",
                "modify_order",
                "cancel_order",
                "submit_order",
                "submit_decision",
                "execute_report",
                "place_limit_order",
                "cancel_pending_order",
            )
        )
    }
    assert direct_name_candidates == DIRECT_BROKER_MUTATION_TOOLS


def test_registered_direct_surfaces_are_classified_across_route_profiles(
    monkeypatch,
):
    monkeypatch.setattr(settings, "alpaca_paper_default_tools_enabled", True)
    monkeypatch.setattr(settings, "binance_demo_scalping_enabled", True)

    direct_markers = (
        "place_order",
        "modify_order",
        "cancel_order",
        "submit_order",
        "submit_decision",
        "execute_report",
        "place_limit_order",
        "cancel_pending_order",
    )
    for profile in McpProfile:
        mcp = DummyMCP()
        register_all_tools(cast(Any, mcp), profile=profile)
        if "route_request" not in mcp.tools:
            continue
        registered_direct = {
            name
            for name in mcp.tools
            if any(marker in name for marker in direct_markers)
        }
        assert registered_direct <= DIRECT_BROKER_MUTATION_TOOLS, (
            f"{profile.value} has unclassified direct mutations: "
            f"{sorted(registered_direct - DIRECT_BROKER_MUTATION_TOOLS)}"
        )

    assert "alpaca_paper_automated_submit_order" in DIRECT_BROKER_MUTATION_TOOLS
    assert "alpaca_paper_automated_preview_order" in PREVIEW_REVALIDATION_TOOLS
    assert ALPACA_PAPER_AUTOMATED_TOOL_NAMES <= MUTATION_TOOLS


def test_every_default_tool_is_classified():
    default = _default_tools()
    unclassified = default - ALL_KNOWN_TOOLS
    assert not unclassified, (
        "new DEFAULT-profile tool(s) not assigned to a route_request bucket "
        "(add to READ_ONLY_ADVISORY_TOOLS or the appropriate mutation set): "
        f"{sorted(unclassified)}"
    )


def test_every_proposal_enabled_default_tool_is_classified(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    default = _default_tools()

    assert ORDER_PROPOSAL_TOOL_NAMES <= default
    assert default <= ALL_KNOWN_TOOLS
    assert ORDER_PROPOSAL_TOOL_NAMES == (
        ORDER_PROPOSAL_READ_TOOLS
        | PROPOSAL_LED_TOOLS
        | PROPOSAL_LIFECYCLE_TOOLS
        | RESERVE_NET_CONSUMER_TOOLS
    )
    assert ORDER_PROPOSAL_READ_TOOLS <= READ_ONLY_ADVISORY_TOOLS
    assert (
        PROPOSAL_LED_TOOLS | PROPOSAL_LIFECYCLE_TOOLS | RESERVE_NET_CONSUMER_TOOLS
        <= MUTATION_TOOLS
    )


def test_read_only_bucket_has_no_phantom_tools():
    # A classified read-only tool that no longer registers = rename/removal drift.
    # Tolerate flag-gated read-only tools that are absent at default settings.
    default = _default_tools()
    _FLAG_GATED_OR_OPTIONAL: set[str] = {
        "analysis_bundle_create",
        "analysis_bundle_get",
        # ROB-907: gated by settings.binance_demo_scalping_enabled (default off).
        "binance_demo_ledger_status",
        # ROB-908: Alpaca paper read/preview/us_dual surface, gated by
        # settings.alpaca_paper_default_tools_enabled (default off).
        *ALPACA_PAPER_READONLY_TOOL_NAMES,
        *ALPACA_PAPER_PREVIEW_TOOL_NAMES,
        *US_DUAL_PAPER_TOOL_NAMES,
        *KIWOOM_MOCK_US_READ_TOOL_NAMES,
        *MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
        *ORDER_PROPOSAL_READ_TOOLS,
    }
    phantom = READ_ONLY_ADVISORY_TOOLS - default - _FLAG_GATED_OR_OPTIONAL
    assert not phantom, (
        f"READ_ONLY_ADVISORY_TOOLS references unregistered tools: {sorted(phantom)}"
    )


def test_partition_is_total_at_default_settings():
    default = _default_tools()
    assert default == (READ_ONLY_ADVISORY_TOOLS | MUTATION_TOOLS) & default


def test_lane_sequences_match_playbook_in_exact_order():
    playbook = _playbook_lane_tools()
    for lane in LANE_SEQUENCES:
        assert lane in playbook, f"lane {lane!r} missing from playbook"
        assert ordered_lane_tool_names(lane) == playbook[lane], (
            f"lane {lane!r} drifted from playbook: "
            f"code={ordered_lane_tool_names(lane)} playbook={playbook[lane]}"
        )
        assert len(playbook[lane]) == len(set(playbook[lane])), (
            f"lane {lane!r} has duplicate playbook steps: {playbook[lane]}"
        )


def test_lane_tools_registered_in_proposal_enabled_default(monkeypatch):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    default = _default_tools()
    for lane in LANE_SEQUENCES:
        missing = set(ordered_lane_tool_names(lane)) - default
        assert not missing, (
            f"lane {lane!r} references unregistered tools: {sorted(missing)}"
        )
