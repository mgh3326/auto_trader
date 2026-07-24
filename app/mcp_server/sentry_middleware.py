"""Sentry scope enrichment middleware for MCP tool calls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from app.mcp_server.caller_identity import get_caller_agent_id, get_caller_source
from app.monitoring.sentry import (
    enrich_mcp_tool_call_scope,
    get_mcp_http_scopes,
    record_mcp_tool_call_result,
)

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.tool import ToolResult

logger = logging.getLogger(__name__)


def _get_transport_session_id() -> str | None:
    try:
        request = get_http_request()
    except RuntimeError:
        return None

    session_id = request.headers.get("mcp-session-id")
    if not session_id:
        session_id = request.query_params.get("session_id")
    if not session_id:
        return None
    cleaned = session_id.strip()
    return cleaned or None


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
        caller_agent_id = get_caller_agent_id()
        caller_source = get_caller_source()
        transport_session_id = _get_transport_session_id()
        span = sentry_sdk.get_current_span()

        async def call_with_observability(scope: sentry_sdk.Scope) -> ToolResult:
            try:
                enrich_mcp_tool_call_scope(
                    scope,
                    tool_name,
                    arguments,
                    caller_agent_id=caller_agent_id,
                    caller_source=caller_source,
                    transport_session_id=transport_session_id,
                    span=span,
                )
            except Exception:  # noqa: BLE001 - telemetry must not break the tool
                logger.exception(
                    "Failed to enrich MCP Sentry call scope for tool=%s",
                    tool_name,
                )

            try:
                result = await call_next(context)
            except BaseException as exc:
                try:
                    record_mcp_tool_call_result(
                        scope,
                        tool_name,
                        arguments,
                        exception=exc,
                        caller_agent_id=caller_agent_id,
                        caller_source=caller_source,
                        transport_session_id=transport_session_id,
                        span=span,
                    )
                except Exception:  # noqa: BLE001 - preserve the original exception
                    logger.exception(
                        "Failed to record MCP Sentry exception result for tool=%s",
                        tool_name,
                    )
                raise

            try:
                record_mcp_tool_call_result(
                    scope,
                    tool_name,
                    arguments,
                    result=result,
                    caller_agent_id=caller_agent_id,
                    caller_source=caller_source,
                    transport_session_id=transport_session_id,
                    span=span,
                )
            except Exception:  # noqa: BLE001 - telemetry must not break the tool
                logger.exception(
                    "Failed to record MCP Sentry result for tool=%s",
                    tool_name,
                )
            return result

        http_scopes = get_mcp_http_scopes()
        if http_scopes is not None:
            isolation_scope, _ = http_scopes
            if isolation_scope is not None:
                return await call_with_observability(isolation_scope)

        with sentry_sdk.isolation_scope() as isolation_scope:
            return await call_with_observability(isolation_scope)
