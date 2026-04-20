"""FastMCP middleware for exposing caller identity via contextvars."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from app.core.config import settings
from app.mcp_server.caller_identity import (
    CallerSource,
    caller_agent_id_var,
    caller_source_var,
)

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult


CALLER_AGENT_ID_HEADER = "x-paperclip-agent-id"


def _clean_agent_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _caller_from_http_header() -> str | None:
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    return _clean_agent_id(request.headers.get(CALLER_AGENT_ID_HEADER))


def _resolve_caller_identity() -> tuple[str | None, CallerSource]:
    header_agent_id = _caller_from_http_header()
    if header_agent_id is not None:
        return header_agent_id, "http_header"

    fallback_agent_id = _clean_agent_id(settings.mcp_caller_agent_id_fallback)
    if fallback_agent_id is not None:
        return fallback_agent_id, "env_fallback"

    return None, "none"


class CallerIdentityMiddleware(Middleware):
    """Resolve MCP caller identity once per tool call."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        caller_agent_id, caller_source = _resolve_caller_identity()
        agent_id_token = caller_agent_id_var.set(caller_agent_id)
        source_token = caller_source_var.set(caller_source)
        try:
            return await call_next(context)
        finally:
            caller_source_var.reset(source_token)
            caller_agent_id_var.reset(agent_id_token)
