"""hk 6805/hk 7081: MCP caller argument errors are warnings + counters, not Sentry.

A bad-argument ``tools/call`` must still return the same ``ToolError`` to the
caller, but it must not produce a Sentry event: the fastmcp failure log record
is dropped and a ``ToolError`` whose whole cause is caller input is dropped in
``_before_send``. A server-side exception — including a plain ``ValueError``,
which is a server fault again under hk 7081 — keeps exactly one Sentry event
(the MCP-integration capture) because the duplicate fastmcp log record is
dropped.
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
    CALLER_INPUT_ERROR_TYPE_NAMES,
    MCP_ERROR_WRAPPER_TYPE_NAMES,
    CallerInputError,
    is_marked_mcp_caller_input_error,
    is_mcp_caller_input_error,
    mark_mcp_caller_input_error,
    record_mcp_caller_input_error,
)
from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware
from app.monitoring import sentry as sentry_module
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
            pytest.param(lambda: CallerInputError("symbol is required"), id="typed"),
            pytest.param(
                lambda: _tool_error_from(CallerInputError("bad argument")),
                id="toolerror_from_typed",
            ),
        ],
    )
    def test_typed_caller_input_error_is_caller_input(self, make_exc: Any):
        assert is_mcp_caller_input_error(make_exc()) is True

    def test_plain_value_error_is_a_server_fault(self):
        # hk 7081: the closed caller-input set no longer includes bare
        # ``ValueError`` — internal ``raise ValueError`` sites are server
        # faults and must keep their Sentry event.
        assert is_mcp_caller_input_error(ValueError("internal")) is False
        assert is_mcp_caller_input_error(_tool_error_from(ValueError("x"))) is False

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
        # A server error raised while handling a caller-input error must not
        # be reclassified as caller input.
        try:
            try:
                raise CallerInputError("inner")
            except CallerInputError:
                raise RuntimeError("server bug")
        except RuntimeError as exc:
            assert is_mcp_caller_input_error(exc) is False

    def test_bare_tool_error_is_not_caller_input(self):
        assert is_mcp_caller_input_error(ToolError("no cause")) is False


@pytest.mark.unit
class TestCallerInputMarker:
    def test_mark_survives_toolerror_wrapping(self):
        inner = CallerInputError("bad arg")
        wrapped = _tool_error_from(inner)
        mark_mcp_caller_input_error(wrapped)
        # A further FastMCP wrap keeps the marked cause reachable.
        outer = _tool_error_from(wrapped)
        assert is_marked_mcp_caller_input_error(outer) is True
        assert is_marked_mcp_caller_input_error(ValueError("x")) is False
        assert is_marked_mcp_caller_input_error(None) is False


@pytest.mark.unit
class TestSerializedTypeSetsAreOneDefinition:
    """The Sentry serialized sets must be the live classifier's tuples."""

    def test_sentry_resolves_the_live_classifier_module(self):
        # ``app.monitoring.sentry`` cannot import ``app.mcp_server`` at module
        # level (service import-boundary guards), so it resolves the
        # classifier lazily — this proves the resolved module IS the one
        # definition the live classifier and middleware share.
        import app.mcp_server.caller_input_errors as classifier

        assert sentry_module._mcp_caller_input_errors() is classifier

    @pytest.mark.parametrize("type_name", sorted(CALLER_INPUT_ERROR_TYPE_NAMES))
    def test_every_caller_input_serialized_name_is_dropped(self, type_name: str):
        event = _mcp_exception_event("ToolError", type_name)
        assert _before_send(event, {}) is None

    @pytest.mark.parametrize("type_name", ["ValueError", "RuntimeError", "OSError"])
    def test_non_caller_input_serialized_names_are_kept(self, type_name: str):
        event = _mcp_exception_event("ToolError", type_name)
        assert _before_send(event, {}) is event

    @pytest.mark.parametrize(
        "type_name", sorted(MCP_ERROR_WRAPPER_TYPE_NAMES - {"McpToolDeadlineExceeded"})
    )
    def test_wrapper_only_serialized_names_are_kept(self, type_name: str):
        # Wrapper types alone carry no caller-input signal.
        # (``McpToolDeadlineExceeded`` is excluded: it has its own dedicated
        # drop rule in ``_before_send``.)
        event = _mcp_exception_event(type_name)
        assert _before_send(event, {}) is event


