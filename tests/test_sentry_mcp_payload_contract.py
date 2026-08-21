"""ROB-1305 R6: MCP payload collection is opt-in at the Sentry seam.

These tests intentionally use ordinary prose sentinels rather than
credential-shaped strings.  They exercise both serialized event callbacks and
an in-memory envelope so the contract cannot pass by only filtering one
representation or one event location.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.consts import SPANDATA
from sentry_sdk.transport import Transport

import app.monitoring.sentry as sentry_module

_FAKE_DSN = "https://fakepublickey@fake.invalid.example/123456"
_QUERY_SENTINEL = "ordinary query prose that must not be collected"
_PROMPT_SENTINEL = "ordinary prompt prose that must not be collected"
_RESULT_SENTINEL = "ordinary tool result sentence that must not be collected"
_SENSITIVE_SENTINEL = "FAKE_SENSITIVE_FIXTURE"
_MCP_REQUEST_ARGUMENT_PREFIX = "mcp.request.argument."
_MCP_TOOL_RESULT_CONTENT_KEY = SPANDATA.MCP_TOOL_RESULT_CONTENT
_MCP_PROMPT_RESULT_CONTENT_KEY = SPANDATA.MCP_PROMPT_RESULT_MESSAGE_CONTENT


class _CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__({"dsn": _FAKE_DSN})
        self.envelopes: list[Any] = []

    def capture_envelope(self, envelope: Any) -> None:
        self.envelopes.append(envelope)

    def flush(self, timeout: float, callback: Any = None) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_sentry_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sentry_module, "_initialized", False)
    monkeypatch.setattr(
        sentry_module,
        "_enabled_integration_flags",
        {"fastapi": False, "sqlalchemy": False, "httpx": False, "mcp": False},
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_DSN", _FAKE_DSN)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENVIRONMENT", "test")
    monkeypatch.setattr(sentry_module.settings, "SENTRY_SEND_DEFAULT_PII", False)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_ENABLE_LOG_EVENTS", True)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", False)
    yield
    sentry_sdk.get_global_scope().set_client(None)


def _init_in_memory(
    monkeypatch: pytest.MonkeyPatch, *, include_prompts: bool
) -> _CapturingTransport:
    transport = _CapturingTransport()
    real_init = sentry_sdk.init

    def intercept(**kwargs: Any) -> Any:
        kwargs["transport"] = transport
        kwargs["default_integrations"] = False
        kwargs["auto_enabling_integrations"] = False
        return real_init(**kwargs)

    monkeypatch.setattr(sentry_module.sentry_sdk, "init", intercept)
    monkeypatch.setattr(
        sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", include_prompts
    )
    monkeypatch.setattr(sentry_module.settings, "SENTRY_TRACES_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(sentry_module.settings, "SENTRY_PROFILES_SAMPLE_RATE", 0.0)
    assert sentry_module.init_sentry(service_name="mcp-payload-contract") is True
    return transport


def _event_payloads(
    transport: _CapturingTransport, item_type: str
) -> list[dict[str, Any]]:
    return [
        item.payload.json
        for envelope in transport.envelopes
        for item in envelope.items
        if item.headers.get("type") == item_type
    ]


@pytest.mark.unit
def test_false_gate_removes_generic_mcp_payload_from_transaction_and_error_events():
    assert _MCP_REQUEST_ARGUMENT_PREFIX == "mcp.request.argument."
    assert _MCP_TOOL_RESULT_CONTENT_KEY == "mcp.tool.result.content"
    assert _MCP_PROMPT_RESULT_CONTENT_KEY == "mcp.prompt.result.message_content"
    transaction_event = {
        "transaction": "POST http://127.0.0.1:8765/mcp",
        "tags": {f"{_MCP_REQUEST_ARGUMENT_PREFIX}tagged": _QUERY_SENTINEL},
        "contexts": {
            "mcp_tool_call": {
                "tool_name": "query_tool",
                "arguments": {"query": _QUERY_SENTINEL},
            }
        },
        "extra": {f"{_MCP_REQUEST_ARGUMENT_PREFIX}extra": _QUERY_SENTINEL},
        "breadcrumbs": {
            "values": [
                {"data": {f"{_MCP_REQUEST_ARGUMENT_PREFIX}breadcrumb": _QUERY_SENTINEL}}
            ]
        },
        "measurements": {"mcp.duration_ms": {"value": 12.5}},
        "spans": [
            {
                "op": "mcp.server",
                "span_id": "abc123abc123abc1",
                "status": "ok",
                "start_timestamp": 1000.1,
                "timestamp": 1000.4,
                "data": {
                    "mcp.tool.name": "query_tool",
                    "mcp.method.name": "tools/call",
                    f"{_MCP_REQUEST_ARGUMENT_PREFIX}query": _QUERY_SENTINEL,
                    SPANDATA.MCP_PROMPT_NAME: "query_prompt",
                    SPANDATA.MCP_PROMPT_RESULT_MESSAGE_ROLE: "user",
                    SPANDATA.MCP_PROMPT_RESULT_MESSAGE_COUNT: 1,
                    _MCP_PROMPT_RESULT_CONTENT_KEY: _PROMPT_SENTINEL,
                    _MCP_TOOL_RESULT_CONTENT_KEY: _RESULT_SENTINEL,
                },
            }
        ],
    }
    error_event = {
        "contexts": {
            "mcp_tool_call": {
                "tool_name": "query_tool",
                "arguments": {"query": _QUERY_SENTINEL},
            },
            "mcp_result_context": {
                _MCP_TOOL_RESULT_CONTENT_KEY: _RESULT_SENTINEL,
                _MCP_PROMPT_RESULT_CONTENT_KEY: _PROMPT_SENTINEL,
                SPANDATA.MCP_PROMPT_RESULT_MESSAGE_ROLE: "user",
                SPANDATA.MCP_PROMPT_RESULT_MESSAGE_COUNT: 1,
            },
        },
        "exception": {"values": [{"type": "RuntimeError", "value": "boom"}]},
        "extra": {f"{_MCP_REQUEST_ARGUMENT_PREFIX}error": _QUERY_SENTINEL},
    }

    scrubbed_transaction = sentry_module._before_send_transaction(transaction_event, {})
    scrubbed_error = sentry_module._before_send(error_event, {})

    assert scrubbed_transaction is not None
    assert scrubbed_error is not None
    serialized_transaction = json.dumps(scrubbed_transaction, sort_keys=True)
    serialized_error = json.dumps(scrubbed_error, sort_keys=True)
    for sentinel in (_QUERY_SENTINEL, _PROMPT_SENTINEL, _RESULT_SENTINEL):
        assert sentinel not in serialized_transaction
        assert sentinel not in serialized_error

    span = scrubbed_transaction["spans"][0]
    assert scrubbed_transaction["transaction"] == "tools/call query_tool"
    assert span["op"] == "mcp.server"
    assert span["data"]["mcp.tool.name"] == "query_tool"
    assert span["data"]["mcp.method.name"] == "tools/call"
    assert span["data"][SPANDATA.MCP_PROMPT_NAME] == "query_prompt"
    assert span["data"][SPANDATA.MCP_PROMPT_RESULT_MESSAGE_ROLE] == "user"
    assert span["data"][SPANDATA.MCP_PROMPT_RESULT_MESSAGE_COUNT] == 1
    assert span["status"] == "ok"
    assert span["start_timestamp"] == 1000.1
    assert span["timestamp"] == 1000.4
    assert scrubbed_transaction["measurements"]["mcp.duration_ms"]["value"] == 12.5


@pytest.mark.unit
def test_false_gate_removes_payload_from_serialized_transaction_and_error_envelopes(
    monkeypatch: pytest.MonkeyPatch,
):
    transport = _init_in_memory(monkeypatch, include_prompts=False)

    with sentry_sdk.start_transaction(name="/mcp", op="http.server") as transaction:
        sentry_sdk.set_tag(f"{_MCP_REQUEST_ARGUMENT_PREFIX}tagged", _QUERY_SENTINEL)
        sentry_sdk.set_context(
            "mcp_tool_call",
            {"tool_name": "query_tool", "arguments": {"query": _QUERY_SENTINEL}},
        )
        sentry_sdk.set_extra(f"{_MCP_REQUEST_ARGUMENT_PREFIX}extra", _QUERY_SENTINEL)
        sentry_sdk.add_breadcrumb(
            category="mcp",
            data={f"{_MCP_REQUEST_ARGUMENT_PREFIX}breadcrumb": _QUERY_SENTINEL},
        )
        with transaction.start_child(op="mcp.server", name="tools/call") as span:
            span.set_data("mcp.tool.name", "query_tool")
            span.set_data("mcp.method.name", "tools/call")
            span.set_data(f"{_MCP_REQUEST_ARGUMENT_PREFIX}query", _QUERY_SENTINEL)
            span.set_data(_MCP_TOOL_RESULT_CONTENT_KEY, _RESULT_SENTINEL)
            span.set_data(_MCP_PROMPT_RESULT_CONTENT_KEY, _PROMPT_SENTINEL)
            span.set_data(SPANDATA.MCP_PROMPT_NAME, "query_prompt")
            span.set_data(SPANDATA.MCP_PROMPT_RESULT_MESSAGE_ROLE, "user")
            span.set_data(SPANDATA.MCP_PROMPT_RESULT_MESSAGE_COUNT, 1)
            span.set_tag(f"{_MCP_REQUEST_ARGUMENT_PREFIX}span_tag", _QUERY_SENTINEL)
            span.set_status("ok")

    with sentry_sdk.new_scope() as scope:
        scope.set_context(
            "mcp_tool_call",
            {"tool_name": "query_tool", "arguments": {"query": _QUERY_SENTINEL}},
        )
        scope.set_extra(_MCP_TOOL_RESULT_CONTENT_KEY, _RESULT_SENTINEL)
        sentry_sdk.capture_exception(RuntimeError("MCP operation failed"))

    sentry_sdk.flush()
    transaction_payloads = _event_payloads(transport, "transaction")
    error_payloads = _event_payloads(transport, "event")
    assert transaction_payloads
    assert error_payloads
    serialized = json.dumps(transaction_payloads + error_payloads, sort_keys=True)
    for sentinel in (_QUERY_SENTINEL, _PROMPT_SENTINEL, _RESULT_SENTINEL):
        assert sentinel not in serialized

    transaction_payload = transaction_payloads[-1]
    assert transaction_payload["transaction"] == "tools/call query_tool"
    assert any(span["op"] == "mcp.server" for span in transaction_payload["spans"])
    serialized_transaction = json.dumps(transaction_payload, sort_keys=True)
    assert "query_prompt" in serialized_transaction
    assert '"user"' in serialized_transaction
    assert '"mcp.prompt.result.message_count": 1' in serialized_transaction


@pytest.mark.unit
def test_true_gate_allows_non_sensitive_payload_but_keeps_sensitive_scrubber():
    event = {
        "transaction": "POST http://127.0.0.1:8765/mcp",
        "spans": [
            {
                "op": "mcp.server",
                "data": {
                    "mcp.tool.name": "query_tool",
                    "mcp.method.name": "tools/call",
                    f"{_MCP_REQUEST_ARGUMENT_PREFIX}query": _QUERY_SENTINEL,
                    f"{_MCP_REQUEST_ARGUMENT_PREFIX}api_key": _SENSITIVE_SENTINEL,
                    _MCP_TOOL_RESULT_CONTENT_KEY: _RESULT_SENTINEL,
                    _MCP_PROMPT_RESULT_CONTENT_KEY: _PROMPT_SENTINEL,
                },
            }
        ],
    }

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(sentry_module.settings, "SENTRY_MCP_INCLUDE_PROMPTS", True)
        kept = sentry_module._before_send_transaction(event, {})
    finally:
        monkeypatch.undo()

    assert kept is not None
    serialized = json.dumps(kept, sort_keys=True)
    assert _QUERY_SENTINEL in serialized
    assert _RESULT_SENTINEL in serialized
    assert _PROMPT_SENTINEL in serialized
    assert _SENSITIVE_SENTINEL not in serialized
    assert kept["spans"][0]["data"]["mcp.request.argument.api_key"] == "[Filtered]"
