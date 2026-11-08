"""hk-3589: expected external responses are not Sentry errors; caller-visible failures are.

Each test names the Sentry source it covers. The "still an error" tests pin the
failures the brief keeps at ERROR, so a filter that swallows them fails here.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from sentry_sdk.consts import SPANSTATUS
from sentry_sdk.transport import Transport

import app.monitoring.sentry as sentry_module
from app.mcp_server.timeout_middleware import (
    McpToolDeadlineExceeded,
    ToolTimeoutMiddleware,
)
from app.services.upbit_websocket import UpbitMyOrderWebSocket


def _exception_event(
    exc_type: str, value: str, *, logger: str | None = None
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "exception": {"values": [{"type": exc_type, "value": value}]},
    }
    if logger is not None:
        event["logger"] = logger
    return event


# --- AUTO_TRADER-R: yfinance "N Failed download:" summary -------------------


@pytest.mark.unit
class TestYfinanceFailedDownloadSummary:
    def test_summary_line_is_dropped(self):
        event = {"logger": "yfinance", "message": "\n1 Failed download:"}
        assert sentry_module._before_send(event, {}) is None

    def test_plural_summary_line_is_dropped(self):
        event = {"logger": "yfinance", "message": "2 Failed downloads:"}
        assert sentry_module._before_send(event, {}) is None

    def test_per_symbol_line_with_unknown_reason_is_kept(self):
        """The per-symbol line carries the reason; an unknown reason stays visible."""
        event = {
            "logger": "yfinance",
            "message": "['005930']: YFTzMissingError('no timezone')",
        }
        assert sentry_module._before_send(event, {}) is not None

    def test_summary_log_record_is_dropped_from_structured_logs(self):
        log: Any = {
            "body": "\n1 Failed download:",
            "attributes": {"logger.name": "yfinance"},
        }
        assert sentry_module._before_send_log(log, {}) is None

    def test_summary_text_from_other_logger_is_kept(self):
        event = {"logger": "app.services.x", "message": "1 Failed download:"}
        assert sentry_module._before_send(event, {}) is not None


# --- Caller-visible failures stay ERROR (rule: never silence what the caller sees)


@pytest.mark.unit
class TestCallerVisibleFailuresStayErrors:
    def test_fundamentals_404_returned_to_caller_stays_error(self):
        """AUTO_TRADER-3H family: the tool raises ValueError("Failed to fetch ...")
        back to the caller, so the event is kept even though the wrapped cause is a
        provider 404."""
        event = _exception_event(
            "ValueError",
            "Failed to fetch income data: HTTP Error 404: Not Found",
        )
        assert sentry_module._before_send(event, {}) is not None

    def test_kis_throttle_after_exhausted_retries_stays_error(self):
        """AUTO_TRADER-156: the retry loop has already run out before this
        RuntimeError is raised, so it is a real failure and must stay an error."""
        event = _exception_event("RuntimeError", "초당 거래건수를 초과하였습니다.")
        assert sentry_module._before_send(event, {}) is not None

    def test_plain_toolerror_with_budget_wording_is_kept(self):
        """Only McpToolDeadlineExceeded is dropped; any other ToolError stays."""
        event = _exception_event(
            "ToolError",
            "Tool 'get_operating_briefing' exceeded its 45s time budget and was cancelled.",
        )
        assert sentry_module._before_send(event, {}) is not None


# --- AUTO_TRADER-2CR: deadline exceeded is measured, not an error event ------


@pytest.mark.unit
class TestMcpDeadlineExceeded:
    def test_deadline_exception_event_is_dropped(self):
        event = _exception_event(
            "McpToolDeadlineExceeded",
            "Tool 'get_operating_briefing' exceeded its 45s time budget and was cancelled.",
        )
        assert sentry_module._before_send(event, {}) is None

    def test_deadline_log_from_fastmcp_is_dropped(self):
        event = {
            "logger": "fastmcp.server.server",
            "message": (
                "Error calling tool 'get_operating_briefing': Tool "
                "'get_operating_briefing' exceeded its 45s time budget and was cancelled."
            ),
        }
        assert sentry_module._before_send(event, {}) is None

    def test_deadline_wording_from_other_logger_is_kept(self):
        event = {
            "logger": "app.services.x",
            "message": "Tool 'x' exceeded its 45s time budget and was cancelled.",
        }
        assert sentry_module._before_send(event, {}) is not None

    @pytest.mark.asyncio
    async def test_middleware_timeout_is_toolerror_warning_and_deadline_status(
        self, caplog
    ):
        mw = ToolTimeoutMiddleware(default_timeout_s=0.05)
        span = MagicMock()

        async def call_next(ctx):
            await asyncio.sleep(1.0)
            return "done"

        ctx = SimpleNamespace(message=SimpleNamespace(name="some_slow_tool"))
        with (
            patch(
                "app.mcp_server.timeout_middleware.sentry_sdk.get_current_span",
                return_value=span,
            ),
            caplog.at_level(
                logging.WARNING, logger="app.mcp_server.timeout_middleware"
            ),
            pytest.raises(McpToolDeadlineExceeded) as excinfo,
        ):
            await mw.on_call_tool(ctx, call_next)

        assert isinstance(excinfo.value, ToolError)
        assert "time budget" in str(excinfo.value)
        span.set_status.assert_called_once_with(SPANSTATUS.DEADLINE_EXCEEDED)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("mcp.tool.timeout" in r.getMessage() for r in warnings)
        record = next(r for r in warnings if "mcp.tool.timeout" in r.getMessage())
        assert record.__dict__["mcp.tool.name"] == "some_slow_tool"


# --- AUTO_TRADER-44: Upbit WS reconnect attempts ----------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_upbit_ws_early_reconnect_attempts_warn_and_last_attempt_errors(
    caplog,
):
    client = UpbitMyOrderWebSocket()
    client.reconnect_delay = 0
    failure = RuntimeError("connect refused")

    with (
        patch.object(
            client,
            "_connect_and_subscribe_internal",
            new=AsyncMock(side_effect=failure),
        ),
        patch("app.services.upbit_websocket.asyncio.sleep", new=AsyncMock()),
        caplog.at_level(logging.INFO, logger="app.services.upbit_websocket"),
        pytest.raises(RuntimeError),
    ):
        await client.connect_and_subscribe()

    attempts = [r for r in caplog.records if "WebSocket 연결 실패" in r.getMessage()]
    assert len(attempts) == client.max_reconnect_attempts
    early = attempts[:-1]
    last = attempts[-1]
    assert all(r.levelno == logging.WARNING for r in early)
    assert last.levelno == logging.ERROR
    assert f"시도 {client.max_reconnect_attempts}/{client.max_reconnect_attempts}" in (
        last.getMessage()
    )


# --- Size budget: long transactions are accepted, not too_large --------------


class _CapturingTransport(Transport):
    """Records the serialized bytes of each envelope instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.envelope_sizes: list[int] = []
        self.transaction_spans: list[int] = []

    def capture_envelope(self, envelope) -> None:
        self.envelope_sizes.append(len(envelope.serialize()))
        for item in envelope.items:
            if item.type == "transaction":
                self.transaction_spans.append(len(item.payload.json.get("spans", [])))


