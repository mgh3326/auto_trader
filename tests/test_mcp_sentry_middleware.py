from __future__ import annotations

import copy
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import sentry_sdk
import sentry_sdk.scope as sentry_scope

import app.monitoring.sentry as sentry_module
from app.monitoring.sentry import (
    _truncate_for_sentry,
    build_mcp_tool_call_context,
)


@pytest.mark.unit
class TestTruncateForSentry:
    def test_short_string_unchanged(self):
        assert _truncate_for_sentry("hello") == "hello"

    def test_long_string_truncated(self):
        long = "a" * 2000
        result = _truncate_for_sentry(long)
        assert result.startswith("a" * 1024)
        assert result.endswith("...[truncated]")
        assert len(result) == 1024 + len("...[truncated]")

    def test_small_list_unchanged(self):
        items = list(range(10))
        assert _truncate_for_sentry(items) == items

    def test_large_list_truncated(self):
        items = list(range(50))
        result = _truncate_for_sentry(items)
        assert len(result) == 26
        assert result[:25] == list(range(25))
        assert "truncated" in str(result[-1])
        assert "25 more" in str(result[-1])

    def test_large_tuple_truncated_keeps_type(self):
        items = tuple(range(50))
        result = _truncate_for_sentry(items)
        assert isinstance(result, tuple)
        assert len(result) == 26

    def test_small_dict_unchanged(self):
        d = {f"k{i}": i for i in range(5)}
        assert _truncate_for_sentry(d) == d

    def test_large_dict_truncated(self):
        d = {f"k{i}": i for i in range(50)}
        result = _truncate_for_sentry(d)
        assert len(result) == 26
        assert "...[truncated]" in result
        assert "25 more keys" in str(result["...[truncated]"])

    def test_nested_truncation(self):
        d: dict[str, Any] = {"data": "a" * 2000, "items": list(range(50))}
        result = _truncate_for_sentry(d)
        assert result["data"].endswith("...[truncated]")
        assert len(result["items"]) == 26

    def test_non_container_passthrough(self):
        assert _truncate_for_sentry(42) == 42
        assert _truncate_for_sentry(3.14) == pytest.approx(3.14)
        assert _truncate_for_sentry(None) is None
        assert _truncate_for_sentry(True) is True

    def test_does_not_mutate_original(self):
        original = {"data": "x" * 2000}
        _truncate_for_sentry(original)
        assert len(original["data"]) == 2000


@pytest.mark.unit
class TestBuildMcpToolCallContext:
    def test_basic_context_shape(self):
        ctx = build_mcp_tool_call_context(
            "get_ohlcv", {"symbol": "005930", "period": "day"}
        )
        assert ctx["tool_name"] == "get_ohlcv"
        assert ctx["arguments"]["symbol"] == "005930"
        assert ctx["arguments"]["period"] == "day"

    def test_sensitive_keys_masked(self):
        ctx = build_mcp_tool_call_context(
            "some_tool",
            {
                "authorization": "Bearer secret",
                "token": "abc123",
                "secret": "mysecret",
                "password": "mypass",  # noqa: S105 -- NOSONAR: fixture asserts redaction, not a real secret
                "normal_key": "visible",
            },
        )
        assert ctx["arguments"]["authorization"] == "[Filtered]"
        assert ctx["arguments"]["token"] == "[Filtered]"
        assert ctx["arguments"]["secret"] == "[Filtered]"
        assert ctx["arguments"]["password"] == "[Filtered]"
        assert ctx["arguments"]["normal_key"] == "visible"

    def test_none_arguments_becomes_empty_dict(self):
        ctx = build_mcp_tool_call_context("my_tool", None)
        assert ctx["arguments"] == {}

    def test_does_not_mutate_original(self):
        original = {"key": "value", "token": "secret"}
        original_copy = copy.deepcopy(original)
        build_mcp_tool_call_context("my_tool", original)
        assert original == original_copy

    def test_large_argument_truncated(self):
        ctx = build_mcp_tool_call_context("my_tool", {"data": "x" * 2000})
        assert ctx["arguments"]["data"].endswith("...[truncated]")
        assert len(ctx["arguments"]["data"]) == 1024 + len("...[truncated]")


