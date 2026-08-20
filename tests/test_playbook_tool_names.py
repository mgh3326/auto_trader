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

from app.core.config import settings
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


def test_playbook_tools_exist_in_proposal_enabled_default_profile(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    registry = _default_profile_tools()
    refs = set(_playbook_tool_refs())
    missing = sorted(refs - registry)
    assert not missing, (
        "playbook references tools absent from the DEFAULT MCP profile "
        f"(rename/removal drift): {missing}"
    )


def test_buy_sell_have_one_canonical_proposal_step() -> None:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    per_lane: dict[str, list[str]] = {}
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            for lane, body in parsed["lanes"].items():
                per_lane.setdefault(lane, []).extend(_collect_tool_refs(body))

    for lane in ("buy", "sell"):
        assert per_lane[lane].count("order_proposal_create") == 1
        assert per_lane[lane][-1] == "order_proposal_create"


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


def test_buy_pipeline_negative_recording_precedes_reserve_net_subsection() -> None:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    negative_recording = text.index("8. **Negative-class recording (ROB-712):")
    reserve_net = text.index("### 1.1 `buy.support_reserve_net`")
    buy_lane_yaml = text.index("# playbook-machine-readable: buy lane")

    assert negative_recording < reserve_net < buy_lane_yaml


def test_reserve_net_playbook_repeats_machine_policy_priority_contract() -> None:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    reserve_net = text.index("### 1.1 `buy.support_reserve_net`")
    buy_lane_yaml = text.index("# playbook-machine-readable: buy lane")
    section = text[reserve_net:buy_lane_yaml]

    for requirement in (
        "이미 active/resting인 동일 symbol을 먼저 dedupe한다.",
        "첫 슬롯은 eligible 신규 후보에 우선 배정한다.",
        "R-931 재심사 PASS와 Q4의 `A_limit(10%)` 완전충족",
        "`strong > moderate`, `3-source > 2-source`",
        "완전 동률이면 신규가 물타기보다 먼저다.",
        "`A_limit<=0` (already target met) is\n   **NO_ORDER**",
    ):
        assert requirement in section


def test_rob1301_shadow_block_is_not_a_lane_and_carries_the_forbidden_three() -> None:
    text = _PLAYBOOK_PATH.read_text(encoding="utf-8")
    section_start = text.index("### 3.2 ROB-1301 buy-gate A/B shadow")
    section_end = text.index("## 4) Recording / retrospective")
    section = text[section_start:section_end]
    for forbidden in (
        "shadow가 제안·주문·워치로 승격 금지(순수 기록)",
        "라이브 게이트 문언 무접촉",
        "채점 전 중간값으로 정책 변경 논거 삼지 않기(사전 등록 원칙)",
    ):
        assert forbidden in section
    assert "evaluate_buy_gate_ab_shadow" in section
    assert "- tool: order_proposal_create" not in section
    assert "never order_proposal_create" in section
    assert "NOT a lane sequence" in section

    lane_names: set[str] = set()
    for block in _YAML_BLOCK_RE.findall(text):
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("lanes"), dict):
            lane_names.update(parsed["lanes"].keys())
    assert "rob-1301-buy-gate-ab" not in lane_names
