from __future__ import annotations

from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import session_bootstrap_pack as pack
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.session_bootstrap_pack import SECTION_SOURCE_TOOLS
from tests.mcp_server._registration_recorder import (
    RegistrationRecorder,
    collect_profile_tools,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("gates_enabled", [False, True])
@pytest.mark.parametrize("profile", list(McpProfile))
async def test_sections_follow_the_actual_profile_registration(
    monkeypatch: pytest.MonkeyPatch,
    gates_enabled: bool,
    profile: McpProfile,
) -> None:
    inventories = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    registered = set(inventories[profile.value])

    async def source(*args, **kwargs):
        return {"success": True}

    monkeypatch.setattr(pack, "_section_source", source)
    recorder = RegistrationRecorder()
    with monkeypatch.context() as gate_patch:
        for name in type(settings).model_fields:
            if name.lower().endswith("enabled"):
                gate_patch.setattr(settings, name, gates_enabled)
        register_all_tools(cast(Any, recorder), profile=profile)
    assert set(recorder.tools) == registered
    result = await recorder.tools["session_bootstrap_pack"]("kr")

    for section, source_tool in SECTION_SOURCE_TOOLS.items():
        if source_tool in inventories[profile.value]:
            assert result["meta"]["sections"][section]["state"] == "fresh"
        else:
            assert result["sections"][section] == {
                "state": "denied_by_profile",
                "tool": source_tool,
            }


@pytest.mark.asyncio
@pytest.mark.parametrize("gates_enabled", [False, True])
async def test_analysis_readonly_has_exactly_four_filled_sections(
    monkeypatch: pytest.MonkeyPatch, gates_enabled: bool
) -> None:
    inventories = collect_profile_tools(monkeypatch, gates_enabled=gates_enabled)
    registered = set(inventories[McpProfile.ANALYSIS_READONLY.value])
    expected_filled = {"briefing", "holdings", "policy", "recent_context"}

    assert {
        section for section, tool in SECTION_SOURCE_TOOLS.items() if tool in registered
    } == expected_filled

    async def source(*args: object, **kwargs: object) -> dict[str, object]:
        return {"success": True}

    monkeypatch.setattr(pack, "_section_source", source)
    result = await pack._session_bootstrap_pack(
        "kr", None, False, registered_tool_names=lambda: registered
    )
    assert {
        section
        for section, meta in result["meta"]["sections"].items()
        if meta["state"] == "fresh"
    } == expected_filled
    assert {
        section
        for section, value in result["sections"].items()
        if value.get("state") == "denied_by_profile"
    } == set(SECTION_SOURCE_TOOLS) - expected_filled
