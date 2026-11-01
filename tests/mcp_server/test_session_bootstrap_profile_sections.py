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
@pytest.mark.parametrize(
    "profile",
    [
        profile
        for profile in McpProfile
        if profile
        not in (
            # FILL_WATCH_CONTEXT is a two-tool, context-only closed world.
            McpProfile.FILL_WATCH_CONTEXT,
            # SHADOW_REPLAY exposes only its exact seven observation/replay tools.
            McpProfile.SHADOW_REPLAY,
            # #1171 — the H3-CRYPTO paper surface is the runner's exact 20
            # tools; session_bootstrap_pack is not one of them.
            McpProfile.H3_CRYPTO_PAPER,
            # #1257 — likewise the H3-US paper surface (runner's exact 20).
            McpProfile.H3_US_PAPER,
        )
    ],
)
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
