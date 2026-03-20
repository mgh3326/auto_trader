"""Sentry scope enrichment middleware for MCP tool calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sentry_sdk

from app.monitoring.sentry import enrich_mcp_tool_call_scope, get_mcp_http_scopes

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult

from fastmcp.server.middleware import Middleware


class McpToolCallSentryMiddleware(Middleware):
    """Set ``mcp.tool.name`` tag and ``mcp_tool_call`` structured context
    on the current Sentry scope for every ``tools/call`` request.

    The middleware **never** calls ``capture_exception`` itself — the
    existing ``sentry-sdk`` ``MCPIntegration`` captures within the same
    scope, so the enriched context is automatically attached to the event.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name: str = context.message.name
        arguments: dict[str, Any] = context.message.arguments or {}

        http_scopes = get_mcp_http_scopes()
        if http_scopes is not None:
            isolation_scope, _ = http_scopes
            if isolation_scope is not None:
                enrich_mcp_tool_call_scope(isolation_scope, tool_name, arguments)
                return await call_next(context)

        with sentry_sdk.isolation_scope() as isolation_scope:
            enrich_mcp_tool_call_scope(isolation_scope, tool_name, arguments)
            return await call_next(context)
