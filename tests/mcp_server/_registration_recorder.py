"""Registration-only recorder: never construct a server or invoke a tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import register_all_tools


class RegistrationRecorder:
    """Support FastMCP's direct and decorator forms with duplicate detection."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}
        self.options: dict[str, dict[str, Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        name = kwargs.get("name")
        direct = args[0] if args and callable(args[0]) else None
        if name is None and args and isinstance(args[0], str):
            name = args[0]

        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or function.__name__
            assert isinstance(tool_name, str) and tool_name, "unnamed MCP tool"
            assert tool_name not in self.tools, f"duplicate MCP tool: {tool_name}"
            self.tools[tool_name] = function
            self.options[tool_name] = dict(kwargs)
            return function

        return register(direct) if direct is not None else register


def collect_profile_tools(
    monkeypatch: pytest.MonkeyPatch, *, gates_enabled: bool
) -> dict[str, list[str]]:
    """Run every actual registrar with local, automatically restored gates.

    This matches the audit's feature-gate inventory, including nested profile
    proxies, without importing main.py or calling any broker/service handler.
    Gate-off coverage separately freezes physical default-disabled behavior.
    """
    with monkeypatch.context() as gate_patch:
        for name in type(settings).model_fields:
            if name.lower().endswith("enabled"):
                gate_patch.setattr(settings, name, gates_enabled)
        profiles: dict[str, list[str]] = {}
        for profile in McpProfile:
            recorder = RegistrationRecorder()
            register_all_tools(cast(Any, recorder), profile=profile)
            profiles[profile.value] = sorted(recorder.tools)
        return profiles
