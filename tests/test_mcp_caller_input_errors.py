"""hk 6805: MCP caller argument errors are warnings + counters, not Sentry.

A bad-argument ``tools/call`` must still return the same ``ToolError`` to the
caller, but it must not produce a Sentry event: the fastmcp failure log record
is dropped and a ``ToolError`` whose whole cause is caller input is dropped in
``_before_send``. A server-side exception keeps exactly one Sentry event (the
MCP-integration capture) because the duplicate fastmcp log record is dropped.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import Mock

import mcp.types as mt
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMcpValidationError
from fastmcp.server.middleware import MiddlewareContext

from app.mcp_server.caller_input_errors import (
    is_mcp_caller_input_error,
    record_mcp_caller_input_error,
)
from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware
from app.monitoring.sentry import _before_send, _before_send_log


def _tool_error_from(cause: BaseException) -> ToolError:
    """Build the ``ToolError ... from cause`` shape FastMCP produces, unraised."""
    err = ToolError(f"Error calling tool 'probe': {cause}")
    err.__cause__ = cause
    err.__suppress_context__ = True
    return err


@pytest.mark.unit
class TestIsMcpCallerInputError:
    @pytest.mark.parametrize(
        "make_exc",
        [
            pytest.param(lambda: ValueError("symbol is required"), id="value_error"),
            pytest.param(
                lambda: _tool_error_from(ValueError("bad argument")),
                id="toolerror_from_valueerror",
            ),
        ],
    )
    def test_value_error_is_caller_input(self, make_exc: Any):
        assert is_mcp_caller_input_error(make_exc()) is True

    def test_pydantic_validation_error_is_caller_input(self):
        import pydantic

        class Model(pydantic.BaseModel):
            count: int

        try:
            Model(count="not-an-int")
        except pydantic.ValidationError as exc:
            assert is_mcp_caller_input_error(exc) is True
        else:  # pragma: no cover - pydantic always raises here
            pytest.fail("expected ValidationError")

    def test_fastmcp_validation_error_is_caller_input(self):
        assert is_mcp_caller_input_error(FastMcpValidationError("bad args")) is True

    def test_server_fault_is_not_caller_input(self):
        assert is_mcp_caller_input_error(RuntimeError("db down")) is False
        assert is_mcp_caller_input_error(_tool_error_from(RuntimeError("x"))) is False
        assert is_mcp_caller_input_error(None) is False

    def test_mixed_chain_stays_a_server_fault(self):
        # A server error raised while handling a ValueError must not be
        # reclassified as caller input.
        try:
            try:
                raise ValueError("inner")
            except ValueError:
                raise RuntimeError("server bug")
        except RuntimeError as exc:
            assert is_mcp_caller_input_error(exc) is False

    def test_bare_tool_error_is_not_caller_input(self):
        assert is_mcp_caller_input_error(ToolError("no cause")) is False


@pytest.mark.unit
class TestRecordMcpCallerInputError:
    def test_warning_and_counter_lines(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="app.mcp_server.caller_input_errors"):
            record_mcp_caller_input_error(
                "get_order_history", _tool_error_from(ValueError("bad"))
            )
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "mcp.tool.caller_input_error" in m and "tool=get_order_history" in m
            for m in messages
        )
        assert any(
            "mcp_metric metric=mcp_tool_caller_input_error_total" in m
            and "value=1" in m
            for m in messages
        )

    def test_tool_name_is_sanitized(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="app.mcp_server.caller_input_errors"):
            record_mcp_caller_input_error(
                "spoofed tool=x", _tool_error_from(ValueError("bad"))
            )
        messages = [record.getMessage() for record in caplog.records]
        assert any("spoofed\\x20tool\\x3dx" in m for m in messages)


def _middleware_context(tool_name: str = "probe_tool") -> MiddlewareContext[Any]:
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=tool_name, arguments={"a": 1}),
        source="client",
        type="request",
        method="tools/call",
        fastmcp_context=None,
    )


@pytest.mark.unit
class TestSentryMiddlewareCallerInput:
    @pytest.mark.asyncio
    async def test_caller_input_error_warns_and_reraises(
        self, caplog: pytest.LogCaptureFixture
    ):
        middleware = McpToolCallSentryMiddleware()

        async def call_next(context):  # noqa: ANN001, ARG001
            raise _tool_error_from(ValueError("bad argument"))

        with caplog.at_level(logging.INFO):
            with pytest.raises(ToolError):
                await middleware.on_call_tool(_middleware_context(), call_next)

        messages = [record.getMessage() for record in caplog.records]
        assert any("mcp.tool.caller_input_error" in m for m in messages)
        assert any("mcp_tool_caller_input_error_total" in m for m in messages)

    @pytest.mark.asyncio
    async def test_server_fault_has_no_caller_input_signal(
        self, caplog: pytest.LogCaptureFixture
    ):
        middleware = McpToolCallSentryMiddleware()

        async def call_next(context):  # noqa: ANN001, ARG001
            raise _tool_error_from(RuntimeError("db down"))

        with caplog.at_level(logging.INFO):
            with pytest.raises(ToolError):
                await middleware.on_call_tool(_middleware_context(), call_next)

        messages = [record.getMessage() for record in caplog.records]
        assert not any("caller_input_error" in m for m in messages)


def _mcp_exception_event(*types: str, with_tool_context: bool = True) -> dict[str, Any]:
    event: dict[str, Any] = {
        "exception": {
            "values": [{"type": t, "value": "boom"} for t in types],
        },
    }
    if with_tool_context:
        event["tags"] = {"mcp.tool.name": "probe_tool"}
        event["contexts"] = {"mcp_tool_call": {"tool_name": "probe_tool"}}
    return event


@pytest.mark.unit
class TestBeforeSendCallerInput:
    def test_toolerror_wrapping_valueerror_is_dropped(self):
        assert _before_send(_mcp_exception_event("ToolError", "ValueError"), {}) is None

    def test_toolerror_wrapping_validationerror_is_dropped(self):
        assert (
            _before_send(_mcp_exception_event("ToolError", "ValidationError"), {})
            is None
        )

    def test_toolerror_wrapping_server_fault_is_kept(self):
        event = _mcp_exception_event("ToolError", "RuntimeError")
        assert _before_send(event, {}) is event

    def test_mixed_chain_is_kept(self):
        event = _mcp_exception_event("ToolError", "RuntimeError", "ValueError")
        assert _before_send(event, {}) is event

    def test_valueerror_without_mcp_context_is_kept(self):
        event = _mcp_exception_event("ValueError", with_tool_context=False)
        assert _before_send(event, {}) is event

    def test_deadline_exceeded_still_dropped(self):
        event = _mcp_exception_event("McpToolDeadlineExceeded")
        assert _before_send(event, {}) is None


@pytest.mark.unit
class TestBeforeSendFastmcpToolErrorLogs:
    def _log_record(self, name: str, message: str) -> Mock:
        record = Mock()
        record.name = name
        record.getMessage.return_value = message
        return record

    @pytest.mark.parametrize(
        "message",
        [
            "Error calling tool 'probe': bad",
            "Error validating tool 'probe': bad",
        ],
    )
    def test_fastmcp_tool_failure_log_is_dropped(self, message: str):
        record = self._log_record("fastmcp.server.server", message)
        assert _before_send({}, {"log_record": record}) is None

    def test_other_fastmcp_log_is_kept(self):
        record = self._log_record("fastmcp.server.server", "server started")
        event: dict[str, Any] = {"message": "server started", "level": "error"}
        assert _before_send(event, {"log_record": record}) is event

    def test_before_send_log_drops_tool_failure_record(self):
        sentry_log: dict[str, Any] = {
            "body": "Error calling tool 'probe': bad",
            "attributes": {"logger.name": "fastmcp.server.server"},
        }
        assert _before_send_log(sentry_log, {}) is None
