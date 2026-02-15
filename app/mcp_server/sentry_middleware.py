"""Sentry tracing middleware for FastMCP tool calls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import sentry_sdk
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastmcp.tools.tool import ToolResult as ToolResultType

logger = logging.getLogger(__name__)

_ACTION_PRIORITY = ("action", "side", "order_type")


def _extract_action(arguments: dict[str, Any] | None) -> str | None:
    """Extract action label from tool arguments using priority: action > side > order_type."""
    if not arguments:
        return None
    for key in _ACTION_PRIORITY:
        if (value := arguments.get(key)) is not None:
            return str(value)
    return None


def _is_error_result(result: Any) -> bool:
    """Check if the tool result indicates an error."""
    if isinstance(result, CallToolResult):
        return result.isError
    if isinstance(result, ToolResult):
        structured = result.structured_content
        if isinstance(structured, dict):
            if structured.get("error") or structured.get("isError"):
                return True
            nested = structured.get("result")
            return isinstance(nested, dict) and nested.get("isError", False)
        return False
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        return isinstance(structured, dict) and bool(
            structured.get("error") or structured.get("isError")
        )
    return False


class McpSentryTracingMiddleware(Middleware):
    """Sentry tracing for MCP tool calls.

    Sets transaction name to mcp.<tool_name>, creates child spans,
    adds tags for filtering, and tracks errors.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[ToolResultType]],
    ) -> ToolResultType:
        message = context.message
        tool_name = getattr(message, "name", "unknown")
        arguments = getattr(message, "arguments", None)
        action = _extract_action(arguments)
        span_name = f"{tool_name}:{action}" if action else tool_name
        transaction_name = f"mcp.{tool_name}"

        scope = sentry_sdk.get_current_scope()
        scope.set_transaction_name(transaction_name, source="custom")
        scope.set_tag("mcp.tool_name", tool_name)
        scope.set_tag("mcp.method", "tools/call")
        if action:
            scope.set_tag("mcp.action", action)

        # FastMCP tool execution can happen without an active Sentry span context.
        # In that case create a transaction so child DB/http spans are captured.
        active_span = scope.span
        if active_span is None:
            with sentry_sdk.start_transaction(
                name=transaction_name,
                op="mcp.request",
                source="custom",
            ) as transaction:
                transaction.set_tag("mcp.tool_name", tool_name)
                transaction.set_tag("mcp.method", "tools/call")
                if action:
                    transaction.set_tag("mcp.action", action)
                return await self._run_tool_span(
                    context=context,
                    call_next=call_next,
                    span_name=span_name,
                    arguments=arguments,
                    transaction=transaction,
                )

        return await self._run_tool_span(
            context=context,
            call_next=call_next,
            span_name=span_name,
            arguments=arguments,
            transaction=None,
        )

    async def _run_tool_span(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[ToolResultType]],
        span_name: str,
        arguments: Any,
        transaction: Any | None,
    ) -> ToolResultType:
        with sentry_sdk.start_span(op="mcp.tool", name=span_name) as span:
            message = context.message
            tool_name = getattr(message, "name", "unknown")
            action = _extract_action(arguments if isinstance(arguments, dict) else None)

            span.set_tag("mcp.tool_name", tool_name)
            span.set_tag("mcp.method", "tools/call")
            if action:
                span.set_tag("mcp.action", action)

            if arguments and isinstance(arguments, dict):
                span.set_data("argument_keys", list(arguments.keys()))

            try:
                result = await call_next(context)
                is_error = _is_error_result(result)
                status = "internal_error" if is_error else "ok"
                span.set_status(status)
                if transaction is not None:
                    transaction.set_status(status)
                return result
            except Exception as exc:
                span.set_status("internal_error")
                span.set_data("error_type", type(exc).__name__)
                if transaction is not None:
                    transaction.set_status("internal_error")
                raise