@pytest.mark.unit
class TestRecordMcpCallerInputError:
    def test_warning_and_counter_lines(self, caplog: pytest.LogCaptureFixture):
        with caplog.at_level(logging.INFO, logger="app.mcp_server.caller_input_errors"):
            record_mcp_caller_input_error(
                "get_order_history", _tool_error_from(CallerInputError("bad"))
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
                "spoofed tool=x", _tool_error_from(CallerInputError("bad"))
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
    async def test_caller_input_error_warns_and_marks_and_reraises(
        self, caplog: pytest.LogCaptureFixture
    ):
        middleware = McpToolCallSentryMiddleware()

        async def call_next(context):  # noqa: ANN001, ARG001
            raise _tool_error_from(CallerInputError("bad argument"))

        with caplog.at_level(logging.INFO):
            with pytest.raises(ToolError) as excinfo:
                await middleware.on_call_tool(_middleware_context(), call_next)

        messages = [record.getMessage() for record in caplog.records]
        assert any("mcp.tool.caller_input_error" in m for m in messages)
        assert any("mcp_tool_caller_input_error_total" in m for m in messages)
        assert is_marked_mcp_caller_input_error(excinfo.value)

    @pytest.mark.asyncio
    async def test_plain_value_error_is_not_counted_or_marked(
        self, caplog: pytest.LogCaptureFixture
    ):
        middleware = McpToolCallSentryMiddleware()

        async def call_next(context):  # noqa: ANN001, ARG001
            raise _tool_error_from(ValueError("internal failure"))

        with caplog.at_level(logging.INFO):
            with pytest.raises(ToolError) as excinfo:
                await middleware.on_call_tool(_middleware_context(), call_next)

        messages = [record.getMessage() for record in caplog.records]
        assert not any("caller_input_error" in m for m in messages)
        assert not is_marked_mcp_caller_input_error(excinfo.value)

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
    def test_toolerror_wrapping_typed_error_is_dropped(self):
        assert (
            _before_send(_mcp_exception_event("ToolError", "CallerInputError"), {})
            is None
        )

    def test_toolerror_wrapping_validationerror_is_dropped(self):
        assert (
            _before_send(_mcp_exception_event("ToolError", "ValidationError"), {})
            is None
        )

    def test_toolerror_wrapping_plain_valueerror_is_kept(self):
        # hk 7081 regression: an internal ``ValueError`` must keep its one
        # Sentry event — it is a server fault, not caller input.
        event = _mcp_exception_event("ToolError", "ValueError")
        assert _before_send(event, {}) is event

    def test_toolerror_wrapping_server_fault_is_kept(self):
        event = _mcp_exception_event("ToolError", "RuntimeError")
        assert _before_send(event, {}) is event

    def test_mixed_chain_is_kept(self):
        event = _mcp_exception_event("ToolError", "RuntimeError", "CallerInputError")
        assert _before_send(event, {}) is event

    def test_valueerror_without_mcp_context_is_kept(self):
        event = _mcp_exception_event("ValueError", with_tool_context=False)
        assert _before_send(event, {}) is event

    def test_deadline_exceeded_still_dropped(self):
        event = _mcp_exception_event("McpToolDeadlineExceeded")
        assert _before_send(event, {}) is None


@pytest.mark.unit
class TestBeforeSendMarkedCallerInput:
    """hk 7081: the drop must work on stdio/SSE where no tool context exists."""

    def _hint(self, exc: BaseException) -> dict[str, Any]:
        return {"exc_info": (type(exc), exc, None)}

    def test_marked_error_drops_without_tool_context(self):
        # Simulates stdio/SSE: the exception carries the middleware marker but
        # the event has no ``mcp.tool.name`` tag / ``mcp_tool_call`` context.
        exc = _tool_error_from(CallerInputError("bad arg"))
        mark_mcp_caller_input_error(exc)
        event = _mcp_exception_event(
            "ToolError", "CallerInputError", with_tool_context=False
        )
        assert _before_send(event, self._hint(exc)) is None

    def test_unmarked_typed_error_with_wrapper_drops_without_context(self):
        exc = _tool_error_from(CallerInputError("bad arg"))
        event = _mcp_exception_event(
            "ToolError", "CallerInputError", with_tool_context=False
        )
        assert _before_send(event, self._hint(exc)) is None

    def test_unmarked_plain_valueerror_with_wrapper_is_kept(self):
        exc = _tool_error_from(ValueError("internal"))
        event = _mcp_exception_event("ToolError", "ValueError", with_tool_context=False)
        assert _before_send(event, self._hint(exc)) is not None

    def test_marked_error_drops_even_when_serialized_types_mismatch(self):
        # The marker is the transport-independent signal: it drops the event
        # even when the serialized exception list carries no recognizable
        # caller-input type name.
        exc = _tool_error_from(CallerInputError("bad arg"))
        mark_mcp_caller_input_error(exc)
        event: dict[str, Any] = {"exception": {"values": [{"type": "Weird"}]}}
        assert _before_send(event, self._hint(exc)) is None


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