def _make_tool_context(tool_name: str, arguments: dict[str, Any] | None = None) -> Mock:
    message = Mock()
    message.name = tool_name
    message.arguments = arguments
    ctx = Mock()
    ctx.message = message
    return ctx


class _ScopeRecorder:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.contexts: dict[str, dict[str, Any]] = {}

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def set_context(self, key: str, value: dict[str, Any]) -> None:
        self.contexts[key] = value


def _make_scope_context_manager(scope: Any) -> Mock:
    cm = Mock()
    cm.__enter__ = Mock(return_value=scope)
    cm.__exit__ = Mock(return_value=False)
    return cm


def _make_http_request_ctx(isolation_scope: Any, current_scope: Any) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(
            scope={
                "type": "http",
                "state": {
                    "sentry_sdk.isolation_scope": isolation_scope,
                    "sentry_sdk.current_scope": current_scope,
                },
            }
        )
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestMcpToolCallSentryMiddleware:
    async def test_enriches_request_isolation_scope_for_http_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        ctx = _make_tool_context("get_ohlcv", {"symbol": "005930", "period": "day"})
        request_ctx = ContextVar[Any]("request_ctx")
        request_isolation_scope = _ScopeRecorder()
        request_current_scope = _ScopeRecorder()
        token = request_ctx.set(
            _make_http_request_ctx(request_isolation_scope, request_current_scope)
        )
        monkeypatch.setattr(sentry_module, "_mcp_request_ctx", request_ctx)
        call_next = AsyncMock(return_value={"success": True})

        try:
            with patch.object(
                sentry_sdk,
                "new_scope",
                side_effect=AssertionError("new_scope should not be used"),
            ):
                with patch.object(
                    sentry_sdk,
                    "isolation_scope",
                    side_effect=AssertionError(
                        "isolation_scope should not be used for HTTP requests"
                    ),
                ):
                    result = await middleware.on_call_tool(ctx, call_next)
        finally:
            request_ctx.reset(token)

        assert result == {"success": True}
        assert request_isolation_scope.tags["mcp.tool.name"] == "get_ohlcv"
        assert (
            request_isolation_scope.contexts["mcp_tool_call"]["tool_name"]
            == "get_ohlcv"
        )
        assert (
            request_isolation_scope.contexts["mcp_tool_call"]["arguments"]["symbol"]
            == "005930"
        )
        assert (
            request_isolation_scope.contexts["mcp_tool_call"]["arguments"]["period"]
            == "day"
        )
        assert request_current_scope.tags == {}
        assert request_current_scope.contexts == {}
        call_next.assert_awaited_once_with(ctx)

    async def test_does_not_capture_exception_directly(self):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        ctx = _make_tool_context("get_ohlcv", {"period": "invalid"})
        call_next = AsyncMock(side_effect=ValueError("Invalid period"))

        non_http_scope = _ScopeRecorder()

        with patch.object(
            sentry_sdk,
            "new_scope",
            side_effect=AssertionError("new_scope should not be used"),
        ):
            with patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(non_http_scope),
            ):
                with patch.object(sentry_sdk, "capture_exception") as mock_capture:
                    with pytest.raises(ValueError, match="Invalid period"):
                        await middleware.on_call_tool(ctx, call_next)
                    mock_capture.assert_not_called()

    async def test_uses_per_call_isolation_scope_for_non_http_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        request_ctx = ContextVar[Any]("request_ctx")
        monkeypatch.setattr(sentry_module, "_mcp_request_ctx", request_ctx)
        scopes: list[_ScopeRecorder] = []

        def make_scope() -> Mock:
            scope = _ScopeRecorder()
            scopes.append(scope)
            return _make_scope_context_manager(scope)

        call_next = AsyncMock(return_value={"ok": True})

        with patch.object(
            sentry_sdk,
            "new_scope",
            side_effect=AssertionError("new_scope should not be used"),
        ):
            with patch.object(sentry_sdk, "isolation_scope", side_effect=make_scope):
                await middleware.on_call_tool(
                    _make_tool_context("get_ohlcv", {"symbol": "005930"}),
                    call_next,
                )
                await middleware.on_call_tool(
                    _make_tool_context("get_quote", {"symbol": "AAPL"}),
                    call_next,
                )

        assert len(scopes) == 2
        assert scopes[0].tags["mcp.tool.name"] == "get_ohlcv"
        assert scopes[1].tags["mcp.tool.name"] == "get_quote"
        assert scopes[0].contexts["mcp_tool_call"]["arguments"]["symbol"] == "005930"
        assert scopes[1].contexts["mcp_tool_call"]["arguments"]["symbol"] == "AAPL"
        assert scopes[0].contexts["mcp_tool_call"]["arguments"] == {"symbol": "005930"}
        assert scopes[1].contexts["mcp_tool_call"]["arguments"] == {"symbol": "AAPL"}

    async def test_sensitive_args_masked_in_context(self):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        ctx = _make_tool_context("some_tool", {"token": "secret123", "query": "hello"})
        scope = _ScopeRecorder()
        call_next = AsyncMock(return_value={})

        with patch.object(
            sentry_sdk,
            "new_scope",
            side_effect=AssertionError("new_scope should not be used"),
        ):
            with patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(scope),
            ):
                await middleware.on_call_tool(ctx, call_next)

        args = scope.contexts["mcp_tool_call"]["arguments"]
        assert args["token"] == "[Filtered]"
        assert args["query"] == "hello"

    async def test_http_scope_context_survives_when_sentry_wrapper_reuses_request_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        request_ctx = ContextVar[Any]("request_ctx")
        request_isolation_scope = sentry_sdk.Scope()
        request_current_scope = sentry_sdk.Scope()
        token = request_ctx.set(
            _make_http_request_ctx(request_isolation_scope, request_current_scope)
        )
        monkeypatch.setattr(sentry_module, "_mcp_request_ctx", request_ctx)
        ctx = _make_tool_context(
            "get_ohlcv",
            {"symbol": "005930", "period": "invalid_period", "count": 100},
        )
        call_next = AsyncMock(side_effect=ValueError("Invalid period: invalid_period"))

        try:
            with patch.object(
                sentry_sdk,
                "new_scope",
                side_effect=AssertionError("new_scope should not be used"),
            ):
                with patch.object(
                    sentry_sdk,
                    "isolation_scope",
                    side_effect=AssertionError(
                        "isolation_scope should not be used for HTTP requests"
                    ),
                ):
                    with pytest.raises(ValueError):
                        await middleware.on_call_tool(ctx, call_next)
        finally:
            request_ctx.reset(token)

        with sentry_scope.use_isolation_scope(request_isolation_scope):
            with sentry_scope.use_scope(request_current_scope):
                preserved = sentry_sdk.get_isolation_scope()._contexts["mcp_tool_call"]
                assert (
                    sentry_sdk.get_isolation_scope()._tags["mcp.tool.name"]
                    == "get_ohlcv"
                )
                assert preserved["arguments"]["period"] == "invalid_period"

    async def test_none_arguments_handled(self):
        from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware

        middleware = McpToolCallSentryMiddleware()
        ctx = _make_tool_context("list_tools", None)
        scope = _ScopeRecorder()
        call_next = AsyncMock(return_value={})

        with patch.object(
            sentry_sdk,
            "new_scope",
            side_effect=AssertionError("new_scope should not be used"),
        ):
            with patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(scope),
            ):
                await middleware.on_call_tool(ctx, call_next)

        assert scope.tags["mcp.tool.name"] == "list_tools"
        assert scope.contexts["mcp_tool_call"]["arguments"] == {}