_LONG_ATTRIBUTE = "x" * 5000
_LONG_SQL = "SELECT " + "c" * 800


def _send_long_transaction(
    *, span_count: int, max_spans: int, before_send_transaction
) -> tuple[int, int]:
    """Drive the real SDK path with a synthetic long batch transaction.

    Returns (spans_in_envelope, serialized_bytes). Content is an illustrative
    shape (many DB children with oversized attributes), not a production sample.
    """
    import sentry_sdk
    from sentry_sdk.client import Client

    transport = _CapturingTransport()
    client = Client(
        dsn="https://public@example.invalid/1",
        transport=transport,
        traces_sample_rate=1.0,
        before_send_transaction=before_send_transaction,
        _experiments={"max_spans": max_spans},
        # Default integrations patch subprocess.Popen, which the socket guard pins.
        default_integrations=False,
        auto_enabling_integrations=False,
    )
    with sentry_sdk.isolation_scope() as scope:
        scope.set_client(client)
        with sentry_sdk.start_transaction(
            name="tools/call forecast_resolve", op="mcp.server"
        ):
            for index in range(span_count):
                with sentry_sdk.start_span(
                    op="db.sql.query", name=_LONG_SQL + str(index)
                ) as span:
                    span.set_data("mcp.tool.arguments", _LONG_ATTRIBUTE)
                    span.set_data("db.operation", "SELECT")
    client.flush()
    assert transport.envelope_sizes, "no transaction envelope reached the transport"
    assert transport.transaction_spans, "no transaction item in the envelope"
    return transport.transaction_spans[-1], transport.envelope_sizes[-1]


@pytest.mark.unit
def test_long_transaction_size_before_and_after_the_configured_bounds():
    """Size test (brief acceptance 4): the same long batch, before (SDK defaults,
    no truncation) and after (configured cap + span value truncation)."""
    span_count = 1500
    before_spans, before_bytes = _send_long_transaction(
        span_count=span_count,
        max_spans=1000,  # SDK default
        before_send_transaction=None,
    )
    after_spans, after_bytes = _send_long_transaction(
        span_count=span_count,
        max_spans=sentry_module.SENTRY_MAX_SPANS_PER_TRANSACTION,
        before_send_transaction=sentry_module._before_send_transaction,
    )
    print(
        f"\nsize test: before spans={before_spans} bytes={before_bytes}; "
        f"after spans={after_spans} bytes={after_bytes}"
    )
    assert after_spans <= sentry_module.SENTRY_MAX_SPANS_PER_TRANSACTION
    assert after_bytes < before_bytes
    assert after_bytes < 1024 * 1024  # one transaction stays under 1 MiB


@pytest.mark.unit
def test_span_values_are_truncated_per_value_not_per_key():
    event = {
        "type": "transaction",
        "transaction": "tools/call forecast_resolve",
        "spans": [
            {
                "op": "db.sql.query",
                "description": "SELECT " + "c" * 2000,
                "data": {
                    "mcp.tool.arguments": _LONG_ATTRIBUTE,
                    "db.operation": "SELECT",
                },
            }
        ],
    }
    truncated = sentry_module._before_send_transaction(event, {})
    assert truncated is not None
    span = truncated["spans"][0]
    assert span["data"]["mcp.tool.arguments"].endswith("...[truncated]")
    assert span["description"].endswith("...[truncated]")
    assert span["data"]["db.operation"] == "SELECT"  # small values untouched
    assert set(span["data"]) == {"mcp.tool.arguments", "db.operation"}


@pytest.mark.unit
def test_span_cap_is_configured_below_sdk_default_and_queue_above_it():
    assert sentry_module.SENTRY_MAX_SPANS_PER_TRANSACTION < 1000
    assert sentry_module.SENTRY_TRANSPORT_QUEUE_SIZE > 100  # SDK default
