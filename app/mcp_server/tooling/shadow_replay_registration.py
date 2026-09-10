"""Exact closed-world registration for the B0X observation/replay profile."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.services.shadow_replay.portability import (
    canonical_session_context_read,
    deterministic_replay_compare,
    emitted_trigger_playbook_read,
    identical_input_snapshot_read,
    raw_difference_artifact_write,
    shadow_report_write,
    source_event_observe,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

SHADOW_REPLAY_TOOL_NAMES = frozenset(
    {
        "canonical_session_context_read",
        "source_event_observe",
        "emitted_trigger_playbook_read",
        "identical_input_snapshot_read",
        "shadow_report_write",
        "deterministic_replay_compare",
        "raw_difference_artifact_write",
    }
)
DENIED_CAPABILITY_FRAGMENTS = frozenset(
    {
        "proposal",
        "place",
        "cancel",
        "watch",
        "broker",
        "order_execution",
        "automatic_approval",
        "action",
    }
)


class ShadowReplaySurfaceViolation(RuntimeError):
    """The actually served observation-only surface drifted."""


def _assert_name_matrix(names: frozenset[str]) -> None:
    denied = sorted(
        name
        for name in names
        if any(fragment in name.lower() for fragment in DENIED_CAPABILITY_FRAGMENTS)
    )
    if denied:
        raise ShadowReplaySurfaceViolation(f"denied capability name(s): {denied}")


def register_shadow_replay_tools(mcp: FastMCP) -> None:
    """Register the seven real functions and no compatibility aliases."""

    registrations = (
        (
            canonical_session_context_read,
            "Read supplied canonical session context from the confined artifact root.",
        ),
        (
            source_event_observe,
            "Observe and validate one supplied B0X source event without consuming it.",
        ),
        (
            emitted_trigger_playbook_read,
            "Read the supplied playbook referenced by an emitted trigger.",
        ),
        (
            identical_input_snapshot_read,
            "Read a supplied identical-input snapshot for deterministic replay.",
        ),
        (
            shadow_report_write,
            "Write one create-only shadow report bound to source, playbook, identical input, canonical context, and independent provenance.",
        ),
        (
            deterministic_replay_compare,
            "Compare exactly the six PR49 deterministic domains with independent live/shadow provenance.",
        ),
        (
            raw_difference_artifact_write,
            "Write classified raw differences with complete independent artifact provenance.",
        ),
    )
    names = frozenset(function.__name__ for function, _ in registrations)
    if names != SHADOW_REPLAY_TOOL_NAMES:
        raise ShadowReplaySurfaceViolation("registration function set is not exact")
    _assert_name_matrix(names)
    for function, description in registrations:
        mcp.tool(name=function.__name__, description=description)(function)


def build_shadow_replay_server(*, name: str = "auto-trader-shadow-replay") -> FastMCP:
    """Build an unstarted server through the production profile registry."""

    from fastmcp import FastMCP as _FastMCP

    from app.mcp_server.profiles import McpProfile
    from app.mcp_server.tooling.registry import register_all_tools

    mcp = _FastMCP(name=name, on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.SHADOW_REPLAY)
    return cast("FastMCP", mcp)


async def provisioned_shadow_tool_names(mcp: Any) -> frozenset[str]:
    lister = getattr(mcp, "list_tools", None)
    if lister is None:
        raise TypeError("server object does not serve tools/list")
    names = frozenset(str(tool.name) for tool in await lister())
    _assert_name_matrix(names)
    return names


async def assert_shadow_replay_surface(mcp: Any) -> frozenset[str]:
    served = await provisioned_shadow_tool_names(mcp)
    if served != SHADOW_REPLAY_TOOL_NAMES:
        raise ShadowReplaySurfaceViolation(
            "shadow-replay MCP surface mismatch: "
            f"missing={sorted(SHADOW_REPLAY_TOOL_NAMES - served)}, "
            f"extra={sorted(served - SHADOW_REPLAY_TOOL_NAMES)}"
        )
    return served


__all__ = [
    "DENIED_CAPABILITY_FRAGMENTS",
    "SHADOW_REPLAY_TOOL_NAMES",
    "ShadowReplaySurfaceViolation",
    "assert_shadow_replay_surface",
    "build_shadow_replay_server",
    "provisioned_shadow_tool_names",
    "register_shadow_replay_tools",
]
