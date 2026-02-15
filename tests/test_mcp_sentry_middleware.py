"""Tests for MCP Sentry tracing middleware."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult

from app.mcp_server.sentry_middleware import (
    McpSentryTracingMiddleware,
    _extract_action,
    _is_error_result,
)


class TestExtractAction:
    def test_extract_action_with_action_key(self):
        arguments = {"action": "buy", "symbol": "AAPL"}
        assert _extract_action(arguments) == "buy"

    def test_extract_action_with_side_key(self):
        arguments = {"side": "sell", "symbol": "AAPL"}
        assert _extract_action(arguments) == "sell"

    def test_extract_action_with_order_type_key(self):
        arguments = {"order_type": "limit", "symbol": "AAPL"}
        assert _extract_action(arguments) == "limit"

    def test_extract_action_priority_action_over_side(self):
        arguments = {"action": "buy", "side": "sell", "symbol": "AAPL"}
        assert _extract_action(arguments) == "buy"

    def test_extract_action_priority_side_over_order_type(self):
        arguments = {"side": "sell", "order_type": "limit", "symbol": "AAPL"}
        assert _extract_action(arguments) == "sell"

    def test_extract_action_none_arguments(self):
        assert _extract_action(None) is None

    def test_extract_action_empty_arguments(self):
        assert _extract_action({}) is None

    def test_extract_action_no_priority_keys(self):
        arguments = {"symbol": "AAPL", "quantity": 10}
        assert _extract_action(arguments) is None

    def test_extract_action_converts_to_string(self):
        arguments = {"action": 123}
        assert _extract_action(arguments) == "123"


class TestIsErrorResult:
    def test_call_tool_result_is_error_true(self):
        result = CallToolResult(content=[], isError=True)
        assert _is_error_result(result) is True

    def test_call_tool_result_is_error_false(self):
        result = CallToolResult(content=[], isError=False)
        assert _is_error_result(result) is False

    def test_tool_result_with_error_in_structured_content(self):
        result = ToolResult(content=[], structured_content={"error": "failed"})
        assert _is_error_result(result) is True

    def test_tool_result_with_is_error_in_structured_content(self):
        result = ToolResult(content=[], structured_content={"isError": True})
        assert _is_error_result(result) is True

    def test_tool_result_with_nested_is_error(self):
        result = ToolResult(
            content=[], structured_content={"result": {"isError": True}}
        )
        assert _is_error_result(result) is True

    def test_tool_result_with_nested_is_error_false(self):
        result = ToolResult(
            content=[], structured_content={"result": {"isError": False}}
        )
        assert _is_error_result(result) is False

    def test_tool_result_no_error(self):
        result = ToolResult(content=[], structured_content={"data": "success"})
        assert _is_error_result(result) is False

    def test_tool_result_none_structured_content(self):
        result = ToolResult(content=[])
        assert _is_error_result(result) is False

    def test_tuple_result_with_error(self):
        result = ([], {"error": "failed"})
        assert _is_error_result(result) is True

    def test_tuple_result_no_error(self):
        result = ([], {"data": "success"})
        assert _is_error_result(result) is False

    def test_unknown_result_type(self):
        assert _is_error_result("not a result") is False


class TestMcpSentryTracingMiddleware:
    @pytest.fixture
    def middleware(self):
        return McpSentryTracingMiddleware()

    @pytest.fixture
    def mock_context(self):
        context = Mock()
        context.message = Mock()
        context.message.name = "place_order"
        context.message.arguments = {"action": "buy", "symbol": "AAPL"}
        return context

    @pytest.mark.asyncio
    async def test_on_call_tool_sets_transaction_name(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_transaction = Mock()
        mock_transaction.name = "old-name"

        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        mock_result = Mock()
        mock_result.isError = False
        mock_result.content = []

        call_next = AsyncMock(return_value=mock_result)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        await middleware.on_call_tool(mock_context, call_next)

        assert mock_transaction.name == "mcp.place_order"
        assert mock_transaction.source == "custom"

    @pytest.mark.asyncio
    async def test_on_call_tool_creates_span_with_tool_name(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = Mock()
        mock_result.isError = False
        mock_result.content = []

        call_next = AsyncMock(return_value=mock_result)

        mock_start_span = Mock(return_value=mock_span)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            mock_start_span,
        )

        await middleware.on_call_tool(mock_context, call_next)

        mock_start_span.assert_called_once_with(op="mcp.tool", name="place_order:buy")

    @pytest.mark.asyncio
    async def test_on_call_tool_sets_tags(self, middleware, mock_context, monkeypatch):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = Mock()
        mock_result.isError = False
        mock_result.content = []

        call_next = AsyncMock(return_value=mock_result)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        await middleware.on_call_tool(mock_context, call_next)

        mock_span.set_tag.assert_any_call("mcp.tool_name", "place_order")
        mock_span.set_tag.assert_any_call("mcp.method", "tools/call")
        mock_span.set_tag.assert_any_call("mcp.action", "buy")

    @pytest.mark.asyncio
    async def test_on_call_tool_stores_argument_keys_only(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = Mock()
        mock_result.isError = False
        mock_result.content = []

        call_next = AsyncMock(return_value=mock_result)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        await middleware.on_call_tool(mock_context, call_next)

        mock_span.set_data.assert_any_call("argument_keys", ["action", "symbol"])

    @pytest.mark.asyncio
    async def test_on_call_tool_sets_ok_status_on_success(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = CallToolResult(content=[], isError=False)

        call_next = AsyncMock(return_value=mock_result)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        await middleware.on_call_tool(mock_context, call_next)

        mock_span.set_status.assert_called_with("ok")

    @pytest.mark.asyncio
    async def test_on_call_tool_sets_error_status_on_error_result(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = CallToolResult(content=[], isError=True)

        call_next = AsyncMock(return_value=mock_result)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        await middleware.on_call_tool(mock_context, call_next)

        mock_span.set_status.assert_called_with("internal_error")

    @pytest.mark.asyncio
    async def test_on_call_tool_sets_error_status_on_exception(
        self, middleware, mock_context, monkeypatch
    ):
        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        call_next = AsyncMock(side_effect=ValueError("Tool failed"))

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            Mock(return_value=mock_span),
        )

        with pytest.raises(ValueError, match="Tool failed"):
            await middleware.on_call_tool(mock_context, call_next)

        mock_span.set_status.assert_called_with("internal_error")
        mock_span.set_data.assert_any_call("error_type", "ValueError")

    @pytest.mark.asyncio
    async def test_on_call_tool_without_action(self, middleware, monkeypatch):
        context = Mock()
        context.message = Mock()
        context.message.name = "get_quote"
        context.message.arguments = {"symbol": "AAPL"}

        mock_span = MagicMock()
        mock_span.__enter__ = Mock(return_value=mock_span)
        mock_span.__exit__ = Mock(return_value=False)
        mock_span.set_tag = Mock()
        mock_span.set_data = Mock()
        mock_span.set_status = Mock()

        mock_scope = Mock()
        mock_scope.transaction = None

        mock_result = Mock()
        mock_result.isError = False
        mock_result.content = []

        call_next = AsyncMock(return_value=mock_result)

        mock_start_span = Mock(return_value=mock_span)

        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.get_current_scope",
            Mock(return_value=mock_scope),
        )
        monkeypatch.setattr(
            "app.mcp_server.sentry_middleware.sentry_sdk.start_span",
            mock_start_span,
        )

        await middleware.on_call_tool(context, call_next)

        mock_start_span.assert_called_once_with(op="mcp.tool", name="get_quote")
        calls = [str(call) for call in mock_span.set_tag.call_args_list]
        assert not any("mcp.action" in call for call in calls)
