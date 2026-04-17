"""Unit tests for CallerIdentityMiddleware.on_call_tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import settings
from app.mcp_server import caller_identity_middleware
from app.mcp_server.caller_identity import (
    caller_agent_id_var,
    caller_source_var,
)
from app.mcp_server.caller_identity_middleware import (
    CALLER_AGENT_ID_HEADER,
    CallerIdentityMiddleware,
)


def _make_http_request(headers: dict[str, str]) -> Mock:
    request = Mock()
    request.headers = headers
    return request


def _capturing_call_next() -> tuple[AsyncMock, dict[str, Any]]:
    """Return a call_next mock that records contextvar state at the time of invocation."""
    observed: dict[str, Any] = {}

    async def _record(ctx: Any) -> str:
        observed["caller_agent_id"] = caller_agent_id_var.get()
        observed["caller_source"] = caller_source_var.get()
        return "tool-result"

    return AsyncMock(side_effect=_record), observed


@pytest.fixture(autouse=True)
def _clear_fallback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep env fallback empty unless a test opts in."""
    monkeypatch.setattr(
        settings,
        "mcp_caller_agent_id_fallback",
        None,
        raising=False,
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestCallerIdentityMiddleware:
    async def test_http_header_resolves_caller_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Header `x-paperclip-agent-id` populates contextvar with source=http_header."""
        monkeypatch.setattr(
            caller_identity_middleware,
            "get_http_request",
            lambda: _make_http_request({CALLER_AGENT_ID_HEADER: "agent-xyz"}),
        )
        call_next, observed = _capturing_call_next()

        result = await CallerIdentityMiddleware().on_call_tool(Mock(), call_next)

        assert result == "tool-result"
        assert observed == {
            "caller_agent_id": "agent-xyz",
            "caller_source": "http_header",
        }

    async def test_env_fallback_used_when_header_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing HTTP request + env fallback set → source=env_fallback."""

        def _raise_no_http() -> None:
            raise RuntimeError("no HTTP request in context")

        monkeypatch.setattr(
            caller_identity_middleware, "get_http_request", _raise_no_http
        )
        monkeypatch.setattr(
            settings, "mcp_caller_agent_id_fallback", "env-agent", raising=False
        )
        call_next, observed = _capturing_call_next()

        await CallerIdentityMiddleware().on_call_tool(Mock(), call_next)

        assert observed == {
            "caller_agent_id": "env-agent",
            "caller_source": "env_fallback",
        }

    async def test_caller_none_when_neither_header_nor_env_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing HTTP request + no env fallback → caller=None, source=none."""

        def _raise_no_http() -> None:
            raise RuntimeError("no HTTP request in context")

        monkeypatch.setattr(
            caller_identity_middleware, "get_http_request", _raise_no_http
        )
        call_next, observed = _capturing_call_next()

        await CallerIdentityMiddleware().on_call_tool(Mock(), call_next)

        assert observed == {"caller_agent_id": None, "caller_source": "none"}

    async def test_contextvars_restored_after_call_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Middleware restores contextvar state via reset(token) after call_next."""
        monkeypatch.setattr(
            caller_identity_middleware,
            "get_http_request",
            lambda: _make_http_request({CALLER_AGENT_ID_HEADER: "agent-xyz"}),
        )

        pre_agent_token = caller_agent_id_var.set("pre-existing-agent")
        pre_source_token = caller_source_var.set("env_fallback")
        try:
            await CallerIdentityMiddleware().on_call_tool(
                Mock(), AsyncMock(return_value="ok")
            )
            assert caller_agent_id_var.get() == "pre-existing-agent"
            assert caller_source_var.get() == "env_fallback"
        finally:
            caller_source_var.reset(pre_source_token)
            caller_agent_id_var.reset(pre_agent_token)

    async def test_contextvars_restored_even_when_call_next_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """reset tokens fire in `finally` so exceptions don't leak contextvar state."""
        monkeypatch.setattr(
            caller_identity_middleware,
            "get_http_request",
            lambda: _make_http_request({CALLER_AGENT_ID_HEADER: "agent-xyz"}),
        )

        pre_agent_token = caller_agent_id_var.set("pre-existing-agent")
        pre_source_token = caller_source_var.set("env_fallback")
        try:
            with pytest.raises(RuntimeError, match="tool exploded"):
                await CallerIdentityMiddleware().on_call_tool(
                    Mock(),
                    AsyncMock(side_effect=RuntimeError("tool exploded")),
                )
            assert caller_agent_id_var.get() == "pre-existing-agent"
            assert caller_source_var.get() == "env_fallback"
        finally:
            caller_source_var.reset(pre_source_token)
            caller_agent_id_var.reset(pre_agent_token)

    async def test_blank_header_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only header treated as missing, env fallback used."""
        monkeypatch.setattr(
            caller_identity_middleware,
            "get_http_request",
            lambda: _make_http_request({CALLER_AGENT_ID_HEADER: "   "}),
        )
        monkeypatch.setattr(
            settings, "mcp_caller_agent_id_fallback", "env-agent", raising=False
        )
        call_next, observed = _capturing_call_next()

        await CallerIdentityMiddleware().on_call_tool(Mock(), call_next)

        assert observed == {
            "caller_agent_id": "env-agent",
            "caller_source": "env_fallback",
        }
