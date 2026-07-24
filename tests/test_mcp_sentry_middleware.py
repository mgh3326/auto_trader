from __future__ import annotations

import copy
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
import sentry_sdk
import sentry_sdk.scope as sentry_scope
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from sentry_sdk.consts import SPANSTATUS

import app.monitoring.sentry as sentry_module
from app.core.config import settings
from app.mcp_server import caller_identity_middleware
from app.mcp_server.caller_identity_middleware import CallerIdentityMiddleware
from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware
from app.monitoring.sentry import (
    _truncate_for_sentry,
    build_mcp_tool_call_context,
    build_mcp_tool_observation,
    extract_mcp_result_envelope,
    resolve_mcp_funnel_stage,
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


def _fresh_cp0_envelope(**overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "success": True,
        "data_state": "fresh",
        "derived_as_of": "2026-07-24T03:00:00+00:00",
        "fetched_at": "2026-07-24T03:00:00+00:00",
        "data_age_seconds": 0.12,
        "cache_hit": False,
        "fallback_source": None,
        "provider_provenance": [
            {
                "provider": "naver",
                "served_by": "naver",
                "mode": "live",
                "status": "ok",
                "error_code": None,
            }
        ],
    }
    envelope.update(overrides)
    return envelope


@pytest.mark.unit
class TestBuildMcpToolObservation:
    def test_extracts_production_tool_result_shape_and_cp0_fields(self):
        artifact_uuid = str(uuid4())
        result = ToolResult(
            structured_content={
                **_fresh_cp0_envelope(),
                "artifact": {"artifact_uuid": artifact_uuid},
            }
        )

        envelope = extract_mcp_result_envelope(result)
        observation = build_mcp_tool_observation(
            "analysis_artifact_save",
            {
                "symbols": ["005930", "000660"],
                "session_label": "kr-open-2026-07-24",
                "correlation_id": "run-42",
            },
            result=result,
            caller_agent_id="scout",
            caller_source="http_header",
        )

        assert envelope is not None
        assert envelope["data_state"] == "fresh"
        assert observation["semantic_success"] is True
        assert observation["span_status"] == SPANSTATUS.OK
        assert observation["consumer"] == "scout"
        assert observation["operator_session"] == "kr-open-2026-07-24"
        assert observation["operator_session_source"] == "session_label"
        assert observation["correlation_id"] == "run-42"
        assert observation["artifact_uuid"] == artifact_uuid
        assert observation["funnel_stage"] == "artifact"
        assert observation["symbol"] == {
            "mode": "batch",
            "count": 2,
            "count_bucket": "2-10",
        }
        freshness = observation["freshness"]
        assert freshness["contract_status"] == "complete"
        assert freshness["data_state"] == "fresh"
        assert freshness["cache_hit"] is False
        assert freshness["derived_as_of"] == "2026-07-24T03:00:00+00:00"
        assert freshness["provider_provenance"][0]["provider"] == "naver"

    def test_absent_cp0_fields_are_unknown_never_fresh(self):
        observation = build_mcp_tool_observation(
            "unrelated_tool",
            {},
            result=ToolResult(structured_content={"success": True}),
        )

        assert observation["semantic_success"] is True
        assert observation["freshness"]["contract_status"] == "absent"
        assert observation["freshness"]["data_state"] == "unknown"
        assert observation["freshness"]["cache_hit"] is None
        assert observation["freshness"]["data_state"] != "fresh"

    @pytest.mark.parametrize("data_state", ["stale", "degraded", "missing"])
    def test_safety_dominant_data_states_are_semantic_failures(self, data_state: str):
        observation = build_mcp_tool_observation(
            "get_news",
            {"symbol": "005930"},
            result=ToolResult(
                structured_content=_fresh_cp0_envelope(data_state=data_state)
            ),
        )

        assert observation["semantic_success"] is False
        assert observation["span_status"] == SPANSTATUS.FAILED_PRECONDITION
        assert observation["error_code"] == f"{data_state}_data"

    def test_provider_fallback_error_code_is_consumed_without_redefining_cp0(self):
        observation = build_mcp_tool_observation(
            "get_news",
            {"symbol": "005930"},
            result=ToolResult(
                structured_content=_fresh_cp0_envelope(
                    data_state="degraded",
                    cache_hit=True,
                    fallback_source="news_articles",
                    provider_provenance=[
                        {
                            "provider": "naver",
                            "served_by": "news_articles",
                            "mode": "fallback",
                            "status": "error",
                            "error_code": "TimeoutError",
                        }
                    ],
                )
            ),
        )

        assert observation["semantic_success"] is False
        assert observation["error_code"] == "TimeoutError"
        freshness = observation["freshness"]
        assert freshness["cache_hit"] is True
        assert freshness["fallback_source"] == "news_articles"

    def test_success_false_and_legacy_degraded_are_semantic_failures(self):
        explicit_failure = build_mcp_tool_observation(
            "route_request",
            {},
            result={"success": False, "error": "missing_intent"},
        )
        legacy_degraded = build_mcp_tool_observation(
            "get_news",
            {},
            result={"success": True, "degraded": True},
        )

        assert explicit_failure["semantic_success"] is False
        assert explicit_failure["error_code"] == "missing_intent"
        assert legacy_degraded["semantic_success"] is False
        assert legacy_degraded["freshness"]["data_state"] == "unknown"
        assert legacy_degraded["error_code"] == "degraded_data"

    def test_invalid_cp0_types_are_not_coerced_to_fresh(self):
        observation = build_mcp_tool_observation(
            "get_news",
            {},
            result=_fresh_cp0_envelope(
                data_state="FRESH",
                cache_hit="false",
                derived_as_of="2026-07-24T03:00:00",
            ),
        )

        freshness = observation["freshness"]
        assert freshness["contract_status"] == "invalid"
        assert freshness["data_state"] == "unknown"
        assert freshness["cache_hit"] is None
        assert freshness["derived_as_of"] is None

    def test_malformed_cp0_collections_fail_closed_without_breaking_telemetry(self):
        observation = build_mcp_tool_observation(
            "get_news",
            {},
            result=_fresh_cp0_envelope(
                data_state=[],
                data_age_seconds=float("inf"),
                provider_provenance=[
                    {
                        "provider": "naver",
                        "served_by": None,
                        "mode": [],
                        "status": "error",
                        "error_code": None,
                    }
                ],
            ),
        )

        freshness = observation["freshness"]
        assert freshness["contract_status"] == "invalid"
        assert freshness["data_state"] == "unknown"
        assert freshness["data_age_seconds"] is None
        assert freshness["provider_provenance"] is None

    def test_cp0_machine_readable_strings_are_consumed_without_local_charset_enum(self):
        observation = build_mcp_tool_observation(
            "get_news",
            {},
            result=_fresh_cp0_envelope(
                fallback_source="cache@edge",
                provider_provenance=[
                    {
                        "provider": "news@edge",
                        "served_by": "cache@edge",
                        "mode": "fallback",
                        "status": "error",
                        "error_code": "provider@timeout",
                    }
                ],
            ),
        )

        freshness = observation["freshness"]
        assert freshness["contract_status"] == "complete"
        assert freshness["fallback_source"] == "cache@edge"
        assert freshness["provider_provenance"][0]["provider"] == "news@edge"

    def test_nested_lineage_and_proposal_id_alias_are_recorded(self):
        proposal_id = str(uuid4())
        report_uuid = str(uuid4())
        observation = build_mcp_tool_observation(
            "order_proposal_create",
            {"analysis_run_id": "analysis-7", "lane": "buy"},
            result=ToolResult(
                structured_content={
                    "success": True,
                    "proposal_id": proposal_id,
                    "result": {"report_uuid": report_uuid},
                }
            ),
            transport_session_id="mcp-session-1",
        )

        assert observation["proposal_uuid"] == proposal_id
        assert observation["report_uuid"] == report_uuid
        assert observation["analysis_run_id"] == "analysis-7"
        assert observation["lane"] == "buy"
        assert observation["operator_session"] == "mcp-session-1"
        assert observation["operator_session_source"] == "mcp_session"
        assert observation["funnel_stage"] == "proposal"

    def test_artifact_id_uuid_compatibility_argument_is_recorded(self):
        artifact_id = str(uuid4())

        observation = build_mcp_tool_observation(
            "analysis_artifact_get",
            {"artifact_id": artifact_id},
            result={"success": False, "error": "not_found"},
        )

        assert observation["artifact_uuid"] == artifact_id

    def test_funnel_stage_labels_are_bounded_and_fill_suffix_is_supported(self):
        assert resolve_mcp_funnel_stage("get_operating_briefing") == "bootstrap"
        assert resolve_mcp_funnel_stage("route_request") == "lane"
        assert resolve_mcp_funnel_stage("analyze_stock") == "evidence"
        assert resolve_mcp_funnel_stage("session_context_get_recent") == "evidence"
        assert resolve_mcp_funnel_stage("toss_get_positions") == "evidence"
        assert resolve_mcp_funnel_stage("investment_report_decide_item") == "verdict"
        assert resolve_mcp_funnel_stage("analysis_artifact_save") == "artifact"
        assert resolve_mcp_funnel_stage("order_proposal_create") == "proposal"
        assert resolve_mcp_funnel_stage("toss_reconcile_orders") == "fill"
        assert resolve_mcp_funnel_stage("save_trade_retrospective") == "retrospective"
        assert resolve_mcp_funnel_stage("list_tools") == "other"


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


class _SpanRecorder:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.data: dict[str, Any] = {}
        self.status: str | None = None

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value

    def set_data(self, key: str, value: Any) -> None:
        self.data[key] = value

    def set_status(self, value: str) -> None:
        self.status = value


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

    async def test_records_semantic_failure_lineage_and_cp0_on_active_span(self):
        middleware = McpToolCallSentryMiddleware()
        report_uuid = str(uuid4())
        ctx = _make_tool_context(
            "get_news",
            {
                "symbol": "005930",
                "analysis_run_id": "analysis-42",
                "correlation_id": "corr-42",
                "payload": {"symbols": ["005930", "000660"]},
            },
        )
        scope = _ScopeRecorder()
        span = _SpanRecorder()
        result = ToolResult(
            structured_content={
                **_fresh_cp0_envelope(
                    data_state="stale",
                    cache_hit=True,
                    fallback_source="news_articles",
                ),
                "report_uuid": report_uuid,
                "verdict": "watch_only",
            }
        )

        with (
            patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(scope),
            ),
            patch.object(sentry_sdk, "get_current_span", return_value=span),
            patch(
                "app.mcp_server.sentry_middleware.get_caller_agent_id",
                return_value="scout",
            ),
            patch(
                "app.mcp_server.sentry_middleware.get_caller_source",
                return_value="http_header",
            ),
            patch(
                "app.mcp_server.sentry_middleware._get_transport_session_id",
                return_value="mcp-session-42",
            ),
        ):
            returned = await middleware.on_call_tool(
                ctx,
                AsyncMock(return_value=result),
            )

        assert returned is result
        assert scope.tags["mcp.consumer"] == "scout"
        assert scope.tags["mcp.operator_session"] == "mcp-session-42"
        assert scope.tags["mcp.analysis_run_id"] == "analysis-42"
        assert scope.tags["mcp.correlation_id"] == "corr-42"
        assert scope.tags["mcp.report_uuid"] == report_uuid
        assert scope.tags["mcp.verdict"] == "watch_only"
        assert scope.tags["mcp.semantic_success"] == "false"
        assert scope.tags["mcp.error_code"] == "stale_data"
        assert scope.tags["mcp.data_state"] == "stale"
        assert scope.tags["mcp.cache_hit"] == "true"
        assert scope.tags["mcp.funnel.stage"] == "evidence"
        assert span.status == SPANSTATUS.FAILED_PRECONDITION
        assert span.data["mcp.semantic_success"] is False
        assert span.data["mcp.derived_as_of"] == "2026-07-24T03:00:00+00:00"
        assert span.data["mcp.data_age_seconds"] == pytest.approx(0.12)
        assert span.data["mcp.provider_provenance"][0]["provider"] == "naver"
        assert span.data["mcp.request.argument.symbol"] == (
            "[Filtered: high-cardinality symbol]"
        )
        assert span.data["mcp.request.argument.payload"]["symbols"] == (
            "[Filtered: high-cardinality symbol]"
        )
        assert "005930" not in str(span.data)
        assert scope.contexts["mcp_tool_call"]["arguments"]["symbol"] == "005930"

    async def test_exception_sets_error_code_and_non_success_span_status(self):
        middleware = McpToolCallSentryMiddleware()
        scope = _ScopeRecorder()
        span = _SpanRecorder()

        with (
            patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(scope),
            ),
            patch.object(sentry_sdk, "get_current_span", return_value=span),
        ):
            with pytest.raises(TimeoutError, match="provider timed out"):
                await middleware.on_call_tool(
                    _make_tool_context("get_news", {"symbol": "AAPL"}),
                    AsyncMock(side_effect=TimeoutError("provider timed out")),
                )

        assert scope.tags["mcp.semantic_success"] == "false"
        assert scope.tags["mcp.error_code"] == "TimeoutError"
        assert span.data["mcp.error_code"] == "TimeoutError"
        assert span.status == SPANSTATUS.DEADLINE_EXCEEDED

    async def test_real_fastmcp_pipeline_uses_tool_result_and_caller_context(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        server = FastMCP("rob-1047-observability-test")
        server.add_middleware(CallerIdentityMiddleware())
        server.add_middleware(McpToolCallSentryMiddleware())

        @server.tool(name="rob_1047_fixture")
        async def fixture(symbol: str) -> dict[str, Any]:
            return {
                **_fresh_cp0_envelope(),
                "symbol": symbol,
                "analysis_run_id": "real-shape-run",
            }

        def no_http_request() -> None:
            raise RuntimeError("no HTTP request")

        monkeypatch.setattr(
            caller_identity_middleware,
            "get_http_request",
            no_http_request,
        )
        monkeypatch.setattr(
            settings,
            "mcp_caller_agent_id_fallback",
            "stdio-consumer",
            raising=False,
        )
        scope = _ScopeRecorder()
        span = _SpanRecorder()

        with (
            patch.object(
                sentry_sdk,
                "isolation_scope",
                return_value=_make_scope_context_manager(scope),
            ),
            patch.object(sentry_sdk, "get_current_span", return_value=span),
        ):
            result = await server.call_tool(
                "rob_1047_fixture",
                {"symbol": "AAPL"},
            )

        assert isinstance(result, ToolResult)
        assert result.structured_content is not None
        assert result.structured_content["data_state"] == "fresh"
        assert scope.tags["mcp.consumer"] == "stdio-consumer"
        assert scope.tags["mcp.caller.source"] == "env_fallback"
        assert scope.tags["mcp.analysis_run_id"] == "real-shape-run"
        assert scope.tags["mcp.semantic_success"] == "true"
        assert span.status == SPANSTATUS.OK
        assert span.data["mcp.request.argument.symbol"] == (
            "[Filtered: high-cardinality symbol]"
        )
